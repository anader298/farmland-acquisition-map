#!/usr/bin/env python3
"""Normalize raw county parcel files into one parcel table.

Every county names its fields differently (owner is `Owner`, `OWNER_NAME`,
`own_full`, or a surname / first-name / middle-name split across columns), so
the field mapping lives in the endpoint manifest, one entry per county, and
this step projects every parcel into one schema:

  county, pin, owner, owner_norm, owner_type, mail (address, city, state, zip),
  gis_acres, farm_acres, class_raw, farm_basis, farm_signal, is_crop, on_map,
  plus the overlay columns (soil index, tillable share, flood, wetland, road)

Before anything is joined, each county's row count is checked against the
count the county publishes. A county that does not match fails the build.

Usage:
  python -m pipeline.normalize --manifest sample/manifest.json --raw sample/raw \
      --overlays sample/overlays.csv --out build/parcels.geojsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path

from . import farm_rule as FR
from .owner_type import norm_owner, owner_type

ACRE_M2 = 4046.8564224


def clean_name(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return s if re.search(r"[A-Za-z0-9]", s) else ""


def owner_of(props: dict, e: dict) -> str:
    """The owner name for one raw feature, per the county's manifest entry.

    Counties that split the name into columns list column groups in
    `owner_concat`; the first group whose surname column is filled wins.
    Reading only the surname column would make every SMITH one owner.
    """
    for group in e.get("owner_concat") or []:
        parts = [clean_name(props.get(k)) for k in group]
        if parts and parts[0]:
            return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    if e.get("owner_concat"):
        return ""
    return clean_name(props.get(e.get("owner_field"))) if e.get("owner_field") else ""


def ring_area_m2(ring: list) -> float:
    """Planar shoelace area of a lon/lat ring after a local equirectangular
    projection. Good to well under 1% at parcel scale; production uses
    ST_Area in EPSG:5070 (Albers equal area) inside DuckDB spatial."""
    if len(ring) < 4:
        return 0.0
    lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
    kx, ky = 111_320.0 * math.cos(lat0), 110_540.0
    a = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        a += (x1 * kx) * (y2 * ky) - (x2 * kx) * (y1 * ky)
    return abs(a) / 2


def geom_acres(geom: dict | None) -> float | None:
    if not geom:
        return None
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    m2 = sum(ring_area_m2(p[0]) - sum(ring_area_m2(h) for h in p[1:]) for p in polys)
    return m2 / ACRE_M2


def load_overlays(path: Path | None) -> dict[tuple[str, str], dict]:
    """(county, pin) -> overlay columns. In production these come from DuckDB
    spatial joins of each parcel against soil, CDL, FEMA and NWI polygons (see
    pipeline/sql/soil_overlay.sql); the sample ships them precomputed."""
    if not path or not path.exists():
        return {}
    out = {}
    with path.open() as fh:
        for r in csv.DictReader(fh):
            out[(r["county"], r["pin"])] = {k: FR.num(v) for k, v in r.items()
                                            if k not in ("county", "pin")}
    return out


def normalize(manifest: Path, raw_dir: Path, overlays: Path | None, out: Path) -> dict:
    entries = json.loads(manifest.read_text())
    ov = load_overlays(overlays)
    out.parent.mkdir(parents=True, exist_ok=True)
    stats = {"counties": [], "parcels": 0, "farm": 0, "on_map": 0, "count_mismatch": []}
    with out.open("w") as fh:
        for e in entries:
            county = e["county"]
            src = raw_dir / f"{county.replace(' ', '_')}.geojsonl"
            n = 0
            for line in src.open():
                ft = json.loads(line)
                p = ft.get("properties") or {}
                n += 1
                pin = str(p.get(e["pin_field"]) or "").strip()
                owner = owner_of(p, e)
                gis = geom_acres(ft.get("geometry"))
                sig = FR.signals(p, e)
                o = ov.get((county, pin), {})
                basis, why = FR.classify(farm_class=sig["farm_class"], class_raw=sig["class_raw"],
                                         farm_acres=sig["farm_acres"], farm_land_av=sig["farm_land_av"],
                                         cult_acres=sig["cult_acres"], sat_share=o.get("sat_share"),
                                         gis_acres=gis)
                rec = {
                    "county": county, "pin": pin, "owner": owner,
                    "owner_norm": norm_owner(owner), "owner_type": owner_type(owner),
                    "mail_addr": clean_name(p.get(e.get("mail_addr_field"))),
                    "mail_city": clean_name(p.get(e.get("mail_city_field"))),
                    "mail_state": clean_name(p.get(e.get("mail_state_field"))),
                    "mail_zip": clean_name(p.get(e.get("mail_zip_field"))),
                    "gis_acres": None if gis is None else round(gis, 2),
                    "deeded_acres": FR.num(p.get(e.get("acres_field"))),
                    "farm_acres": sig["farm_acres"], "class_raw": sig["class_raw"],
                    "farm_basis": basis, "farm_signal": why,
                    "is_crop": FR.is_crop(basis, o.get("till_share")),
                    "on_map": FR.on_map(basis, gis, ft.get("geometry") is not None),
                    **o,
                }
                fh.write(json.dumps({"type": "Feature", "geometry": ft.get("geometry"),
                                     "properties": rec}, separators=(",", ":")) + "\n")
                stats["parcels"] += 1
                stats["farm"] += FR.is_farm(basis)
                stats["on_map"] += rec["on_map"]
            expected = e.get("feature_count")
            ok = n == expected
            stats["counties"].append({"county": county, "rows": n, "published": expected, "match": ok})
            if not ok:
                stats["count_mismatch"].append(county)
            print(f"  {'PASS' if ok else 'FAIL'} {county}: {n:,} rows, county publishes {expected:,}")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--overlays", type=Path)
    ap.add_argument("--out", type=Path, default=Path("build/parcels.geojsonl"))
    a = ap.parse_args()
    st = normalize(a.manifest, a.raw, a.overlays, a.out)
    print(f"{st['parcels']:,} parcels, {st['farm']:,} farm, {st['on_map']:,} on the map -> {a.out}")
    return 1 if st["count_mismatch"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
