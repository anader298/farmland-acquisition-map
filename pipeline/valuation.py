#!/usr/bin/env python3
"""Three valuations per parcel, kept separate, and a flag where they disagree.

No fitted model and no blend. Every number is a median or weighted median of
recorded sales (Illinois PTAX-203 declarations), shown with its count.

  comps   weighted median $/ac of the 20 nearest arm's-length farm sales in
          the same or a bordering county, sold in the last 5 years. Each
          comp's weight is
              1/(1 + miles/2) x 1/(1 + |soil index diff|/12) x 0.5^(years old/2)
          capped so no one sale carries more than a third of the weight.
          Each comp's $/ac is carried to the valuation year by the ratio of
          the district's median $/soil-point that year to its sale year.
  soil    $ per soil-index point x the parcel's soil index. The rate is the
          county's median $/point in the latest year with 10+ sales, else
          the district's.
  rent    the parcel's cash-rent estimate / the cap rate the local sales imply
          (median rent / price over the comps pool).

Status:
  thin      fewer than 3 comps, or fewer than two of the three values
  agree     the three (or two) values sit within 20% of each other: range is
            the comps estimate +/- the hold-out median miss
  disagree  no range; the parcel is flagged for a closer look

Hold-out: every pool sale is predicted from the other sales with the same
rule. The median miss is printed and stored, so the range on each parcel
says how wrong the method has been, not how wrong it hopes to be.
"""

from __future__ import annotations

import math
import statistics as st
from collections import defaultdict
from datetime import date

K_MAX, K_MIN, K_THIN = 20, 5, 3
PI_BAND, AC_LO, AC_HI = 10.0, 0.5, 3.0
PPI_MIN_N = 10
AGREE = 1.20
D0_MI, PI0, HALF_YRS, W_CAP = 2.0, 12.0, 2.0, 0.33
PPA_MIN, PPA_MAX = 300, 60_000
WINDOW_YEARS = 5
EXCLUDE_FLAGS = {"related", "court", "foreclosure", "short_sale"}

# Rent model: the county's published cash rent, moved by soil quality only
# relative to the county's own mean, at a within-district slope; clamped.
RENT_SLOPE = 3.21          # $/ac per soil-index point
RENT_CLAMP = 0.40


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[1] + b[1]) / 2)
    dx = (a[0] - b[0]) * 69.172 * math.cos(lat)
    dy = (a[1] - b[1]) * 68.97
    return math.hypot(dx, dy)


def rent_estimate(pi, county_rent, county_mean_pi):
    if pi is None or county_rent is None or county_mean_pi is None:
        return None
    adj = RENT_SLOPE * (pi - county_mean_pi)
    adj = max(-RENT_CLAMP * county_rent, min(RENT_CLAMP * county_rent, adj))
    return county_rent + adj


def wmedian(vals: list[float], ws: list[float]) -> float | None:
    pairs = sorted((v, w) for v, w in zip(vals, ws) if w > 0)
    tot = sum(w for _, w in pairs)
    if not tot:
        return None
    c = 0.0
    for v, w in pairs:
        c += w
        if c >= tot / 2:
            return v
    return pairs[-1][0]


def cap_weights(ws: list[float]) -> list[float]:
    for _ in range(8):
        tot = sum(ws)
        ws = [min(w, W_CAP * tot) for w in ws]
    return ws


