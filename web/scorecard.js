/* Scorecard for the parcel panel.
 *
 * Plain facts, each with a green / amber / red dot. Not a score: nothing is
 * added up. Every threshold is the user's to change (Scorecard.setThresholds);
 * the choice is kept in localStorage['fl.scorecard.v1'] = {on, t: {key: {g, a}}}.
 * Unknown values are grey "no data", never red.
 *
 * Classic script (no modules): defines window.Scorecard.
 *   rows(detail, props, county) -> [{key, label, value, color: "g"|"a"|"r"|"x", note}]
 *   thresholds() -> {key: {g, a}}      setThresholds(t)      reset()
 *   enabled() -> bool                  setEnabled(b)
 *   METRICS                            (definitions, for the settings sheet)
 *   validate(t) -> {key: "message"}    (which pairs are out of order)
 */
(function () {
  "use strict";
  const KEY = "fl.scorecard.v1";

  // dir: "hi" = higher is better (green when v >= g, amber when v >= a)
  //      "lo" = lower is better  (green when v <= g, amber when v <= a)
  //      "bool" / "info" = fixed rule, no thresholds to edit
  // pct: stored as a 0-1 share, shown and edited as a percentage
  const METRICS = [
    { key: "pi", label: "Soil PI", dir: "hi", g: 117, a: 100, unit: "",
      note: "Bulletin 811 classes: Excellent 133+, Good 117-132, Average 100-116, Fair under 100" },
    { key: "pi_vs_county", label: "PI vs county mean", dir: "hi", g: 5, a: -5, unit: "points", signed: true },
    { key: "tillable", label: "Tillable share", dir: "hi", g: 0.90, a: 0.75, pct: true,
      note: "satellite crop map, 12-year mean" },
    { key: "flood", label: "Floodplain share", dir: "lo", g: 0.05, a: 0.25, pct: true, note: "FEMA 100-year" },
    { key: "wetland", label: "Wetland share", dir: "lo", g: 0.05, a: 0.20, pct: true, note: "NWI map, all types" },
    { key: "drain_poor", label: "Poorly drained share", dir: "lo", g: 0.25, a: 0.60, pct: true },
    { key: "tiled", label: "Tile-drained share where tile pays", dir: "hi", g: 0.50, a: 0.20, pct: true,
      note: "only rated where the soil says tile pays" },
    { key: "road", label: "Road access", dir: "bool", note: "landlocked = red" },
    { key: "elevator", label: "Grain elevator", dir: "lo", g: 10, a: 20, unit: "mi" },
    { key: "size", label: "Parcel size", dir: "hi", g: 40, a: 20, unit: "ac" },
    { key: "tax_share", label: "Tax share of rent", dir: "lo", g: 0.15, a: 0.25, pct: true, note: "estimate" },
    { key: "rent_yield", label: "Rent yield at the guess", dir: "hi", g: 0.030, a: 0.025, pct: true, digits: 1,
      note: "rent / middle of the good-guess price; only when sources agree" },
    { key: "comps", label: "Comps evidence", dir: "hi", g: 5, a: 3, unit: "sales" },
    { key: "weather", label: "Weather loss vs state", dir: "lo", g: 25, a: 40, unit: "$/insured ac/yr",
      note: "county insurance record; correlation only" },
    { key: "income", label: "Income extras", dir: "info", note: "informational; grey when none" },
  ];
  const BY = Object.fromEntries(METRICS.map((m) => [m.key, m]));
  const DEFAULTS = Object.fromEntries(METRICS.filter((m) => m.g != null).map((m) => [m.key, { g: m.g, a: m.a }]));

  let mem = null;   // used when localStorage is blocked (Safari private mode)
  function load() {
    let s = null;
    try { s = JSON.parse(localStorage.getItem(KEY) || "null"); } catch { s = null; }
    if (!s && mem) s = mem;
    if (!s || typeof s !== "object") s = {};
    const t = {};
    for (const [k, d] of Object.entries(DEFAULTS)) {
      const u = s.t && s.t[k];
      const g = u && Number.isFinite(+u.g) && u.g !== null && u.g !== "" ? +u.g : d.g;
      const a = u && Number.isFinite(+u.a) && u.a !== null && u.a !== "" ? +u.a : d.a;
      t[k] = { g, a };
    }
    return { on: s.on !== false, t };
  }
  function save(st) {
    const out = { on: st.on, t: st.t };
    mem = out;
    try { localStorage.setItem(KEY, JSON.stringify(out)); } catch { /* memory copy only */ }
  }

  /* Which threshold pairs are out of order (green must be the better side of amber). */
  function validate(t) {
    const bad = {};
    for (const [k, v] of Object.entries(t || {})) {
      const m = BY[k];
      if (!m || m.g == null) continue;
      if (!Number.isFinite(v.g) || !Number.isFinite(v.a)) { bad[k] = "numbers only"; continue; }
      if (m.dir === "hi" && v.g < v.a) bad[k] = "green must be at least the amber limit";
      if (m.dir === "lo" && v.g > v.a) bad[k] = "green must be at most the amber limit";
    }
    return bad;
  }

  function grade(key, v, t) {
    const m = BY[key];
    if (v == null || Number.isNaN(v)) return "x";
    const th = t[key];
    if (m.dir === "hi") return v >= th.g ? "g" : v >= th.a ? "a" : "r";
    if (m.dir === "lo") return v <= th.g ? "g" : v <= th.a ? "a" : "r";
    return "x";
  }

  const n0 = (v, d = 0) => (+v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const pc = (v, d = 0) => n0(100 * v, d) + "%";
  const num = (v) => (v == null || v === "" || !Number.isFinite(+v) ? null : +v);

  /* detail = the shard record, props = the tile properties, county = the
   * county lookup object (pi, w_loss ...). Every input may be missing. */
  function rows(detail, props, county) {
    const d = detail || {}, p = props || {}, c = county || {};
    const t = load().t;
    const out = [];
    const add = (key, v, text, extra) => {
      const m = BY[key];
      const color = extra && extra.color ? extra.color : grade(key, v, t);
      out.push({ key, label: m.label, value: v == null ? "no data" : text,
        color: v == null && !(extra && extra.color) ? "x" : color, note: (extra && extra.note) || "" });
    };
    const pi = num(d.pi ?? p.pi);
    add("pi", pi, pi == null ? "" : n0(pi, 1));

    const cpi = num(c.pi);
    const dpi = pi != null && cpi != null ? pi - cpi : null;
    add("pi_vs_county", dpi, dpi == null ? "" : `${dpi >= 0 ? "+" : ""}${n0(dpi, 1)} (county ${n0(cpi, 1)})`);

    const acres = num(d.acres ?? p.acres);
    const cr = d.crop || {};
    const till = num(cr.tillable) != null && acres ? Math.min(1, num(cr.tillable) / acres) : null;
    add("tillable", till, till == null ? "" : `${pc(till)} (${n0(cr.tillable, 1)} of ${n0(acres, 1)} ac)`);

    const w = d.water || {};
    const sfha = w.no_flood_map ? null : num(w.sfha);
    add("flood", sfha, sfha == null ? "" : pc(sfha, sfha > 0 && sfha < 0.01 ? 1 : 0),
      w.no_flood_map ? { note: "no FEMA flood map for this county" } : null);

    const wet = num((d.wetland || {}).share ?? w.wetland);
    add("wetland", wet, wet == null ? "" : pc(wet));

    const dn = d.drain || {};
    const poor = num(dn.poor);
    add("drain_poor", poor, poor == null ? "" : pc(poor));

    const tiled = num(dn.tiled);
    if (dn.tile_band && dn.tile_band !== "pays")
      out.push({ key: "tiled", label: BY.tiled.label, value: `not rated (tile ${dn.tile_band})`, color: "x", note: "" });
    else add("tiled", dn.tile_band === "pays" ? tiled : null, tiled == null ? "" : pc(tiled));

    const ll = w.landlocked;
    if (ll === true || ll === false || ll === 1 || ll === 0)
      out.push({ key: "road", label: BY.road.label, value: ll ? "landlocked (no road frontage)" : "touches a road",
        color: ll ? "r" : "g", note: "" });
    else out.push({ key: "road", label: BY.road.label, value: "no data", color: "x", note: "" });

    const el = num(w.elevator_mi);
    add("elevator", el, el == null ? "" : `${n0(el, 1)} mi`);

    add("size", acres, acres == null ? "" : `${n0(acres, 1)} ac`);

    const tx = d.tax || {};
    const ts = num(tx.tax_share_rent);
    add("tax_share", ts, ts == null ? "" : pc(ts));

    const pr = d.price || {};
    const rent = num(d.rent);
    const lo = num(pr.guess_lo_ac), hi = num(pr.guess_hi_ac);
    if (pr.guess_status === "agree" && rent != null && lo != null && hi != null) {
      const mid = (lo + hi) / 2;
      const y = mid > 0 ? rent / mid : null;
      add("rent_yield", y, y == null ? "" : `${pc(y, 1)} ($${n0(rent)} rent / $${n0(mid)} mid)`);
    } else {
      out.push({ key: "rent_yield", label: BY.rent_yield.label,
        value: pr.guess_status === "disagree" ? "no guess (sources disagree)"
          : pr.guess_status === "thin" ? "no guess (too few sales)" : "no data", color: "x", note: "" });
    }

    const cn = num(pr.comps_n);
    add("comps", cn, cn == null ? "" : `${n0(cn)} sale${cn === 1 ? "" : "s"}`);

    const wl = num(c.w_loss);
    add("weather", wl, wl == null ? "" : `$${n0(wl, wl < 10 ? 2 : 0)}/ac/yr`);

    const sig = (d.income && d.income.signals) || [];
    out.push({ key: "income", label: BY.income.label, value: sig.length ? `${sig.length} signal${sig.length > 1 ? "s" : ""}` : "none found",
      color: sig.length ? "g" : "x", note: "" });
    return out;
  }

  window.Scorecard = {
    METRICS, DEFAULTS, KEY,
    rows, validate,
    thresholds() { return load().t; },
    setThresholds(t) {
      const st = load();
      const bad = validate(t);
      for (const [k, v] of Object.entries(t || {}))
        if (DEFAULTS[k] && !bad[k]) st.t[k] = { g: +v.g, a: +v.a };
      save(st);
      return bad;
    },
    reset() { const st = load(); st.t = JSON.parse(JSON.stringify(DEFAULTS)); save(st); },
    enabled() { return load().on; },
    setEnabled(b) { const st = load(); st.on = !!b; save(st); },
  };
})();
