"""Scrape AeroTrader.com via its JSON search API.

AeroTrader migrated to a Nuxt SPA fronted by DataDome + AWS WAF, so the old
HTML index scrape no longer works (curl_cffi gets a challenge page; even a
JS render returns an un-hydrated shell). The SPA loads listings from an
internal JSON endpoint:

    /ssr-api/search-results?page=<n>&make=<name>

which returns clean structured records (year, make, model, price, location,
description, photo ids, and the canonical detail URL). That endpoint still
sits behind the WAF, but ScrapingBee's *stealth* proxy clears the challenge
and returns the JSON. Because the API carries the full listing payload we no
longer fetch per-listing detail pages at all.

We query *per make* (`?make=<name>`). The unfiltered search (no `make`) is
unreliable — it returns a varying subset of inventory between requests, so
niche makes would intermittently drop to zero. A `?make=` filter returns the
complete, stable set for that make. Each search config carries an `at_make`
(the AeroTrader taxonomy make string) plus `at_patterns` for model-level
filtering on the detail-URL slug. Kit/experimental brands that AeroTrader
files under its catch-all "Other" make (CubCrafters, Bearhawk, Glasair, …)
use `at_make="Other"`; the at_patterns then pick out the specific brand.
Results are cached per make in-process (lru_cache), so the eight Cessna
searches share one fetch.

Cost note: only the stealth proxy gets through (premium just gets the
challenge), at ~75 ScrapingBee credits per request; page size is fixed at 42.
"""
from __future__ import annotations

import functools
import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests

from .common import (
    Listing,
    ScraperFailure,
    extract_engine,
    extract_times_from,
    save_raw,
)

BASE = "https://www.aerotrader.com"
API_URL = f"{BASE}/ssr-api/search-results"
SOURCE = "aerotrader"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"
IMG_TEMPLATE = (
    "https://cdn-media.tilabs.io/v1/media/{pid}.jpg"
    "?width=1024&height=768&quality=70&upsize=true"
)
MAX_PAGES = 15  # safety cap; inventory is normally ~6 pages of 42

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(text: str | None) -> str | None:
    """The API returns HTML-escaped descriptions with inline tags. Unescape
    entities, strip tags, and collapse whitespace to plain text."""
    if not text:
        return None
    text = _TAG_RE.sub(" ", html.unescape(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _fetch_api_page(make: str, page: int) -> dict:
    """Fetch one page of the per-make search API via ScrapingBee's stealth proxy.

    Returns the parsed `data` object. Raises ScraperFailure if the key is
    missing or every attempt is blocked, so scrape.py preserves prior rows.
    """
    api_key = os.environ.get("SCRAPINGBEE_API_KEY")
    if not api_key:
        raise ScraperFailure("AeroTrader needs SCRAPINGBEE_API_KEY (WAF-gated API)")

    target = f"{API_URL}?make={quote(make)}&page={page}"
    params = {
        "api_key": api_key, "url": target,
        "stealth_proxy": "True", "country_code": "us",
        "block_resources": "False",
    }
    for attempt in range(6):
        try:
            r = requests.get(SCRAPINGBEE_ENDPOINT, params=params, timeout=200)
        except requests.RequestException:
            time.sleep(2 + attempt)
            continue
        # Stealth proxy intermittently 500s with "try again (not charged)".
        if r.status_code == 200 and '"total_results"' in r.text:
            body = r.text
            i, j = body.find("{"), body.rfind("}")
            try:
                return json.loads(body[i : j + 1])["data"]
            except (ValueError, KeyError):
                pass
        time.sleep(2 + attempt)
    raise ScraperFailure(
        f"AeroTrader API make={make!r} page {page} blocked by DataDome/WAF "
        "(stealth retries exhausted)"
    )


@functools.lru_cache(maxsize=None)
def _fetch_make(make: str) -> tuple[dict, ...]:
    """Paginate the full result set for one make. Cached per make, so searches
    that share a make (the eight Cessna slugs, the "Other" bucket) fetch once.
    A partial crawl would falsely look like removals, so any failed page raises
    ScraperFailure (preserving prior rows)."""
    first = _fetch_api_page(make, 1)
    total_pages = min(int(first["meta"]["page"].get("total_pages", 1)), MAX_PAGES)

    by_id: dict[int, dict] = {}
    for res in first["results"]:
        by_id[res["ad_id"]["raw"]] = res
    for page in range(2, total_pages + 1):
        data = _fetch_api_page(make, page)
        for res in data["results"]:
            by_id[res["ad_id"]["raw"]] = res

    return tuple(by_id.values())


def _raw(res: dict, key: str):
    v = res.get(key)
    if isinstance(v, dict):
        v = v.get("raw")
    if isinstance(v, list):
        v = v[0] if v else None
    return v


def _make_listing(res: dict, search: dict) -> Listing:
    year = _raw(res, "year")
    make_name = _raw(res, "make_name")
    model_name = _raw(res, "model_name")
    description = _clean_text(_raw(res, "description"))

    price = _raw(res, "price")
    price_str = f"${int(price):,}" if isinstance(price, (int, float)) and price else None

    city = _raw(res, "city")
    state = _raw(res, "state_code")
    location = f"{city.title()}, {state}" if city and state else (state or None)

    photo_ids = res.get("photo_ids", {})
    photo_ids = photo_ids.get("raw") if isinstance(photo_ids, dict) else photo_ids
    image_url = IMG_TEMPLATE.format(pid=photo_ids[0]) if photo_ids else None

    title_bits = [str(b) for b in (year, make_name, model_name) if b]
    title = " ".join(title_bits) or None

    af, en = extract_times_from(description)
    model = search.get("default_model")

    return Listing(
        source=SOURCE,
        url=_raw(res, "ad_detail_url"),
        make=search["make"],
        year=int(year) if isinstance(year, (int, float)) else None,
        model=model,
        price=price_str,
        total_time=af,
        engine_time=en,
        location=location,
        title=title,
        description=description[:1500] if description else None,
        image_url=image_url,
        engine=extract_engine(description, model),
    )


def scrape(search: dict) -> list[Listing]:
    patterns = [p.lower() for p in search["at_patterns"]]

    make_listings = _fetch_make(search["at_make"])
    matched = []
    for res in make_listings:
        url = _raw(res, "ad_detail_url") or ""
        slug = url.lower()
        if any(p in slug for p in patterns):
            matched.append(res)

    save_raw(
        f"{SOURCE}_{search['slug']}",
        "\n".join(_raw(r, "ad_detail_url") or "" for r in matched),
    )

    return [_make_listing(res, search) for res in matched]
