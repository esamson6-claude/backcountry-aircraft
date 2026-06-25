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
import os
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
# between the city, state, and "USA": "Location: McAllen    ,    TX    USA".
# "USA" is optional so non-US listings (City, ST <Country>) still resolve to a
# clean "City, ST".
_TAP_LOC_RE = re.compile(
    r"Location:\s*([A-Za-z][A-Za-z\.\-' ]+?)\s*,\s*([A-Z]{2})\b(?:\s+USA)?",
    re.IGNORECASE,
)

# Business-name tokens that indicate a seller name glued in front of the city,
# e.g. "Air LLC Houston, TX" or "Solutions Group Troy, MI". Used as a last-resort
# cleaner for rows the detail-page fetch can't fix.
_BIZ_RE = re.compile(
    r"^(LLC|L\.L\.C\.?|Inc\.?|Incorporated|Co\.?|Corp\.?|Aviation|Aircraft|Aero|"
    r"Airlines|Services|Service|Group|Sales|Jet|Jets|Aviators?|Air|Avionics|"
    r"Center|Aerospace|Flight|Flying|Wings|Hangar|Brokerage|Brokers?|Trading|"
    r"Holdings|Enterprises|Partners|Capital|Leasing|Sky|Skies)$",
    re.IGNORECASE,
)


def _strip_seller_name(loc: str) -> str:
    """Best-effort: drop a seller business name glued before a 'City, ST' tail.

    "Air LLC Houston, TX" -> "Houston, TX"; "Solutions Group Troy, MI" -> "Troy, MI".
    Conservative: only fires when a business-keyword token precedes the city, and
    never when the business word *is* the last (city) word. Person-name prefixes
    (e.g. "Brian Jenkins Alpine, WY") aren't detectable and are left for the
    detail-page fetch to clean.
    """
    if not loc or "," not in loc:
        return loc
    before, after = loc.rsplit(",", 1)
    state = after.strip()
    if not re.fullmatch(r"[A-Za-z]{2}", state):  # only US/CA 2-letter tails
        return loc
    words = before.split()
    last_biz = -1
    for i, w in enumerate(words):
        if _BIZ_RE.match(w.strip(".,")):
            last_biz = i
    if last_biz == -1 or last_biz == len(words) - 1:
        return loc  # no business word, or it would eat the whole city
    city = " ".join(words[last_biz + 1:]).strip()
    return f"{city}, {state.upper()}" if city else loc


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

    # Normally fetch only uncached URLs plus transient errors (so we retry a blip
    # but never re-hit pages that genuinely have no clean Location). Set
    # ENRICH_RECLEAN=1 to also re-fetch entries that resolved to no location —
    # used for the one-time backfill after hardening the extractor.
    reclean = os.environ.get("ENRICH_RECLEAN") == "1"

    def _needs_fetch(url: str) -> bool:
        if url not in cache:
            return True
        entry = cache[url]
        if "error" in entry:           # transient failure — retry
            return True
        if reclean and entry.get("location") is None:
            return True
        return False

    todo = [u for u in tap_urls if _needs_fetch(u)]

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
    """Clean each row's location: prefer the cached TAP detail-page value, then
    run the seller-name stripper (all sources) as a final fallback."""
    changed = 0
    for r in rows:
        loc = r.get("location") or ""
        if r.get("source") == "trade-a-plane":
            cached_loc = cache.get(r["url"], {}).get("location")
            if cached_loc:
                loc = cached_loc
        loc = _strip_seller_name(loc)
        if loc and loc != r.get("location"):
            r["location"] = loc
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
