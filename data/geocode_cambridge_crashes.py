"""
Step 2: Geocode the cleaned Cambridge PD crash log.

Input : cambridge_clean.csv (output of clean_cambridge_crashes.py)
Output: same rows plus lat, lon, geocode_source, geocode_status, matched_address

Methods
  * address rows      -> US Census batch geocoder (free, no key)
  * intersection rows -> OpenStreetMap Nominatim (free, 1 request/second)
  * street_only / none rows are NOT geocoded (too vague to place on a map)

Every result is checked against a Cambridge bounding box; anything outside is
marked "out_of_bounds" and its coordinates are dropped.

Results are cached in geocode_cache.json (next to the output file), so re-runs
only query things not already looked up.

Usage
  Test on a random sample first (recommended):
    python geocode_cambridge_crashes.py IN.csv OUT.csv --sample 300
  Then run everything:
    python geocode_cambridge_crashes.py IN.csv OUT.csv
"""

import argparse
import csv
import io
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim policy requires an identifying User-Agent. Put your contact info in.
USER_AGENT = (
    "BCU-Labs-BU-Spark-crash-geocoding/1.0 (student project; contact: trentr@bu.edu)"
)

# Rough bounding box around Cambridge, MA (a little padding on each side)
LAT_MIN, LAT_MAX = 42.345, 42.410
LON_MIN, LON_MAX = -71.165, -71.050
# Nominatim viewbox is left,top,right,bottom
VIEWBOX = f"{LON_MIN},{LAT_MAX},{LON_MAX},{LAT_MIN}"

CENSUS_CHUNK = 1000  # rows per batch request (Census limit is 10,000), we have about 5000 addresses so 1000 is 5 batches
NOMINATIM_DELAY = 1.1  # default seconds between requests (policy: max 1/second)
# Scripts run at regular intervals (e.g. monthly automation) or for more than a
# day are limited to 4 requests/minute -> use --nominatim-delay 15 for those.


class NominatimBlocked(Exception):
    """Raised on HTTP 403/429 so we STOP instead of hammering the server."""


def in_bounds(lat, lon):
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


def clean_num(n):
    """'203.0' -> '203'; blank -> ''."""
    if pd.isna(n):
        return ""
    m = re.match(r"\d+", str(n).strip())
    return m.group(0) if m else ""


# ---------------------------------------------------------------- cache
def load_cache(path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_cache(cache, path):
    path.write_text(json.dumps(cache), encoding="utf-8")


# ---------------------------------------------------------------- Census
def parse_census_response(text):
    """Return {id: result dict}. Census returns CSV with no header row:
    id, input, status, match type, matched address, 'lon,lat', tiger id, side
    (No_Match rows only have the first three fields)."""
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3:
            continue
        rid, status = row[0], row[2]
        if status == "Match" and len(row) >= 6 and row[5]:
            lon, lat = (float(x) for x in row[5].split(","))
            out[rid] = {
                "status": "match",
                "lat": lat,
                "lon": lon,
                "matched": row[4],
                "quality": row[3],
            }
        else:
            out[rid] = {
                "status": "no_match",
                "lat": None,
                "lon": None,
                "matched": None,
                "quality": status,
            }
    return out


def census_batch(queries):
    """queries: list of (id, street). Returns {id: result}."""
    buf = io.StringIO()
    w = csv.writer(buf)
    for qid, street in queries:
        w.writerow([qid, street, "Cambridge", "MA", ""])
    files = {"addressFile": ("addresses.csv", buf.getvalue())}
    data = {"benchmark": "Public_AR_Current"}
    for attempt in range(3):
        try:
            r = requests.post(CENSUS_URL, files=files, data=data, timeout=300)
            r.raise_for_status()
            return parse_census_response(r.text)
        except requests.RequestException as e:
            print(f"  Census request failed ({e}); retry {attempt + 1}/3")
            time.sleep(5 * (attempt + 1))
    return {
        qid: {
            "status": "error",
            "lat": None,
            "lon": None,
            "matched": None,
            "quality": "request_failed",
        }
        for qid, _ in queries
    }


# ------------------------------------------------------------- Nominatim
def nominatim_lookup(query):
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
        "viewbox": VIEWBOX,
        "bounded": 1,
    }
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(3):
        try:
            r = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=30)
            if r.status_code in (403, 429):
                raise NominatimBlocked(f"HTTP {r.status_code}")
            r.raise_for_status()
            hits = r.json()
            if not hits:
                return {
                    "status": "no_match",
                    "lat": None,
                    "lon": None,
                    "matched": None,
                    "quality": "no_result",
                }
            h = hits[0]
            return {
                "status": "match",
                "lat": float(h["lat"]),
                "lon": float(h["lon"]),
                "matched": h.get("display_name"),
                "quality": h.get("type"),
            }
        except (requests.RequestException, ValueError) as e:
            print(f"  Nominatim failed ({e}); retry {attempt + 1}/3")
            time.sleep(5 * (attempt + 1))
    return {
        "status": "error",
        "lat": None,
        "lon": None,
        "matched": None,
        "quality": "request_failed",
    }


