"""Scrape aircraft.com (Sandhills network — sister site to Controller).

aircraft.com has two surfaces:
  * Detail pages — /aircraft/<id>/<n-number>-<year>-<make>-<model> — these
    fetch fine with curl_cffi (Chrome TLS impersonation). No Imperva.
  * Index/category pages (/cessna/aircraft etc.) — Imperva-protected and
    only return 24 random "featured" listings even when reached via
    ScrapingBee. No pagination, no filterable search.

Because the index isn't enumerable for arbitrary makes, we don't scrape it.
Instead, this scraper accepts an explicit `urls` list in the search config
and fetches each detail page once (cached). Add aircraft.com URLs you find
in the wild to the SEARCHES entry for the corresponding make.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cr

from .common import (
    Listing,
    extract_engine,
    extract_times_from,
    save_raw,
)

SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"
_INTERSTITIAL_RE = re.compile(r"Pardon Our Interruption", re.I)

SOURCE = "aircraft.com"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = PROJECT_ROOT / "data" / "detail_cache.json"

# Title format on aircraft.com: "N432WC - 1951 CESSNA L19 305A BIRD DOG"
_TITLE_RE = re.compile(
    r"^(?:N\w+\s*[-–]\s*)?((?:19|20)\d{2})\s+(.+)$", re.I
)


def _load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, sort_keys=True, indent=2))


def _fetch_html(url: str) -> str | None:
    """Try curl_cffi first; if Imperva blocks, fall back to ScrapingBee."""
    try:
        r = cr.get(url, impersonate="chrome", timeout=30)
        if r.status_code == 200 and not _INTERSTITIAL_RE.search(r.text):
            return r.text
    except Exception:
        pass

    api_key = os.environ.get("SCRAPINGBEE_API_KEY")
    if not api_key:
        return None
    params = {
        "api_key": api_key, "url": url,
        "render_js": "True", "premium_proxy": "True",
        "country_code": "us", "block_resources": "False", "wait": "5000",
    }
    for attempt in range(3):
        try:
            r = requests.get(SCRAPINGBEE_ENDPOINT, params=params, timeout=180)
        except requests.RequestException:
            continue
        if r.status_code == 200 and not _INTERSTITIAL_RE.search(r.text):
            return r.text
    print(
        f"  [aircraft.com] all fetchers failed for {url[:60]}",
        file=sys.stderr,
    )
    return None


def _parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.select_one("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else ""
    m = _TITLE_RE.match(h1_text)
    year = int(m.group(1)) if m else None
    rest = m.group(2).strip() if m else h1_text

    text_blob = soup.get_text(" ", strip=True)
    price_m = re.search(r"\$[\d,]+", text_blob)
    af, en = extract_times_from(text_blob)
    # Listing image — aircraft.com puts photos under media.sandhills.com/img.axd
    # The first <img> is a country flag and the logo is in Images/Logos.
    img_url = None
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "media.sandhills.com/img.axd" in src:
            img_url = src
            break

    # Location — aircraft.com surfaces this in the spec table
    loc_m = re.search(
        r"\b([A-Z][a-zA-Z\.\-' ]+,\s*[A-Z]{2})\b(?:\s+\d{5})?", text_blob
    )

    return {
        "year": year,
        "title": h1_text[:200] if h1_text else None,
        "price": price_m.group(0) if price_m else None,
        "total_time": af,
        "engine_time": en,
        "image_url": img_url,
        "location": loc_m.group(1).strip() if loc_m else None,
        "description": text_blob[:1500] if text_blob else None,
    }


def scrape(search: dict) -> list[Listing]:
    urls: list[str] = list(search.get("urls") or [])
    if not urls:
        return []

    cache = _load_cache()
    # Re-fetch any cached entries that previously got the Imperva interstitial
    # (their "title" got stored as "Pardon Our Interruption").
    needs_fetch = [
        u for u in urls
        if u not in cache
        or "error" in cache.get(u, {})
        or (cache.get(u, {}).get("title") or "").startswith("Pardon Our Interruption")
    ]
    for i, url in enumerate(needs_fetch):
        html = _fetch_html(url)
        if html is None:
            cache[url] = {"error": "fetch failed (Imperva)"}
        else:
            cache[url] = _parse_detail(html)
        if i < len(needs_fetch) - 1:
            time.sleep(2.0)
    if needs_fetch:
        _save_cache(cache)

    save_raw(f"aircraftcom_{search['slug']}", "\n".join(urls))

    listings: list[Listing] = []
    for url in urls:
        data = cache.get(url) or {}
        if "error" in data or not data:
            continue
        model = search.get("default_model")
        listings.append(
            Listing(
                source=SOURCE,
                url=url,
                make=search["make"],
                year=data.get("year"),
                model=model,
                price=data.get("price"),
                total_time=data.get("total_time"),
                engine_time=data.get("engine_time"),
                location=data.get("location"),
                title=data.get("title"),
                description=data.get("description"),
                image_url=data.get("image_url"),
                engine=extract_engine(data.get("description"), model),
            )
        )

    return listings
