#!/usr/bin/env python3
"""Download free county parcel layers from public ArcGIS REST services.

Reads an endpoint manifest (one JSON object per county, see
sample/manifest.json for the shape) and pages through each county's
FeatureServer or MapServer layer, writing newline-delimited GeoJSON, one
feature per line, to <out>/<County>.geojsonl.

Every county's download is checked against the feature count the county's
own service reports. A short county is not marked done, so the next run
retries it instead of trusting a truncated file.

Resumable: a county whose .done marker exists is skipped.

Usage:
  python -m pipeline.fetch_parcels --manifest endpoints.json --out raw/
  python -m pipeline.fetch_parcels --manifest endpoints.json --out raw/ --county Wayne Clay
  python -m pipeline.fetch_parcels --manifest endpoints.json --list
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from pathlib import Path

PAGE_SLEEP = 0.4          # seconds between page requests
PAGE_SIZE_CAP = 2000      # ArcGIS maxRecordCount ceiling we respect
TIMEOUT = 90
USER_AGENT = "farmland-parcel-fetch/1.0 (public-records research)"


def make_session():
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    retry = Retry(total=5, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "POST"), raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def layer_ids_for(entry: dict) -> list[int]:
    """A county may expose one countywide layer or one layer per township."""
    ids = entry.get("layer_ids")
    if isinstance(ids, list) and ids:
        return [int(i) for i in ids]
    lid = entry.get("layer_id")
    return [lid] if isinstance(lid, int) else []


def layer_metadata(session, base: str, lid: int) -> dict:
    url = f"{base.rstrip('/')}/{lid}?f=json"
    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return {}


def pick_order_field(meta: dict, entry: dict) -> str | None:
    """ArcGIS offset paging is only stable with ORDER BY on a unique field.
    Several services use FID rather than OBJECTID and reject resultOffset
    without an explicit orderByFields."""
    if entry.get("order_by_field"):
        return entry["order_by_field"]
    if meta.get("objectIdField"):
        return meta["objectIdField"]
    for f in meta.get("fields") or []:
        if f.get("type") == "esriFieldTypeOID":
            return f.get("name")
    return None


def _tag(ft: dict, entry: dict, layer_name: str, lid: int) -> str:
    props = ft.get("properties") or {}
    props["_src_county"] = entry.get("county")
    props["_src_layer"] = layer_name
    props["_src_layer_id"] = lid
    ft["properties"] = props
    return json.dumps(ft, separators=(",", ":")) + "\n"


def fetch_layer(session, entry, base, lid, where, out) -> tuple[int, list[str]]:
    """Page through one layer by offset. Returns (count, warnings)."""
    warnings: list[str] = []
    meta = layer_metadata(session, base, lid)
    order_by = pick_order_field(meta, entry)
    page = max(1, min(int(meta.get("maxRecordCount") or entry.get("max_record_count") or 1000),
                      PAGE_SIZE_CAP))
    name = meta.get("name") or f"layer{lid}"
    offset = written = 0
    while True:
        params = {"where": where, "outFields": "*", "outSR": "4326", "f": "geojson",
                  "resultOffset": str(offset), "resultRecordCount": str(page),
                  "returnGeometry": "true"}
        if order_by:
            params["orderByFields"] = order_by
        url = f"{base.rstrip('/')}/{lid}/query?{urllib.parse.urlencode(params)}"
        try:
            r = session.get(url, timeout=TIMEOUT)
            payload = r.json() if r.ok else {"error": {"message": f"HTTP {r.status_code}"}}
        except Exception as exc:
            warnings.append(f"layer {lid} offset {offset}: {exc}")
            break
        if payload.get("error"):
            warnings.append(f"layer {lid} offset {offset}: {payload['error'].get('message')}")
            break
        feats = payload.get("features") or []
        for ft in feats:
            out.write(_tag(ft, entry, name, lid))
        written += len(feats)
        exceeded = bool(payload.get("exceededTransferLimit")) or bool(
            (payload.get("properties") or {}).get("exceededTransferLimit"))
        if not feats or (not exceeded and len(feats) < page):
            break
        offset += len(feats)
        sys.stderr.write(f"\r    {entry.get('county')} L{lid}: {written:,}")
        time.sleep(PAGE_SLEEP)
    sys.stderr.write("\n" if written else "")
    return written, warnings


def fetch_layer_by_ids(session, entry, base, lid, where, out) -> tuple[int, list[str]]:
    """Fetch a layer by object id instead of by offset.

    Offset paging can drop rows silently: it stalls on servers that ignore
    resultOffset, and a resume point stored as a count skips any row removed
    mid-run. Asking for the full id list first and then requesting those ids
    cannot skip anything: every id comes back or is visibly missing.
    """
    warnings: list[str] = []
    meta = layer_metadata(session, base, lid)
    oid = (pick_order_field(meta, entry) or "OBJECTID").rsplit(".", 1)[-1]
    name = meta.get("name") or f"layer{lid}"
    url = f"{base.rstrip('/')}/{lid}/query?" + urllib.parse.urlencode(
        {"where": where, "returnIdsOnly": "true", "f": "json"})
    try:
        ids = (session.get(url, timeout=TIMEOUT).json() or {}).get("objectIds") or []
    except Exception as exc:
        return 0, [f"layer {lid}: id list failed: {exc}"]
    if not ids:
        return 0, [f"layer {lid}: server returned no object ids"]

    # 250 ids per POST: long GET query strings get truncated by some servers
    # and come back as a partial result rather than an error.
    chunk = max(1, min(int(meta.get("maxRecordCount") or 1000), 250))
    written = 0
    q = f"{base.rstrip('/')}/{lid}/query"
    for i in range(0, len(ids), chunk):
        params = {"where": f"{oid} IN ({','.join(map(str, ids[i:i + chunk]))})",
                  "outFields": "*", "outSR": "4326", "f": "geojson", "returnGeometry": "true"}
        try:
            payload = session.post(q, data=params, timeout=TIMEOUT).json()
        except Exception as exc:
            warnings.append(f"layer {lid}: id batch at {i} failed: {exc}")
            continue
        if payload.get("error"):
            warnings.append(f"layer {lid}: {payload['error'].get('message')}")
            continue
        for ft in payload.get("features") or []:
            out.write(_tag(ft, entry, name, lid))
        written += len(payload.get("features") or [])
        time.sleep(PAGE_SLEEP)
    if written < len(ids):
        warnings.append(f"layer {lid}: {len(ids) - written} ids returned no feature")
    return written, warnings


def count_ok(written: int, expected) -> bool:
    """Within 2% (or 5 rows) of the county's published count."""
    if not isinstance(expected, int) or not expected:
        return True
    return abs(written - expected) <= max(5, expected * 0.02)


