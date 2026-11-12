#!/usr/bin/env python3
"""Trace farmland LLCs to the named people behind them.

The Illinois Secretary of State publishes its full LLC registry as free bulk
files, republished daily:

  llcallnam.txt  file number (8) + entity name                  -> name match
  llcallmgr.txt  file number (8) + manager/member name + address -> the people

The join is on normalized entity name. Records are fixed-width and
right-trimmed, so every line is padded before slicing; without the padding
most rows silently lose their trailing fields.

Two rules keep the join honest:

  * The LLC must be the owner, not the care-of contact. "SMITH JOHN C/O
    EXAMPLE FARMS LLC" is John Smith's land, so the care-of tail is cut
    before matching.
  * One owner string matching two or more file numbers (a dissolved LLC and
    a new one under the same name) gets no people at all. The bulk files
    carry no status, so there is no way to tell which one holds the land,
    and listing both would attach strangers to the parcel.

Absentee = every manager or member on file has an out-of-state address.

Usage:
  python -m pipeline.llc_registry --parcels build/parcels.geojsonl \
      --registry sample/registry --out build/llc_owners.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

from .owner_type import CARE_TAIL

HOME_STATE = "IL"

# Fixed-width offsets of the published files.
NAM_FILE_NO, NAM_NAME = (0, 8), (8, 68)
MGR_FILE_NO, MGR_NAME, MGR_ADDR = (0, 8), (8, 68), (68, 113)
MGR_CITY, MGR_STATE, MGR_ZIP = (113, 143), (143, 145), (145, 154)

LLC_HINT = re.compile(r"\b(L\s*\.?\s*L\s*\.?\s*C|LLC|LC)\b\.?", re.I)
COLS = ["llc_name", "farm_acres", "parcels", "counties", "sos_file_no", "sos_ambiguous",
        "n_people", "people", "addresses", "mgr_states", "absentee"]


def norm_entity(name: str) -> str:
    """Normalize an entity name so the assessor's string matches the registry's."""
    n = (name or "").upper()
    n = CARE_TAIL.sub("", n)
    n = re.sub(r"[.,'#]", "", n)
    n = re.sub(r"\bL\s*L\s*C\b", "LLC", n)
    n = re.sub(r"\s+", " ", n).strip()
    return re.sub(r"\s+(ETAL|ET AL)$", "", n)


def slice_fixed(line: str, span: tuple[int, int], width: int = 200) -> str:
    return line.ljust(width)[span[0]:span[1]].strip()


def farmland_llcs(parcels: list[dict]) -> dict[str, dict]:
    """normalized LLC name -> {example_name, acres, parcels, counties}."""
    want: dict[str, dict] = {}
    for p in parcels:
        if p.get("owner_type") != "entity":
            continue
        key = norm_entity(p["owner"])
        if not key or not LLC_HINT.search(key):
            continue
        rec = want.setdefault(key, {"example_name": p["owner"], "acres": 0.0,
                                    "parcels": 0, "counties": set()})
        rec["acres"] += float(p.get("gis_acres") or 0)
        rec["parcels"] += 1
        rec["counties"].add(p["county"])
    return want


def join_registry(want: dict[str, dict], registry: Path) -> tuple[list[dict], dict]:
    name_to_files: dict[str, list[str]] = defaultdict(list)
    with (registry / "llcallnam.txt").open(errors="replace") as fh:
        next(fh, None)                                  # RUN DATE header
        for line in fh:
            if len(line) < 10:
                continue
            fno = slice_fixed(line, NAM_FILE_NO)
            nm = norm_entity(slice_fixed(line, NAM_NAME))
            if nm in want and fno not in name_to_files[nm]:
                name_to_files[nm].append(fno)
    ambiguous = {nm for nm, fs in name_to_files.items() if len(fs) > 1}
    wanted = {fs[0] for nm, fs in name_to_files.items() if nm not in ambiguous}

    mgrs: dict[str, list[dict]] = defaultdict(list)
    with (registry / "llcallmgr.txt").open(errors="replace") as fh:
        next(fh, None)
        for line in fh:
            fno = slice_fixed(line, MGR_FILE_NO)
            if len(line) < 10 or fno not in wanted:
                continue
            mgrs[fno].append({k: slice_fixed(line, s) for k, s in (
                ("name", MGR_NAME), ("addr", MGR_ADDR), ("city", MGR_CITY),
                ("state", MGR_STATE), ("zip", MGR_ZIP))})

    out, with_people, absentee_n = [], 0, 0
    for nm, rec in sorted(want.items(), key=lambda kv: -kv[1]["acres"]):
        files = name_to_files.get(nm, [])
        people = [] if nm in ambiguous else [p for f in files for p in mgrs.get(f, [])]
        states = sorted({p["state"] for p in people if p["state"]})
        absentee = bool(states) and all(s != HOME_STATE for s in states)
        with_people += bool(people)
        absentee_n += absentee
        out.append({
            "llc_name": rec["example_name"], "farm_acres": round(rec["acres"], 1),
            "parcels": rec["parcels"], "counties": "; ".join(sorted(rec["counties"])),
            "sos_file_no": "; ".join(files), "sos_ambiguous": int(nm in ambiguous),
            "n_people": len(people),
            "people": " | ".join(p["name"] for p in people[:8]),
            "addresses": " | ".join(f"{p['addr']}, {p['city']} {p['state']} {p['zip']}".strip(", ")
                                    for p in people[:8]),
            "mgr_states": ",".join(states), "absentee": int(absentee),
        })
    stats = {"llcs": len(want), "matched": len(name_to_files), "ambiguous": len(ambiguous),
             "with_people": with_people, "absentee": absentee_n}
    return out, stats


def read_parcels(path: Path) -> list[dict]:
    return [json.loads(s)["properties"] for s in path.open()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parcels", type=Path, required=True)
    ap.add_argument("--registry", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("build/llc_owners.csv"))
    a = ap.parse_args()
    parcels = [p for p in read_parcels(a.parcels) if p["on_map"]]
    rows, st = join_registry(farmland_llcs(parcels), a.registry)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"farmland LLCs {st['llcs']}, in registry {st['matched']}, ambiguous {st['ambiguous']}, "
          f"traced to named people {st['with_people']}, absentee {st['absentee']} -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
