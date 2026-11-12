"""The one farm rule.

Every stage that needs to know "is this parcel farmland" imports this file,
so there is one definition and one place to change a threshold.

Signals, strongest first; `farm_basis` is the first that fires:

  assessor_crop   the county publishes cropland or pasture acres and they are > 0
  assessor_farm   farm acres > 0, or farm-land assessed value > 0, or a farm
                  property class (Illinois codes 11, 21, 27, 28, 29, or the
                  county's own text values listed in its manifest entry)
  satellite_crop  12-year USDA Cropland Data Layer mean share of row crop,
                  small grain and hay/pasture >= 0.50
  size_only       >= 20 GIS acres and nothing above fired
  None            nothing fired: not farmland

A firm non-farm assessor class (residential, commercial, industrial, railroad,
solar, exempt) overrides the weak signals (satellite, size). Assessor farm
evidence still wins over it.

`is_crop` (the default "cropland and pasture" view) = farm AND (the basis is
crop evidence OR the CDL tillable share >= 0.50).
`on_map` = farm AND >= 5 GIS acres AND has geometry.

Run this file to self-test:  python -m pipeline.farm_rule
"""

from __future__ import annotations

import math
import re

MIN_MAP_ACRES = 5.0
SIZE_ONLY_ACRES = 20.0
SAT_SHARE = 0.50
TILL_SHARE = 0.50

# Illinois property classes that are farm-assessed: 0011 farm homesite,
# 0021 farmland, 0027 farm, 0028 conservation stewardship, 0029 wooded
# acreage transition.
FARM_CODES = frozenset({"11", "21", "27", "28", "29"})
CROP_BASES = frozenset({"assessor_crop", "satellite_crop"})

FARM_ACRE_FIELDS = ("FarmAcres", "farm_acres", "TSC_Farm_Acreage", "farm_ac", "taxFarmAcres")

_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")
_CODE = re.compile(r"^\s*0*([0-9]+)")


