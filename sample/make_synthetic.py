#!/usr/bin/env python3
"""Generate the SYNTHETIC sample the pipeline and map run on.

Nothing here is real. Counties, parcels, owners, LLCs, registry people,
addresses, sales and soil values are generated from a fixed seed. Names are
invented; addresses use the fictional town SAMPLETON and ZIP codes 00001 to
00009. The shapes of the files match the real public sources so the same
code runs on both:

  raw/<County>.geojsonl    county parcel layers, each county with its own
                           field names (as ArcGIS services publish them)
  manifest.json            per-county field mapping and published row count
  registry/llcallnam.txt   fixed-width state LLC registry, names
  registry/llcallmgr.txt   fixed-width state LLC registry, managers/members
  sales.csv                recorded farm sales with transfer flags
  overlays.csv             per-parcel soil index, crop history, flood, wetland
                           and road values (the output of the spatial joins)
  county_rent.csv          county cash rent
  elevators.csv            grain elevator points

Usage: python sample/make_synthetic.py [--seed 7]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent

LON0, LAT0 = -89.20, 38.25          # south-west corner of the synthetic grid
MI_LON, MI_LAT = 0.01845, 0.01449   # degrees per mile at this latitude
COLS_PER_COUNTY, ROWS = 8, 10       # sections (1 sq mi) per county
COUNTIES = ["Example", "Sample", "Demo"]
NEIGHBORS = {"Example": ["Sample"], "Sample": ["Example", "Demo"], "Demo": ["Sample"]}
CASH_RENT = {"Example": 248, "Sample": 262, "Demo": 236}

SURNAMES = ["QUILLBY", "FENWORTH", "MARROWAY", "TESKEVAN", "BRANNOCK", "ALDERWICK", "CORVELLE",
            "DUNMERE", "HOLLISTRAD", "KESTWICK", "LORRIMAN", "MAPLEDOWN", "NORVATH", "OSTERLY",
            "PELLINGHAM", "RADLEIGH", "SOMERHAUS", "THORNEBY", "UPWELLER", "VANTREASE", "WEXBRIDGE",
            "YARDLEY-SMOTE", "ZELLINGER", "ASHVALE", "BRIXHOLM", "CALDERWEN", "DRAVENHOLT",
            "EMBERTON", "FAIRWOLD", "GRISTAN", "HAVERLOCK", "INGLEMOOR", "JESSOP-VANE", "KORVANEK",
            "LINDQUART", "MOSSGROVE", "NETHERLY", "ORMSKELL", "PRAEGER-LUND", "QUENBY"]
FIRST = ["JOHN", "MARY", "ROBERT", "LINDA", "JAMES", "SUSAN", "DAVID", "KAREN", "THOMAS", "NANCY",
         "PAUL", "CAROL", "MARK", "JANET", "STEVEN", "DIANE", "GARY", "RUTH", "DENNIS", "JOAN"]
LLC_A = ["PRAIRIE", "CREEK", "OAK", "MEADOW", "RIDGE", "WILLOW", "CEDAR", "HILLTOP", "TIMBER", "BLACKSOIL"]
LLC_B = ["FARMS", "LAND", "AG", "ACRES", "HOLDINGS", "GRAIN"]
OUT_OF_STATE = [("ANYTOWN", "IA"), ("ANYTOWN", "TX"), ("ANYTOWN", "FL"), ("ANYTOWN", "IN"), ("ANYTOWN", "CO")]

# Each county publishes its own field names; the manifest maps them.
FIELDS = {
    "Example": {"pin_field": "PIN", "owner_field": "Owner", "acres_field": "CalcAcres",
                "class_field": "PropClass", "mail_addr_field": "MailAddr", "mail_city_field": "MailCity",
                "mail_state_field": "MailState", "mail_zip_field": "MailZip"},
    "Sample": {"pin_field": "PARCEL_ID", "owner_field": "OWNER_NAME", "acres_field": "ACRES",
               "class_field": "CLASS", "mail_addr_field": "MAIL_ADDR", "mail_city_field": "MAIL_CITY",
               "mail_state_field": "MAIL_ST", "mail_zip_field": "MAIL_ZIP"},
    "Demo": {"pin_field": "parcel_no", "owner_concat": [["own_last", "own_first", "own_mid"], ["own_entity"]],
             "acres_field": "gis_acres", "class_field": "tax_class",
             "farm_class_values": ["FARMLAND", "AG LAND"], "mail_addr_field": "mail1",
             "mail_city_field": "mail_city", "mail_state_field": "mail_state", "mail_zip_field": "mail_zip"},
}


def rect(x0, y0, x1, y1):
    """Rectangle in miles from the grid origin -> GeoJSON polygon in lon/lat."""
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [[[round(LON0 + x * MI_LON, 6), round(LAT0 + y * MI_LAT, 6)]
                                                 for x, y in pts]]}


def river_y(x: float) -> float:
    return 3.6 + 1.6 * math.sin(x / 3.1) + 0.6 * math.sin(x / 1.3)


def parcels_grid(rng: random.Random) -> list[dict]:
    out = []
    for i in range(COLS_PER_COUNTY * len(COUNTIES)):
        for j in range(ROWS):
            county = COUNTIES[i // COLS_PER_COUNTY]
            sec = (j * COLS_PER_COUNTY + i % COLS_PER_COUNTY) + 1
            pieces = []
            if rng.random() < 0.06:
                pieces.append((i, j, i + 1, j + 1))
            else:
                for qx, qy in ((0, 0.5), (0.5, 0.5), (0, 0), (0.5, 0)):
                    x0, y0 = i + qx, j + qy
                    u = rng.random()
                    if u < 0.5:
                        pieces.append((x0, y0, x0 + 0.5, y0 + 0.5))
                    elif u < 0.75:
                        pieces += [(x0, y0 + 0.25, x0 + 0.5, y0 + 0.5), (x0, y0, x0 + 0.5, y0 + 0.25)]
                    else:
                        for ax, ay in ((0, 0.25), (0.25, 0.25), (0, 0), (0.25, 0)):
                            pieces.append((x0 + ax, y0 + ay, x0 + ax + 0.25, y0 + ay + 0.25))
            for k, (x0, y0, x1, y1) in enumerate(pieces):
                kind = "farm"
                # carve a homesite off some 40s: a strip on the road side
                if (x1 - x0) == 0.25 and (y1 - y0) == 0.25 and rng.random() < 0.18:
                    h = 0.25 * (rng.choice([3, 6, 8]) / 40)
                    out.append({"county": county, "sec": sec, "k": f"{k}H", "box": (x0, y0, x1, y0 + h),
                                "kind": "homesite"})
                    y0 += h
                out.append({"county": county, "sec": sec, "k": str(k), "box": (x0, y0, x1, y1), "kind": kind})
    return out


def make_owner(kind: str, rng: random.Random, used: set) -> dict:
    sur = rng.choice(SURNAMES)
    first, mid = rng.choice(FIRST), rng.choice("ABCDEFGHJKLMRSTW")
    home = (f"{rng.randint(100, 9900)} EXAMPLE RD", "SAMPLETON", "IL", f"0000{rng.randint(1, 5)}")
    away = (f"{rng.randint(100, 9900)} SAMPLE AVE", *rng.choice(OUT_OF_STATE), f"0000{rng.randint(6, 9)}")
    mail = away if rng.random() < 0.12 else home
    if kind == "person":
        canon = f"{sur} {first} {mid}"
        variants = [canon, f"{first} {mid} {sur}", f"{canon} ETAL", f"{sur} {first} {mid} & {rng.choice(FIRST)}"]
        if rng.random() < 0.3:          # a typo the fuzzy pass should catch
            variants.append(f"{sur[:3]}{sur[4:]} {first} {mid}" if len(sur) > 5 else canon)
    elif kind == "trust":
        if rng.random() < 0.25:
            n = rng.randint(1000, 1999)
            canon = f"FIRST EXAMPLE BANK TR #{n}"
            variants = [canon, f"FIRST EXAMPLE BANK TRUST NO {n}"]
        else:
            dt = f"{rng.randint(1, 12)}/{rng.randint(1, 28)}/{rng.randint(1985, 2020)}"
            canon = f"{sur} {first} {mid} REVOCABLE TRUST"
            variants = [canon, f"{sur} {first} {mid} TR", f"{sur} {first} {mid} REV TRUST DTD {dt}",
                        f"{sur} {first} {mid} TTEE"]
    elif kind == "entity":
        canon = f"EXAMPLE {rng.choice(LLC_A)} {rng.choice(LLC_B)} LLC"
        k = 2
        while canon in used:
            canon = f"EXAMPLE {rng.choice(LLC_A)} {rng.choice(LLC_B)} {k} LLC"
            k += 1
        variants = [canon, canon.replace(" LLC", " L.L.C."), f"{canon} C/O SAMPLE FARM MGMT"]
        if rng.random() < 0.25:
            canon = canon.replace(" LLC", " INC").replace("EXAMPLE", "DEMO")
            variants = [canon]
    elif kind == "estate":
        canon = f"{sur} {first} {mid} ESTATE"
        variants = [canon, f"{sur} {first} HEIRS"]
    else:
        canon = rng.choice(["EXAMPLE TOWNSHIP", "ILLINOIS DEPT OF NATURAL RESOURCES",
                            "SAMPLE PARK DISTRICT", "EXAMPLE DRAINAGE DISTRICT"])
        variants = [canon]
        mail = ("1 PUBLIC SQ", "SAMPLETON", "IL", "00001")
    used.add(canon)
    return {"kind": kind, "canon": canon, "variants": variants, "mail": mail, "surname": sur}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    P = parcels_grid(rng)
    kinds = ["person"] * 50 + ["trust"] * 26 + ["entity"] * 17 + ["estate"] * 4 + ["government"] * 3
    owners, used = [], set()
    i = 0
    while i < len(P):
        if owners and rng.random() < 0.35:
            pool = [o for o in owners if o["kind"] in ("entity", "trust", "person")]
            o = rng.choice(pool)
        else:
            o = make_owner(rng.choice(kinds), rng, used)
            owners.append(o)
        run = min(len(P) - i, max(1, int(rng.expovariate(1 / 2.6))))
        for p in P[i:i + run]:
            p["owner"] = o
        i += run

    # family links: some trusts share a person's surname and mailing address
    persons = [o for o in owners if o["kind"] == "person"]
    for o in owners:
        if o["kind"] == "trust" and "BANK" not in o["canon"] and rng.random() < 0.5:
            m = rng.choice(persons)
            o["mail"], o["surname"] = m["mail"], m["surname"]
            first = o["canon"].split()[1:3]
            o["canon"] = f"{m['surname']} {' '.join(first)} REVOCABLE TRUST"
            o["variants"] = [o["canon"], f"{m['surname']} {' '.join(first)} TR"]

    # per-parcel attributes
    woods_seed = [(rng.uniform(0, 24), rng.uniform(0, 10), rng.uniform(0.5, 1.4)) for _ in range(9)]
    elevators = [(3.5, 8.0), (13.5, 2.5)]
    raw = {c: [] for c in COUNTIES}
    overlays, keyed = [], []
    for n, p in enumerate(P):
        x0, y0, x1, y1 = p["box"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        acres = (x1 - x0) * (y1 - y0) * 640
        d_river = abs(cy - river_y(cx))
        woods = any(math.hypot(cx - wx, cy - wy) < wr for wx, wy, wr in woods_seed)
        pi = 127 + 11 * math.sin(cx / 2.3) * math.cos(cy / 1.9) + rng.gauss(0, 4) - (6 if woods else 0)
        pi = max(86, min(147, pi))
        till = rng.uniform(0.12, 0.45) if woods else (rng.uniform(0.55, 0.85) if d_river < 0.5 else
                                                       rng.uniform(0.86, 0.995))
        sat = min(1.0, till + rng.uniform(0.0, 0.08))
        flood = None if p["county"] == "Demo" else (
            rng.uniform(0.45, 1.0) if d_river < 0.35 else rng.uniform(0.04, 0.3) if d_river < 0.7 else
            (rng.uniform(0, 0.02) if rng.random() < 0.1 else 0.0))
        wet = rng.uniform(0.05, 0.4) if d_river < 0.4 else (rng.uniform(0.0, 0.08) if rng.random() < 0.08 else 0.0)
        road = 0 if rng.random() < 0.04 else 1
        o = p["owner"]
        name = o["variants"][0] if rng.random() < 0.7 else rng.choice(o["variants"])
        c = p["county"]
        code = {"Example": 1, "Sample": 2, "Demo": 3}[c]
        pin = f"{code:02d}-{p['sec']:02d}-{p['k'].zfill(2)}-{n:04d}"
        if p["kind"] == "homesite":
            cls = {"Example": "0040", "Sample": 40, "Demo": "RESIDENTIAL"}[c]
        elif o["kind"] == "government":
            cls = {"Example": "0090", "Sample": 90, "Demo": "EXEMPT"}[c]
        elif woods and rng.random() < 0.5:
            cls = {"Example": "0029", "Sample": None, "Demo": "AG LAND"}[c]
        else:
            cls = {"Example": "0021", "Sample": 21 if rng.random() < 0.8 else None, "Demo": "FARMLAND"}[c]
        f = FIELDS[c]
        props = {f["pin_field"]: pin, f["class_field"]: cls}
        if c == "Demo":
            if o["kind"] == "person":
                parts = name.split()
                if parts[0] == o["surname"].split()[0] and len(parts) >= 3:
                    props.update(own_last=parts[0], own_first=parts[1], own_mid=" ".join(parts[2:]))
                else:
                    props["own_entity"] = name
            else:
                props["own_entity"] = name
            props["FarmAcres"] = round(acres * till, 2) if cls in ("FARMLAND", "AG LAND") else 0
        else:
            props[f["owner_field"]] = name
        props[f["acres_field"]] = round(acres * rng.uniform(0.985, 1.015), 2)
        props[f["mail_addr_field"]], props[f["mail_city_field"]] = o["mail"][0], o["mail"][1]
        props[f["mail_state_field"]], props[f["mail_zip_field"]] = o["mail"][2], o["mail"][3]
        props["OBJECTID"] = len(raw[c]) + 1
        raw[c].append({"type": "Feature", "geometry": rect(*p["box"]), "properties": props})
        overlays.append({"county": c, "pin": pin, "pi": round(pi, 1), "till_share": round(till, 3),
                         "sat_share": round(sat, 3), "flood_share": "" if flood is None else round(flood, 3),
                         "wetland_share": round(wet, 3), "road": road})
        keyed.append((c, pin, acres, pi, p["kind"], o, cx, cy))

    (HERE / "raw").mkdir(exist_ok=True)
    manifest = []
    for c in COUNTIES:
        with (HERE / "raw" / f"{c}.geojsonl").open("w") as fh:
            for ft in raw[c]:
                fh.write(json.dumps(ft, separators=(",", ":")) + "\n")
        manifest.append({
            "county": c, "state": "Illinois", "synthetic": True,
            "rest_url": f"https://gis.{c.lower()}-county.example/arcgis/rest/services/Parcels/MapServer",
            "layer_id": 0, "feature_count": len(raw[c]), "free": True,
            "license": "synthetic sample", "neighbors": NEIGHBORS[c], **FIELDS[c]})
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=1))

    with (HERE / "overlays.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(overlays[0]))
        w.writeheader()
        w.writerows(overlays)

    # ---------------------------------------------------------------- registry
    reg = HERE / "registry"
    reg.mkdir(exist_ok=True)
    llcs = sorted({o["canon"] for o in owners if o["kind"] == "entity" and o["canon"].endswith(" LLC")})
    nam, mgr = ["RUN DATE 2026-11-01"], ["RUN DATE 2026-11-01"]
    fno = 10_000_000
    for llc in llcs:
        u = rng.random()
        if u < 0.12:
            continue                                  # not in the state registry (foreign LLC)
        fno += rng.randint(3, 40)
        files = [f"{fno:08d}"]
        if u > 0.94:                                  # same name, two file numbers -> ambiguous
            fno += 1
            files.append(f"{fno:08d}")
        for fn in files:
            nam.append(f"{fn}{llc:<60}")
            absentee = rng.random() < 0.25
            for _ in range(rng.choice([1, 1, 2, 2, 3])):
                if rng.random() < 0.35 and persons:
                    m = rng.choice(persons)
                    person = m["canon"]
                    addr = m["mail"]
                else:
                    person = f"{rng.choice(SURNAMES)} {rng.choice(FIRST)} {rng.choice('ABCDEFGHJKLM')}"
                    addr = (f"{rng.randint(100, 9900)} SAMPLE AVE", *rng.choice(OUT_OF_STATE), "00007") \
                        if absentee else (f"{rng.randint(100, 9900)} EXAMPLE RD", "SAMPLETON", "IL", "00003")
                if absentee:
                    addr = (addr[0], *rng.choice(OUT_OF_STATE), "00008")
                mgr.append(f"{fn}{person:<60}{addr[0]:<45}{addr[1]:<30}{addr[2]:<2}{addr[3]:<9}".rstrip())
    for k in range(40):                               # unrelated registry rows
        fno += 7
        nam.append(f"{fno:08d}{f'UNRELATED SAMPLE VENTURE {k} LLC':<60}".rstrip())
    (reg / "llcallnam.txt").write_text("\n".join(nam) + "\n")
    (reg / "llcallmgr.txt").write_text("\n".join(mgr) + "\n")

    # ---------------------------------------------------------------- sales
    sales = []
    start = date(2021, 11, 1)
    for c, pin, acres, pi, kind, o, cx, cy in keyed:
        if kind == "homesite" or acres < 10 or o["kind"] == "government" or rng.random() > 0.17:
            continue
        d = start + timedelta(days=rng.randint(0, 5 * 365 - 1))
        yrs = (d - start).days / 365
        ppi = 58.0 * 1.045 ** yrs
        flags = []
        for flag, pr in (("auction", 0.12), ("estate", 0.12), ("related", 0.06), ("court", 0.01),
                         ("adjacent_buyer", 0.06)):
            if rng.random() < pr:
                flags.append(flag)
        mult = math.exp(rng.gauss(0, 0.17)) * (1.12 if "auction" in flags else 1.0) * \
            (0.75 if "related" in flags else 1.0) * \
            (1 + 0.24 * math.sin(cx / 1.7 + cy / 2.9))      # a location premium soil and rent do not see
        price = round(ppi * pi * acres * mult, -2)
        sales.append({"county": c, "pin": pin, "date": d.isoformat(), "price": int(price),
                      "acres": round(acres, 2), "flags": ";".join(flags)})
    sales.sort(key=lambda s: s["date"])
    with (HERE / "sales.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(sales[0]))
        w.writeheader()
        w.writerows(sales)

    with (HERE / "county_rent.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["county", "cash_rent"])
        for c in COUNTIES:
            w.writerow([c, CASH_RENT[c]])
    with (HERE / "elevators.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "lon", "lat"])
        for k, (x, y) in enumerate(elevators):
            w.writerow([f"Sample Grain Elevator {k + 1}", round(LON0 + x * MI_LON, 5), round(LAT0 + y * MI_LAT, 5)])

    print(f"synthetic sample: {len(P):,} parcels in {len(COUNTIES)} counties, {len(owners)} owners, "
          f"{len(llcs)} LLCs, {len(sales)} sales -> {HERE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
