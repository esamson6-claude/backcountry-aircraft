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
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

from .common import (
    Listing,
    extract_engine,
    extract_times_from,
    save_raw,
)

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
    # Listing image — aircraft.com puts the first listing photo as the only
    # non-logo CDN image on the page
    img_url = None
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith(("http://", "https://")) and (
            "sandhills.com/CDN/Images" in src or "media.sandhills" in src
        ) and "logo" not in src.lower():
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
    new_fetches = [u for u in urls if u not in cache or "error" in cache.get(u, {})]
    for i, url in enumerate(new_fetches):
        try:
            r = cr.get(url, impersonate="chrome", timeout=30)
            if r.status_code == 200:
                cache[url] = _parse_detail(r.text)
            else:
                cache[url] = {"error": f"status {r.status_code}"}
        except Exception as e:
            cache[url] = {"error": str(e)[:120]}
        if i < len(new_fetches) - 1:
            time.sleep(2.0)
    if new_fetches:
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
