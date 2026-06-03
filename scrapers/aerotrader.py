"""Scrape AeroTrader.com — small but high-quality general marketplace.

Site is bot-protected (returns 403 for plain requests), so we use
curl_cffi with Chrome TLS impersonation. The listings index at
/aircraft-for-sale exposes all current detail URLs in the format
/listing/<year>-<make>-<model>-<numeric-id>. We filter those by
`at_patterns` and fetch each detail page, caching per URL.
"""
from __future__ import annotations

import functools
import json
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

from .common import (
    Listing,
    ScraperFailure,
    extract_engine,
    extract_times_from,
    save_raw,
)

BASE = "https://www.aerotrader.com"
INDEX_URL = f"{BASE}/aircraft-for-sale"
SOURCE = "aerotrader"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = PROJECT_ROOT / "data" / "detail_cache.json"

_SLUG_RE = re.compile(
    r"/listing/(\d{4})-([A-Z][a-zA-Z+]+)-([A-Za-z0-9\-+]+)-(\d+)", re.I
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


@functools.lru_cache(maxsize=1)
def _fetch_listing_urls() -> tuple[str, ...]:
    # AeroTrader sits behind DataDome + AWS WAF and intermittently returns
    # a challenge page (status 200 but with no listings) instead of the real
    # index. Retry a few times with brief backoff; raise ScraperFailure if
    # every attempt comes back empty so existing rows are preserved.
    for attempt in range(4):
        r = cr.get(INDEX_URL, impersonate="chrome", timeout=30)
        if r.status_code == 200:
            urls = sorted(set(re.findall(r'href="(/listing/[^"#?]+)"', r.text)))
            if urls:
                return tuple(urls)
        time.sleep(2 + attempt)  # 2, 3, 4, 5s
    raise ScraperFailure(
        "AeroTrader served a DataDome/WAF challenge — no listings extracted"
    )


def _parse_detail(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.select_one("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else ""
    year_m = re.match(r"(\d{4})", h1_text)
    year = int(year_m.group(1)) if year_m else None

    text_blob = soup.get_text(" ", strip=True)
    price_m = re.search(r"\$[\d,]+", text_blob)
    loc_m = re.search(
        r"(?:located|in)\s+([A-Z][a-zA-Z\.\-' ]+,\s*[A-Z]{2})\b", text_blob
    )
    af, en = extract_times_from(text_blob)

    # Try a few image strategies — they don't expose og:image so look in DOM
    img_url = None
    og = soup.find("meta", property="og:image")
    if og:
        img_url = og.get("content")
    if not img_url:
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith(("http://", "https://")) and any(
                k in src.lower() for k in ("listing", "vehicle", "aircraft", "cdn")
            ):
                img_url = src
                break

    return {
        "year": year,
        "title": h1_text[:200] if h1_text else None,
        "price": price_m.group(0) if price_m else None,
        "total_time": af,
        "engine_time": en,
        "location": loc_m.group(1).strip() if loc_m else None,
        "image_url": img_url,
        "description": text_blob[:1500] if text_blob else None,
    }


def scrape(search: dict) -> list[Listing]:
    patterns = [p.lower() for p in search["at_patterns"]]

    inv_urls = _fetch_listing_urls()
    matched = []
    for path in inv_urls:
        m = _SLUG_RE.search(path)
        if not m:
            continue
        slug = path.lower()
        if any(p in slug for p in patterns):
            matched.append(path)

    cache = _load_cache()
    new_fetches = [u for u in matched if u not in cache or "error" in cache.get(u, {})]
    for i, path in enumerate(new_fetches):
        url = f"{BASE}{path}"
        try:
            r = cr.get(url, impersonate="chrome", timeout=30)
            if r.status_code == 200:
                cache[path] = _parse_detail(url, r.text)
            else:
                cache[path] = {"error": f"status {r.status_code}"}
        except Exception as e:
            cache[path] = {"error": str(e)[:120]}
        if i < len(new_fetches) - 1:
            time.sleep(2.0)
    if new_fetches:
        _save_cache(cache)

    save_raw(f"{SOURCE}_{search['slug']}", "\n".join(matched))

    listings: list[Listing] = []
    for path in matched:
        data = cache.get(path) or {}
        if "error" in data or not data:
            continue
        full_url = f"{BASE}{path}"
        model = search.get("default_model")
        listings.append(
            Listing(
                source=SOURCE,
                url=full_url,
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