def run(args) -> int:
    entries = [e for e in json.loads(Path(args.manifest).read_text())
               if e.get("free") is not False and e.get("rest_url") and layer_ids_for(e)]
    if args.county:
        want = {c.lower() for c in args.county}
        entries = [e for e in entries if (e.get("county") or "").lower() in want]
    entries.sort(key=lambda e: e.get("county") or "")

    if args.list:
        for e in entries:
            print(f"{e['county']:<14} {e.get('feature_count') or '?':>9}  {e['rest_url']}")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    session = make_session()
    report = []
    for e in entries:
        county = e["county"]
        path = out_dir / f"{county.replace(' ', '_').replace('.', '')}.geojsonl"
        done = path.with_suffix(".done")
        if done.exists() and not args.force:
            print(f"= {county}: already downloaded")
            continue
        expected = e.get("feature_count")
        tmp = path.with_suffix(".partial")
        total, warns = 0, []
        with tmp.open("w") as fh:
            for lid in layer_ids_for(e):
                n, w = fetch_layer(session, e, e["rest_url"], lid, "1=1", fh)
                total += n
                warns += w
        if args.by_id or not count_ok(total, expected):
            print(f"  {county}: offset paging gave {total:,} of {expected}; retrying by object id")
            total, warns = 0, []
            with tmp.open("w") as fh:
                for lid in layer_ids_for(e):
                    n, w = fetch_layer_by_ids(session, e, e["rest_url"], lid, "1=1", fh)
                    total += n
                    warns += w
        tmp.replace(path)
        ok = count_ok(total, expected)
        report.append({"county": county, "written": total, "expected": expected,
                       "complete": ok, "warnings": warns})
        if not ok:
            # Never mark a short county done: it would be skipped forever.
            print(f"! {county}: {total:,} of {expected:,} - INCOMPLETE, will retry next run")
            continue
        print(f"+ {county}: {total:,} features (county reports {expected})")
        done.write_text(json.dumps(report[-1], indent=1))
    (out_dir / "_fetch_report.json").write_text(json.dumps(report, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", default="raw")
    ap.add_argument("--county", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--by-id", action="store_true", help="address every layer by object id")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
