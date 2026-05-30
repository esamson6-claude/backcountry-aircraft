"""Fetch listing detail pages for richer fields (currently: clean location).

Most search-page cards on Trade-A-Plane embed the seller's business name
into the location text (e.g. "Aero Services Sidney , BC USA"), which
breaks both display and geocoding. The detail page has a clean
"Location: <City>, <ST> USA" field. We fetch it once per URL and cache
the result in data/detail_cache.json.

Idempotent — only URLs missing from the cache hit the network.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

PROJECT_ROOT = Path(__file__).resolve().parent
CSV_PATH = PROJECT_ROOT / "data" / "listings.csv"
CACHE_PATH = PROJECT_ROOT / "data" / "detail_cache.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Matches the detail-page "Location:" block which renders with whitespace/tabs
# between the city, state, and "USA": "Location: McAllen    ,    TX    USA"
_TAP_LOC_RE = re.compile(
    r"Location:\s*([A-Za-z][A-Za-z\.\-' ]+?)\s*,\s*([A-Z]{2})\s+USA",
    re.IGNORECASE,
)


def load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, sort_keys=True, indent=2))


def fetch_tap_detail(url: str) -> dict:
    """Pull the clean Location field from a Trade-A-Plane detail page.

    HTML between the 'Location:' label and the city is interspersed with
    tags + tabs, so we strip to rendered text via BS4 first.
    """
    try:
        html = cr.get(url, impersonate="chrome", timeout=30).text
    except Exception as e:
        return {"error": str(e)[:120]}
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    m = _TAP_LOC_RE.search(text)
    if not m:
        return {"location": None}
    city = re.sub(r"\s+", " ", m.group(1)).strip()
    return {"location": f"{city}, {m.group(2)}"}


def main() -> int:
    if not CSV_PATH.exists():
        print(f"{CSV_PATH} not found — run scrape.py first", file=sys.stderr)
        return 1

    cache = load_cache()
    rows = list(csv.DictReader(CSV_PATH.open(newline="", encoding="utf-8")))

    # Only enrich TAP listings (other sources have clean data or trivial counts)
    tap_urls = [r["url"] for r in rows if r.get("source") == "trade-a-plane"]
    todo = [u for u in tap_urls if u not in cache]

    if not todo:
        print(f"  enrich: cache complete ({len(cache)} entries, 0 new)", file=sys.stderr)
        # Apply cache to rows anyway (in case CSV was regenerated)
        _apply_cache_to_csv(rows, cache)
        return 0

    print(f"  enrich: fetching {len(todo)} TAP detail pages…", file=sys.stderr)
    for i, url in enumerate(todo):
        cache[url] = fetch_tap_detail(url)
        if i < len(todo) - 1:
            time.sleep(2.5)  # be polite to TAP — they 403 us above ~1/sec
        if (i + 1) % 25 == 0:
            save_cache(cache)
            print(f"    progress: {i+1}/{len(todo)}", file=sys.stderr)

    save_cache(cache)
    hits = sum(1 for url in tap_urls if cache.get(url, {}).get("location"))
    print(f"  enrich: done — {hits}/{len(tap_urls)} TAP listings now have clean location", file=sys.stderr)

    _apply_cache_to_csv(rows, cache)
    return 0


def _apply_cache_to_csv(rows: list[dict], cache: dict[str, dict]) -> None:
    """Update each row's location field with the cached cleaner value (if any)."""
    changed = 0
    for r in rows:
        if r.get("source") != "trade-a-plane":
            continue
        cached_loc = cache.get(r["url"], {}).get("location")
        if cached_loc and cached_loc != r.get("location"):
            r["location"] = cached_loc
            changed += 1
    if changed:
        fields = list(rows[0].keys())
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"  enrich: updated {changed} rows in {CSV_PATH.name}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
