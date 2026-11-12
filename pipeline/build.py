#!/usr/bin/env python3
"""Run the whole pipeline: raw county files in, map layer and shortlist out.

  1. normalize      manifest-driven field mapping, owner typing, farm rule,
                    count check against each county's published total
  2. llc_registry   farmland LLCs joined to the state registry -> named people
  3. resolve_owners name variants -> one owner; family / control groups
  4. footprint      each owner's acres, parcels and counties across the region
  5. rent, value    rent estimate, three valuations, hold-out miss
  6. screen         eight default tests, chained -> shortlist
  7. map            GeoJSON (and PMTiles if tippecanoe is installed), owner
                    search index, funnel and summary for the front end

Usage:
  python -m pipeline.build --sample sample --out build
  python -m pipeline.build --sample sample --out build --pmtiles
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from . import llc_registry, normalize, resolve_owners, screen, valuation

VAL_DATE = date(2026, 11, 1)


def centroid(geom) -> tuple[float, float] | None:
    if not geom:
        return None
    ring = geom["coordinates"][0] if geom["type"] == "Polygon" else geom["coordinates"][0][0]
    pts = ring[:-1] or ring
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def bbox(geom):
    ring = geom["coordinates"][0] if geom["type"] == "Polygon" else geom["coordinates"][0][0]
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    return [min(xs), min(ys), max(xs), max(ys)]


def read_csv(path: Path) -> list[dict]:
    with path.open() as fh:
        return list(csv.DictReader(fh))


def r(v, d=0):
    return None if v is None else round(v, d) if d else int(round(v))


def build(sample: Path, out: Path, pmtiles: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    (out / "map").mkdir(exist_ok=True)
    manifest = json.loads((sample / "manifest.json").read_text())

    print("1. normalize")
    st = normalize.normalize(sample / "manifest.json", sample / "raw", sample / "overlays.csv",
                             out / "parcels.geojsonl")
    if st["count_mismatch"]:
        raise SystemExit(f"count check failed: {st['count_mismatch']}")
    feats = [json.loads(s) for s in (out / "parcels.geojsonl").open()]
    on_map = [f for f in feats if f["properties"]["on_map"]]
    P = [f["properties"] for f in on_map]
    print(f"   {st['parcels']:,} parcels, {st['farm']:,} farm, {len(on_map):,} on the map")

    print("2. trace LLCs through the state registry")
    llc_rows, llc_st = llc_registry.join_registry(llc_registry.farmland_llcs(P), sample / "registry")
    with (out / "llc_owners.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=llc_registry.COLS)
        w.writeheader()
        w.writerows(llc_rows)
    llc_by_norm = {llc_registry.norm_entity(x["llc_name"]): x for x in llc_rows}
    print(f"   {llc_st['with_people']} of {llc_st['llcs']} farmland LLCs traced to named people "
          f"({llc_st['ambiguous']} ambiguous, left blank; {llc_st['absentee']} absentee)")

    print("3. resolve owners")
    owners: dict[str, dict] = {}
    for p in P:
        o = owners.setdefault(p["owner"], {"name": p["owner"], "counties": set(), "mails": set(),
                                           "files": set()})
        o["counties"].add(p["county"])
        if p["mail_addr"]:
            o["mails"].add(f"{p['mail_addr'].upper()}|{p['mail_zip']}")
        rec = llc_by_norm.get(llc_registry.norm_entity(p["owner"]))
        if rec and rec["sos_file_no"]:
            o["files"] |= {f.strip() for f in rec["sos_file_no"].split(";")}
    llc_people = {}
    for name in owners:
        rec = llc_by_norm.get(llc_registry.norm_entity(name))
        if rec and rec["people"]:
            llc_people[name] = rec["people"].split(" | ")
    oid, ogrp, log = resolve_owners.resolve(list(owners.values()), llc_people)
    with (out / "owner_merge_log.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["a", "b", "pass", "decision", "reason"])
        w.writeheader()
        w.writerows(log)
    merges = sum(1 for x in log if x["decision"] == "merge")
    print(f"   {len(owners)} owner strings -> {len(set(oid.values()))} owners "
          f"({merges} merges, {sum(1 for x in log if x['decision'] == 'refuse')} refused), "
          f"{len(set(ogrp.values()))} family/control groups")

    print("4. owner footprint")
    foot = defaultdict(lambda: {"ac": 0.0, "par": 0, "cty": set()})
    for p in P:
        f = foot[oid[p["owner"]]]
        f["ac"] += p["gis_acres"]
        f["par"] += 1
        f["cty"].add(p["county"])

    print("5. rent and three valuations")
    rents = {x["county"]: float(x["cash_rent"]) for x in read_csv(sample / "county_rent.csv")}
    mean_pi = {}
    for c in rents:
        w = [(p["pi"], p["gis_acres"]) for p in P if p["county"] == c and p.get("pi")]
        mean_pi[c] = sum(a * b for a, b in w) / sum(b for _, b in w)
    elevators = [(float(e["lon"]), float(e["lat"])) for e in read_csv(sample / "elevators.csv")]
    pmap = {}
    for f in on_map:
        p = f["properties"]
        p["xy"] = centroid(f["geometry"])
        p["rent"] = valuation.rent_estimate(p.get("pi"), rents.get(p["county"]), mean_pi.get(p["county"]))
        p["elevator_mi"] = min(valuation.miles(p["xy"], e) for e in elevators)
        pmap[(p["county"], p["pin"])] = p
    sales = []
    last_sale = {}
    for s in read_csv(sample / "sales.csv"):
        rec = {"key": (s["county"], s["pin"]), "date": date.fromisoformat(s["date"]),
               "price": float(s["price"]), "acres": float(s["acres"]),
               "flags": set(x for x in s["flags"].split(";") if x)}
        sales.append(rec)
        if rec["key"] not in last_sale or rec["date"] > last_sale[rec["key"]]["date"]:
            last_sale[rec["key"]] = rec
    adj = {e["county"]: set(e.get("neighbors") or []) for e in manifest}
    V = valuation.Valuer(sales, pmap, adj, VAL_DATE)
    ho = V.holdout()
    print(f"   comps pool {len(V.pool)} sales; hold-out median miss {ho['median_miss']:.1%} "
          f"(n {ho['n']}, {ho['within_15pct']:.0%} within 15%); implied cap rate {V.cap:.2%}")
    for p in P:
        v = V.value((p["county"], p["pin"]), p.get("pi"), p["gis_acres"], p["xy"], p["rent"])
        p["val"] = v
        p["val_status"] = v["status"]
        mid = (v["lo"] + v["hi"]) / 2 if v["status"] == "agree" else None
        p["rent_yield"] = p["rent"] / mid if mid and p["rent"] else None

    print("6. screen")
    funnel, short = screen.run(P)
    short_ids = {(p["county"], p["pin"]) for p in short}
    for row in funnel:
        cut = "" if row["cut"] is None else f"{row['cut']:+,}"
        print(f"   {row['remaining']:>6,} {cut:>7}  {row['test']}")
    short_owners = {oid[p["owner"]] for p in short}
    print(f"   shortlist: {len(short)} parcels, {sum(p['gis_acres'] for p in short):,.0f} acres, "
          f"{len(short_owners)} owners")

    print("7. map layer and search index")
    gid_n = {g: i for i, g in enumerate(sorted(set(ogrp.values())))}
    out_feats, idx = [], defaultdict(lambda: {"names": Counter(), "ac": 0.0, "par": 0, "cty": set(),
                                               "t": Counter(), "b": [180, 90, -180, -90], "sx": 0, "sy": 0})
    group_size = Counter(ogrp[n] for n in set(p["owner"] for p in P))
    for f in on_map:
        p = f["properties"]
        o = oid[p["owner"]]
        ft = foot[o]
        llc = llc_by_norm.get(llc_registry.norm_entity(p["owner"])) if p["owner_type"] == "entity" else None
        v = p["val"]
        ls = last_sale.get((p["county"], p["pin"]))
        props = {
            "pin": p["pin"], "county": p["county"], "owner": p["owner"], "otype": p["owner_type"],
            "oi": o, "gi": gid_n[ogrp[p["owner"]]] if group_size[ogrp[p["owner"]]] > 1 else None,
            "acres": r(p["gis_acres"], 1), "facres": p["farm_acres"], "class": p["class_raw"],
            "basis": p["farm_basis"], "crop": int(bool(p["is_crop"])),
            "o_ac": r(ft["ac"]), "o_par": ft["par"], "o_cty": len(ft["cty"]),
            "pi": r(p.get("pi"), 1), "till": p.get("till_share"), "flood": p.get("flood_share"),
            "wet": p.get("wetland_share"), "road": p.get("road"), "elev_mi": r(p["elevator_mi"], 1),
            "rent": r(p["rent"]), "v_comps": r(v.get("comps")), "v_soil": r(v.get("soil")),
            "v_rent": r(v.get("rent_cap")), "v_n": v.get("comps_n"), "v_status": v["status"],
            "v_lo": r(v.get("lo")), "v_hi": r(v.get("hi")),
            "yield": r(p["rent_yield"], 4) if p["rent_yield"] else None,
            "short": int((p["county"], p["pin"]) in short_ids),
            "last_sale": ls["date"].isoformat() if ls else None,
            "last_ppa": r(ls["price"] / ls["acres"]) if ls else None,
            "auction": int(bool(ls and "auction" in ls["flags"])),
        }
        if llc and llc["people"]:
            props.update(people=llc["people"], p_addr=llc["addresses"], mgr_st=llc["mgr_states"],
                         absentee=int(llc["absentee"]))
        elif llc and int(llc["sos_ambiguous"]):
            props["ambiguous"] = 1
        props = {k: v for k, v in props.items() if v is not None}
        out_feats.append({"type": "Feature", "geometry": f["geometry"], "properties": props})

        a = idx[o]
        a["names"][p["owner"]] += p["gis_acres"]
        a["ac"] += p["gis_acres"]
        a["par"] += 1
        a["cty"].add(p["county"])
        a["t"][p["owner_type"]] += 1
        bb = bbox(f["geometry"])
        a["b"] = [min(a["b"][0], bb[0]), min(a["b"][1], bb[1]), max(a["b"][2], bb[2]), max(a["b"][3], bb[3])]
        a["sx"] += p["xy"][0] * p["gis_acres"]
        a["sy"] += p["xy"][1] * p["gis_acres"]
        if "people" in props:
            a["ppl"], a["pa"], a["ab"] = props["people"], props["p_addr"], props["absentee"]

    geo = {"type": "FeatureCollection", "features": out_feats}
    (out / "map" / "parcels.geojson").write_text(json.dumps(geo, separators=(",", ":")))
    owners_idx = []
    for o, a in idx.items():
        names = [n for n, _ in a["names"].most_common()]
        owners_idx.append({"n": names[0], "v": " | ".join(names[1:]), "oi": o,
                           "t": a["t"].most_common(1)[0][0], "a": r(a["ac"]), "p": a["par"],
                           "c": sorted(a["cty"]), "x": round(a["sx"] / a["ac"], 5),
                           "y": round(a["sy"] / a["ac"], 5), "b": [round(x, 5) for x in a["b"]],
                           "ppl": a.get("ppl", ""), "pa": a.get("pa", ""), "ab": a.get("ab", 0)})
    owners_idx.sort(key=lambda o: -o["a"])
    (out / "map" / "owners.json").write_text(json.dumps(owners_idx, separators=(",", ":")))

    tiles = "geojson"
    if pmtiles:
        if shutil.which("tippecanoe"):
            subprocess.run(["tippecanoe", "-o", str(out / "map" / "parcels.pmtiles"), "-l", "parcels",
                            "-Z8", "-z15", "--drop-densest-as-needed", "--extend-zooms-if-still-dropping",
                            "--coalesce-densest-as-needed", "--simplification=4", "--force",
                            str(out / "map" / "parcels.geojson")], check=True)
            tiles = "pmtiles"
        else:
            print("   tippecanoe not found; serving GeoJSON")

    type_ac = Counter()
    for p in P:
        type_ac[p["owner_type"]] += p["gis_acres"]
    tot = sum(type_ac.values())
    meta = {
        "synthetic": True, "tiles": tiles, "counties": [e["county"] for e in manifest],
        "parcels_downloaded": st["parcels"], "farm_parcels": st["farm"], "on_map": len(on_map),
        "acres_on_map": r(tot), "owners": len(set(oid.values())),
        "owner_type_acres_share": {k: round(v / tot, 3) for k, v in type_ac.most_common()},
        "llc": llc_st, "holdout": ho, "cap_rate": round(V.cap, 4),
        "agree_share": round(sum(p["val_status"] == "agree" for p in P) / len(P), 3),
        "funnel": funnel,
        "shortlist": {"parcels": len(short), "acres": r(sum(p["gis_acres"] for p in short)),
                      "owners": len(short_owners)},
        "county_pi": {c: round(v, 1) for c, v in mean_pi.items()},
        "center": [round(sum(p["xy"][0] for p in P) / len(P), 4), round(sum(p["xy"][1] for p in P) / len(P), 4)],
    }
    (out / "map" / "meta.json").write_text(json.dumps(meta, indent=1))
    with (out / "shortlist.csv").open("w", newline="") as fh:
        cols = ["county", "pin", "owner", "owner_type", "gis_acres", "pi", "till_share", "rent", "val_status",
                "rent_yield"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for p in sorted(short, key=lambda p: -(p["rent_yield"] or 0)):
            w.writerow({**p, "gis_acres": r(p["gis_acres"], 1), "rent": r(p["rent"]),
                        "rent_yield": r(p["rent_yield"], 4)})
    print(f"   -> {out / 'map'}  ({tiles})")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=Path, default=Path("sample"))
    ap.add_argument("--out", type=Path, default=Path("build"))
    ap.add_argument("--pmtiles", action="store_true", help="also build vector tiles with tippecanoe")
    a = ap.parse_args()
    build(a.sample, a.out, a.pmtiles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