def _missing(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def num(v) -> float | None:
    """'$ 2,236' / '1388.000' / 12 / None -> float or None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if _missing(v) else float(v)
    s = str(v).replace(",", "").replace("$", "").strip()
    if not s or not _NUM.fullmatch(s):
        return None
    return float(s)


def class_code(v) -> str | None:
    """'0021' / 21 / '0029 - Wooded Transition' -> '21' / '21' / '29'."""
    if v is None:
        return None
    m = _CODE.match(str(v).strip().strip('"'))
    if not m:
        return None
    return m.group(1).lstrip("0") or None


def _norm_text(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().upper()


def signals(props: dict, rule: dict | None) -> dict:
    """Assessor signals of one raw parcel under its county's manifest rule."""
    rule = rule or {}
    cf = rule.get("class_field")
    raw = props.get(cf) if cf else None
    if raw is not None and str(raw).strip() in ("", "None"):
        raw = None
    code = class_code(raw)
    farm_values = frozenset(_norm_text(x) for x in rule.get("farm_class_values") or ())
    farm_class = bool(raw is not None and (code in FARM_CODES or _norm_text(raw) in farm_values))
    fa = next((num(props.get(k)) for k in FARM_ACRE_FIELDS if num(props.get(k)) is not None), None)
    av = num(props.get(rule["farm_av_field"])) if rule.get("farm_av_field") else None
    crop = num(props.get(rule["crop_acres_field"])) if rule.get("crop_acres_field") else None
    past = num(props.get(rule["pasture_acres_field"])) if rule.get("pasture_acres_field") else None
    cult = None if crop is None and past is None else (crop or 0.0) + (past or 0.0)
    return {"class_raw": None if raw is None else str(raw).strip(), "class_code": code,
            "farm_class": farm_class, "farm_acres": fa, "farm_land_av": av, "cult_acres": cult}


# ------------------------------------------------------------- non-farm classes
NONFARM_OFF = frozenset({"residential", "commercial", "industrial", "railroad", "solar", "exempt"})
WEAK_BASES = frozenset({"satellite_crop", "size_only"})
_TEXT_CLASS = {"RESIDENTIAL": "residential", "COMMERCIAL": "commercial",
               "INDUSTRIAL": "industrial", "EXEMPT": "exempt", "AG DWELLING": "homesite"}


def nonfarm_reason(raw) -> str | None:
    """Why the assessor's class makes a parcel non-farm, or None.

    Uses the statewide Illinois numeric scheme. Counties that run their own
    scheme get a table of their own in production; the text values above
    cover the common textual one.
    """
    if _missing(raw):
        return None
    txt = _norm_text(raw)
    if txt in _TEXT_CLASS:
        return _TEXT_CLASS[txt]
    code = class_code(raw)
    if code is None:
        return None
    n = int(code)
    if n in (10, 15, 20, 21, 27, 28, 29):
        return None                     # farm land
    if n in (11, 12, 22):
        return "homesite"               # farm homesite and farm buildings
    if n == 26:
        return "solar"
    if 30 <= n <= 49:
        return "residential"
    if 50 <= n <= 69:
        return "commercial"
    if 70 <= n <= 89:
        return "industrial"
    if 90 <= n <= 99:
        return "exempt"
    return "unclear"


def classify(*, farm_class=False, class_raw=None, farm_acres=None, farm_land_av=None,
             cult_acres=None, sat_share=None, gis_acres=None) -> tuple[str | None, str]:
    """-> (farm_basis, evidence text). The single definition of the rule."""
    pos = lambda v: not _missing(v) and v > 0  # noqa: E731
    ev = []
    if farm_class and class_raw is not None:
        ev.append(f"class {str(class_raw)[:24]}")
    if pos(farm_acres):
        ev.append(f"farm acres {farm_acres:,.1f}")
    if pos(farm_land_av):
        ev.append(f"farm land AV ${farm_land_av:,.0f}")
    if pos(cult_acres):
        ev.append(f"crop+pasture acres {cult_acres:,.1f}")
    sat_ok = not _missing(sat_share)
    if sat_ok:
        ev.append(f"cdl {sat_share:.2f}")
    ga = None if _missing(gis_acres) else gis_acres

    if pos(cult_acres):
        basis = "assessor_crop"
    elif farm_class or pos(farm_acres) or pos(farm_land_av):
        basis = "assessor_farm"
    elif sat_ok and sat_share >= SAT_SHARE:
        basis = "satellite_crop"
    elif ga is not None and ga >= SIZE_ONLY_ACRES:
        basis = "size_only"
    else:
        basis = None
    if basis == "size_only":
        ev.append(f"{ga:,.1f} GIS ac")

    why = nonfarm_reason(class_raw)
    if basis in WEAK_BASES and why in NONFARM_OFF:
        return "non_farm", f"non-farm class ({why}): {str(class_raw)[:24]}"
    return basis, "; ".join(ev)


def is_farm(basis: str | None) -> bool:
    return basis is not None and basis != "non_farm"


def is_crop(basis: str | None, till_share=None) -> bool:
    if not is_farm(basis):
        return False
    if basis in CROP_BASES:
        return True
    return not _missing(till_share) and till_share >= TILL_SHARE


def on_map(basis: str | None, gis_acres, has_geometry: bool = True) -> bool:
    return is_farm(basis) and has_geometry and not _missing(gis_acres) and gis_acres >= MIN_MAP_ACRES


# ------------------------------------------------------------------ self-test
def _selftest() -> int:
    fails = 0

    def eq(name, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))

    for v, w in [("0021", "21"), (21, "21"), ("0029 - Wooded Transition", "29"),
                 ("0000", None), ("Ag Land", None), (None, None)]:
        eq(f"class_code({v!r})", class_code(v), w)
    for v, w in [("$ 2,236", 2236.0), ("1388.000", 1388.0), ("", None), ("abc", None), (True, None)]:
        eq(f"num({v!r})", num(v), w)
    eq("crop beats everything", classify(cult_acres=3, farm_class=True, class_raw="0021")[0], "assessor_crop")
    eq("farm class", classify(farm_class=True, class_raw="0028", gis_acres=6)[0], "assessor_farm")
    eq("farm AV only", classify(farm_land_av=100, gis_acres=7)[0], "assessor_farm")
    eq("satellite", classify(sat_share=0.61, gis_acres=7)[0], "satellite_crop")
    eq("satellite 0.4999", classify(sat_share=0.4999, gis_acres=7)[0], None)
    eq("size only", classify(sat_share=0.2, gis_acres=20.0)[0], "size_only")
    eq("residential beats satellite", classify(class_raw="0040", sat_share=0.8, gis_acres=30)[0], "non_farm")
    eq("farm AV beats residential", classify(class_raw="0040", farm_land_av=900, gis_acres=30)[0],
       "assessor_farm")
    eq("is_crop wooded farm class", is_crop("assessor_farm", 0.1), False)
    eq("is_crop tilled farm class", is_crop("assessor_farm", 0.8), True)
    eq("on_map 4.9 ac", on_map("assessor_farm", 4.9), False)
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAIL'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