def intersection_key(c1, c2):
    """Same key for 'A & B' and 'B & A' so we only look each up once."""
    a, b = sorted([c1, c2])
    return f"{a} & {b}, CAMBRIDGE, MA"


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("infile")
    ap.add_argument("outfile")
    ap.add_argument(
        "--sample",
        type=int,
        default=None,
        help="geocode only N random geocodable rows (for testing)",
    )
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--nominatim-delay",
        type=float,
        default=NOMINATIM_DELAY,
        help="seconds between Nominatim requests (default 1.1; "
        "use 15 for scheduled/recurring runs)",
    )
    args = ap.parse_args()

    df = pd.read_csv(args.infile, dtype=str)
    cache_path = Path(args.outfile).with_name("geocode_cache.json")
    cache = load_cache(cache_path)

    df["street_num"] = df["street_num"].map(clean_num)
    is_addr = df["location_type"] == "address"
    is_int = df["location_type"] == "intersection"

    if args.sample:
        pool = df[is_addr | is_int]
        keep = pool.sample(n=min(args.sample, len(pool)), random_state=args.seed).index
        df = df.loc[keep].copy()
        is_addr = df["location_type"] == "address"
        is_int = df["location_type"] == "intersection"
        print(
            f"SAMPLE MODE: {len(df)} rows "
            f"({is_addr.sum()} address, {is_int.sum()} intersection)"
        )

    # Build the lookup key for each row
    df["_key"] = None
    df.loc[is_addr, "_key"] = "A|" + df["street_num"] + " " + df["street_name"]
    df.loc[is_int, "_key"] = [
        "I|" + intersection_key(a, b)
        for a, b in zip(df.loc[is_int, "cross_1"], df.loc[is_int, "cross_2"])
    ]

    # ---- addresses via Census (only keys not already cached)
    addr_keys = sorted(
        {k for k in df["_key"].dropna() if k.startswith("A|") and k not in cache}
    )
    print(
        f"Census: {len(addr_keys)} unique addresses to look up "
        f"({sum(k.startswith('A|') for k in cache)} cached)"
    )
    for i in range(0, len(addr_keys), CENSUS_CHUNK):
        chunk = addr_keys[i : i + CENSUS_CHUNK]
        queries = [(str(j), k[2:]) for j, k in enumerate(chunk)]
        results = census_batch(queries)
        for j, k in enumerate(chunk):
            res = results.get(str(j), {"status": "error"})
            if res["status"] != "error":  # never cache transient failures
                cache[k] = {**res, "source": "census"}
        save_cache(cache, cache_path)
        print(f"  Census chunk {i // CENSUS_CHUNK + 1}: done")

    # ---- intersections via Nominatim (slow: ~1 per second)
    int_keys = sorted(
        {k for k in df["_key"].dropna() if k.startswith("I|") and k not in cache}
    )
    delay = args.nominatim_delay
    mins = len(int_keys) * delay / 60
    print(f"Nominatim: {len(int_keys)} unique intersections (~{mins:.0f} min)")
    if int_keys:
        print(
            "  Reminder: public Nominatim policy = single thread, one machine, "
            "cached results, valid User-Agent. Credit OpenStreetMap contributors "
            "if you publish results."
        )
    for n, k in enumerate(int_keys, 1):
        try:
            res = nominatim_lookup(k[2:])
        except NominatimBlocked as e:
            save_cache(cache, cache_path)
            sys.exit(
                f"STOPPED: Nominatim refused the request ({e}). Progress is "
                f"saved in the cache. Do not retry right away; check the "
                f"User-Agent and rate, and wait before rerunning."
            )
        if res["status"] != "error":  # never cache transient failures
            cache[k] = {**res, "source": "nominatim"}
        time.sleep(delay)
        if n % 50 == 0:
            save_cache(cache, cache_path)
            print(f"  {n}/{len(int_keys)}")
    save_cache(cache, cache_path)

    # ---- attach results, bounds-check
    def get(key, field):
        return cache.get(key, {}).get(field) if key else None

    df["lat"] = df["_key"].map(lambda k: get(k, "lat"))
    df["lon"] = df["_key"].map(lambda k: get(k, "lon"))
    df["geocode_source"] = df["_key"].map(lambda k: get(k, "source"))
    df["geocode_status"] = df["_key"].map(lambda k: get(k, "status"))
    df["matched_address"] = df["_key"].map(lambda k: get(k, "matched"))
    df["match_quality"] = df["_key"].map(lambda k: get(k, "quality"))

    has_xy = df["lat"].notna() & df["lon"].notna()
    oob = has_xy & ~df.apply(
        lambda r: in_bounds(r["lat"], r["lon"]) if pd.notna(r["lat"]) else False, axis=1
    )
    df.loc[oob, ["lat", "lon"]] = None
    df.loc[oob, "geocode_status"] = "out_of_bounds"
    df.loc[~(is_addr | is_int), "geocode_status"] = "not_attempted"
    df.loc[(is_addr | is_int) & df["geocode_status"].isna(), "geocode_status"] = (
        "lookup_failed_rerun"
    )

    df = df.drop(columns="_key")
    df.to_csv(args.outfile, index=False)

    # ---- report (paste this into your data-limitations notes)
    print("\n=== Geocoding report ===")
    for lt in ["address", "intersection"]:
        sub = df[df["location_type"] == lt]
        if len(sub):
            ok = (sub["geocode_status"] == "match").sum()
            print(f"{lt:13s} {ok:>6,} / {len(sub):>6,} placed ({ok / len(sub):.1%})")
    print("\nStatus counts:")
    print(df["geocode_status"].value_counts(dropna=False).to_string())
    print(f"\nWrote {args.outfile}")


if __name__ == "__main__":
    sys.exit(main())
