"""Scrape AircraftForSale.com via their public sitemap.

The site's search-page UI is JS-rendered and unreliable to scrape. The
public sitemap at /uploads/sitemap/sitemap_item_detail.xml lists every
detail-page URL in plain XML, which is much cleaner. For each search
config, we filter the sitemap URLs by `sitemap_patterns` (path
substrings, e.g. '/cessna/180'), then fetch each detail page once and
parse year/title/price/AFTT/location/image.

Detail-page responses are cached in data/detail_cache.json (shared with
the enrich script) so daily runs only fetch URLs we haven't seen before.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .common import (
    UA,
    Listing,
    ScraperFailure,
    extract_engine,
    extract_engine_time,
    fetch_via_scrapingbee,
    save_raw,
)

BASE = "https://aircraftforsale.com"
SITEMAP = f"{BASE}/uploads/sitemap/sitemap_item_detail.xml"
SOURCE = "aircraftforsale"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = PROJECT_ROOT / "data" / "detail_cache.json"

_YEAR_TITLE_RE = re.compile(r"^((?:19|20)\d{2})\s+(.+?)\s+For\s+sale", re.I)
_AFTT_RE = re.compile(r"AFTT:\s*([\d,]+)\s*hrs?", re.I)


def _load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, sort_keys=True, indent=2))


def _get(url: str, timeout: int = 20) -> str | None:
    """Fetch a URL directly, falling back to ScrapingBee when blocked.

    The site's nginx answers datacenter IPs (GitHub Actions) with 405 while
    serving residential IPs normally, so the direct attempt succeeds locally
    and the proxy carries CI. The block is purely IP-based — no JS challenge —
    so the 1-credit "classic" tier is enough.
    """
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code == 200:
            return r.text
        blocked = r.status_code in (403, 405, 429)
    except requests.RequestException:
        blocked = True
    if not blocked:
        return None
    return fetch_via_scrapingbee(url, tier="classic")


# The sitemap is one 150KB document covering every listing on the site, but
# scrape() runs once per search config (30+ times a run). Fetch it once.
_sitemap_cache: list[str] | None = None


def _fetch_sitemap_urls() -> list[str]:
    global _sitemap_cache
    if _sitemap_cache is None:
        xml = _get(SITEMAP)
        if xml is None:
            raise RuntimeError(f"could not fetch sitemap {SITEMAP} (direct + ScrapingBee)")
        _sitemap_cache = re.findall(r"<loc>([^<]+)</loc>", xml)
    return _sitemap_cache


def _location_from_url(url: str) -> str | None:
    """Pull '<City>, <ST>' from the URL slug.

    Slugs vary, e.g.:
      .../watsonville-ca-usa/...
      .../78058-mountain-home-texas-united-states/...
    """
    m = re.search(
        r"/(?:\d{5}-)?([a-z][a-z\-]*?)-([a-z]+|[a-z]{2})-(?:united-states|usa)/",
        url,
        re.I,
    )
    if not m:
        return None
    city = m.group(1).replace("-", " ").title()
    state = m.group(2).upper() if len(m.group(2)) == 2 else m.group(2).title()
    return f"{city}, {state}"


def _parse_detail(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.select_one("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else ""
    m = _YEAR_TITLE_RE.match(h1_text)
    year = int(m.group(1)) if m else None
    title = h1_text.replace(" For sale", "").strip() if h1_text else None

    text_blob = soup.get_text(" ", strip=True)
    price_m = re.search(r"\$[\d,]+", text_blob)
    aftt_m = _AFTT_RE.search(text_blob)
    og_img = soup.find("meta", property="og:image")
    img_url = og_img.get("content") if og_img else None

    return {
        "year": year,
        "title": title[:200] if title else None,
        "price": price_m.group(0) if price_m else None,
        "total_time": f"{aftt_m.group(1)} TT" if aftt_m else None,
        "image_url": img_url,
        "description": text_blob[:1500] if text_blob else None,
        "location": _location_from_url(url),
    }


def scrape(search: dict) -> list[Listing]:
    patterns = [p.lower() for p in search["sitemap_patterns"]]
    us_only = search.get("us_only", True)

    sitemap_urls = _fetch_sitemap_urls()
    matched = [u for u in sitemap_urls if any(p in u.lower() for p in patterns)]
    if us_only:
        matched = [
            u for u in matched
            if "united-states" in u.lower() or "-usa/" in u.lower() or "/usa/" in u.lower()
        ]

    cache = _load_cache()
    new_fetches = [u for u in matched if u not in cache or "error" in cache.get(u, {})]

    for i, url in enumerate(new_fetches):
        try:
            html = _get(url, timeout=30)
            if html:
                cache[url] = _parse_detail(url, html)
            else:
                cache[url] = {"error": "fetch failed (direct + ScrapingBee)"}
        except ScraperFailure:
            # Out of credits / bad key — that's about the account, not this URL.
            # Don't poison the cache with it; save progress and let it bubble up.
            _save_cache(cache)
            raise
        except Exception as e:
            cache[url] = {"error": str(e)[:120]}
        if i < len(new_fetches) - 1:
            time.sleep(1.5)
        if (i + 1) % 10 == 0:
            _save_cache(cache)
    if new_fetches:
        _save_cache(cache)

    save_raw(f"{SOURCE}_{search['slug']}", "\n".join(matched))

    listings: list[Listing] = []
    for url in matched:
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
                location=data.get("location"),
                title=data.get("title"),
                description=data.get("description"),
                image_url=data.get("image_url"),
                engine=extract_engine(data.get("description"), model),
                engine_time=extract_engine_time(data.get("description")),
            )
        )

    return listings
