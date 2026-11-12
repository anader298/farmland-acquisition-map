"""The default screen: eight tests chained over the cropland-and-pasture view.

Each test is a scorecard metric at its default threshold. Thresholds are the
buyer's to change; these are the defaults the map opens with. A parcel with
no data on a test passes it (a missing flood map is not a flood), and the
funnel reports how many passed that way.
"""

from __future__ import annotations

DEFAULTS = {
    "pi_min": 117,            # soil productivity index, "Good" or better
    "till_min": 0.90,         # tillable share, 12-year satellite crop history
    "flood_max": 0.05,        # FEMA 100-year floodplain share
    "wetland_max": 0.05,      # National Wetlands Inventory share
    "elevator_max_mi": 10.0,  # miles to the nearest grain elevator
    "acres_min": 40.0,
    "yield_min": 0.030,       # cash rent / middle of the agreed price range
}


def _ok(v, test) -> bool:
    return v is None or test(v)


def tests(t: dict = DEFAULTS):
    """(label, predicate) in chain order. p is a map parcel's properties."""
    return [
        ("Cropland and pasture (default view)", lambda p: bool(p.get("is_crop"))),
        (f"Soil index {t['pi_min']} or better", lambda p: _ok(p.get("pi"), lambda v: v >= t["pi_min"])),
        (f"Tillable {t['till_min']:.0%} or more", lambda p: _ok(p.get("till_share"), lambda v: v >= t["till_min"])),
        (f"Floodplain {t['flood_max']:.0%} or less (or no FEMA map), road frontage",
         lambda p: _ok(p.get("flood_share"), lambda v: v <= t["flood_max"]) and p.get("road") != 0),
        (f"Wetland {t['wetland_max']:.0%} or less", lambda p: _ok(p.get("wetland_share"), lambda v: v <= t["wetland_max"])),
        (f"Grain elevator within {t['elevator_max_mi']:.0f} miles",
         lambda p: _ok(p.get("elevator_mi"), lambda v: v <= t["elevator_max_mi"])),
        (f"{t['acres_min']:.0f} acres or more", lambda p: (p.get("gis_acres") or 0) >= t["acres_min"]),
        ("Three price methods agree within 20%", lambda p: p.get("val_status") == "agree"),
        (f"Rent yield {t['yield_min']:.1%} or more at that price",
         lambda p: p.get("rent_yield") is not None and p["rent_yield"] >= t["yield_min"]),
    ]


def run(parcels: list[dict], t: dict = DEFAULTS) -> tuple[list[dict], list[dict]]:
    """-> (funnel rows, shortlisted parcels)."""
    left = parcels
    funnel = []
    for label, pred in tests(t):
        before = len(left)
        left = [p for p in left if pred(p)]
        funnel.append({"test": label, "remaining": len(left), "cut": len(left) - before})
    funnel[0]["cut"] = None
    return funnel, left
