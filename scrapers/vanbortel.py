"""Scrape Van Bortel Aircraft (vanbortel.com) — a single-location dealer.

Their /inventory page lists every aircraft detail URL under
/cessnas-for-sale/<year>-<make>-<model>-<n-number> (despite the name, the
prefix is used for non-Cessna inventory too). For each search config we
filter those URLs by `vb_patterns` (path substrings) and fetch each
detail page once, caching in data/detail_cache.json.

All Van Bortel listings show their Arlington, TX location since that's
where the aircraft live.
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
    extract_engine,
    extract_times_from,
    save_raw,
)

BASE = "https://www.vanbortel.com"
INVENTORY = f"{BASE}/inventory"
SOURCE = "vanbortel"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = PROJECT_ROOT / "data" / "detail_cache.json"

_TITLE_RE = re.compile(r"^((?:19|20)\d{2})\s+(.+)", re.I)


def _load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, sort_keys=True, indent=2))


def _fetch_inventory_urls() -> list[str]:
    r = requests.get(INVENTORY, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return sorted(
        set(
            re.findall(
                r'href="(/[a-z-]+for-sale/[^"#?]+)"', r.text, re.I
            )
        )
    )


def _parse_detail(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.select_one("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else ""
    m = _TITLE_RE.match(h1_text)
    year = int(m.group(1)) if m else None
    title = h1_text.strip() if h1_text else None

    text_blob = soup.get_text(" ", strip=True)
    price_m = re.search(r"\$[\d,]+", text_blob)
    og_img = soup.find("meta", property="og:image")
    img_url = og_img.get("content") if og_img else None
    af, en = extract_times_from(text_blob)

    return {
        "year": year,
        "title": title[:200] if title else None,
        "price": price_m.group(0) if price_m else None,
        "total_time": af,
        "engine_time": en,
        "image_url": img_url,
        "description": text_blob[:1500] if text_blob else None,
        # Van Bortel operates out of Arlington Municipal Airport (KGKY).
        "location": "Arlington, TX",
    }


def scrape(search: dict) -> list[Listing]:
    patterns = [p.lower() for p in search["vb_patterns"]]

    inv_urls = _fetch_inventory_urls()
    matched = [u for u in inv_urls if any(p in u.lower() for p in patterns)]

    cache = _load_cache()
    new_fetches = [u for u in matched if u not in cache or "error" in cache.get(u, {})]
    for i, path in enumerate(new_fetches):
        url = f"{BASE}{path}"
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            if r.status_code == 200:
                cache[path] = _parse_detail(url, r.text)
            else:
                cache[path] = {"error": f"status {r.status_code}"}
        except Exception as e:
            cache[path] = {"error": str(e)[:120]}
        if i < len(new_fetches) - 1:
            time.sleep(1.5)
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