class Valuer:
    def __init__(self, sales: list[dict], parcels: dict, adjacency: dict[str, set], val_date: date):
        """sales: dicts with key (county, pin), date (date), price, acres, flags (set).
        parcels: (county, pin) -> dict with pi, xy (lon, lat), rent, county."""
        self.adj = adjacency
        self.val_date = val_date
        lo = date(val_date.year - WINDOW_YEARS, val_date.month, min(val_date.day, 28))
        self.pool = []
        for s in sales:
            p = parcels.get(s["key"])
            if not p or p.get("pi") is None or s["flags"] & EXCLUDE_FLAGS or not s["acres"]:
                continue
            ppa = s["price"] / s["acres"]
            if not (PPA_MIN <= ppa <= PPA_MAX) or not (lo <= s["date"] <= val_date):
                continue
            self.pool.append({**s, "ppa": ppa, "pi": p["pi"], "xy": p["xy"], "county": s["key"][0],
                              "rent": p.get("rent")})
        # $/soil-point by year, district-wide (the index) and by county (the soil method)
        by_year, by_cy = defaultdict(list), defaultdict(list)
        for s in self.pool:
            by_year[s["date"].year].append(s["ppa"] / s["pi"])
            by_cy[(s["county"], s["date"].year)].append(s["ppa"] / s["pi"])
        self.index = {y: st.median(v) for y, v in by_year.items()}
        self.ppi_cy = {k: (st.median(v), len(v)) for k, v in by_cy.items()}
        caps = [s["rent"] / s["ppa"] for s in self.pool if s.get("rent")]
        self.cap = st.median(caps) if caps else None
        self.miss = None

    # ------------------------------------------------------------------ comps
    def _carry(self, ppa: float, year: int) -> float:
        i0, i1 = self.index.get(year), self.index.get(self.val_date.year) or self.index.get(max(self.index))
        return ppa * (i1 / i0) if i0 and i1 else ppa

    def comps(self, key, pi, acres, xy, exclude=None):
        county = key[0]
        ok_cty = {county} | self.adj.get(county, set())
        cand = [s for s in self.pool if s["county"] in ok_cty and s["key"] != key and s is not exclude]
        levels = [(lambda s: abs(s["pi"] - pi) <= PI_BAND and AC_LO <= s["acres"] / acres <= AC_HI, None),
                  (lambda s: AC_LO <= s["acres"] / acres <= AC_HI, "pi"),
                  (lambda s: True, "pi+acres")]
        for test, relaxed in levels:
            sel = [s for s in cand if test(s)]
            if len(sel) >= K_MIN or relaxed == "pi+acres":
                sel = sorted(sel, key=lambda s: miles(xy, s["xy"]))[:K_MAX]
                return sel, relaxed
        return [], "pi+acres"

    def comps_value(self, key, pi, acres, xy, exclude=None):
        sel, relaxed = self.comps(key, pi, acres, xy, exclude)
        if len(sel) < K_THIN:
            return None, len(sel), relaxed
        vals, ws = [], []
        for s in sel:
            age = abs((self.val_date - s["date"]).days) / 365.0
            w = 1 / (1 + miles(xy, s["xy"]) / D0_MI) * 1 / (1 + abs(s["pi"] - pi) / PI0) * 0.5 ** (age / HALF_YRS)
            vals.append(self._carry(s["ppa"], s["date"].year))
            ws.append(w)
        return wmedian(vals, cap_weights(ws)), len(sel), relaxed

    # ------------------------------------------------------------------ soil, rent
    def soil_value(self, county, pi):
        if pi is None:
            return None
        for y in range(self.val_date.year, self.val_date.year - WINDOW_YEARS - 1, -1):
            m = self.ppi_cy.get((county, y))
            if m and m[1] >= PPI_MIN_N:
                return m[0] * pi
            dist = [s["ppa"] / s["pi"] for s in self.pool if s["date"].year == y]
            if len(dist) >= PPI_MIN_N:
                return st.median(dist) * pi
        return None

    def rent_value(self, rent):
        return rent / self.cap if rent and self.cap else None

    # ------------------------------------------------------------------ verdict
    def value(self, key, pi, acres, xy, rent) -> dict:
        if pi is None or not acres:
            return {"status": "thin", "comps_n": 0}
        c, n, relaxed = self.comps_value(key, pi, acres, xy)
        s, r = self.soil_value(key[0], pi), self.rent_value(rent)
        vals = [v for v in (c, s, r) if v]
        out = {"comps": c, "soil": s, "rent_cap": r, "comps_n": n, "relaxed": relaxed}
        if c is None or len(vals) < 2:
            return {**out, "status": "thin"}
        if max(vals) / min(vals) <= AGREE:
            m = self.miss or 0.2
            return {**out, "status": "agree", "lo": c * (1 - m), "hi": c * (1 + m)}
        return {**out, "status": "disagree"}

    def holdout(self) -> dict:
        """Predict every pool sale from the others; median absolute miss."""
        errs = []
        for s in self.pool:
            est, n, _ = self.comps_value(s["key"], s["pi"], s["acres"], s["xy"], exclude=s)
            if est:
                errs.append(abs(est - self._carry(s["ppa"], s["date"].year)) / self._carry(s["ppa"], s["date"].year))
        if not errs:
            return {"n": 0}
        self.miss = st.median(errs)
        return {"n": len(errs), "median_miss": self.miss,
                "within_15pct": sum(e <= 0.15 for e in errs) / len(errs)}
