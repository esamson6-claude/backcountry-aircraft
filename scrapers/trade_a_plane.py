"""Scrape Trade-A-Plane (curl_cffi Chrome impersonation)."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

from .common import (
    Listing,
    extract_engine,
    extract_engine_time,
    first_hours,
    first_price,
    first_year,
    save_raw,
)

BASE = "https://www.trade-a-plane.com"
SOURCE = "trade-a-plane"

_LISTING_ID_RE = re.compile(r"listing_id=(\d+)")
# Allow optional whitespace before the comma since TAP renders both
# "City, ST USA" and "City , ST USA" depending on make/template.
_LOC_RE = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:[ \-][A-Z][a-zA-Z]+){0,2})\s*,\s+([A-Z]{2})\b"
)


def _model_from_url(href: str) -> str | None:
    m = re.search(r"model=([^&]+)", href)
    return m.group(1).replace("+", " ") if m else None


def scrape(search: dict) -> list[Listing]:
    html = cr.get(search["url"], impersonate="chrome", timeout=30).text
    save_raw(f"{SOURCE}_{search['slug']}", html)
    soup = BeautifulSoup(html, "lxml")

    listings: list[Listing] = []
    seen: set[str] = set()

    for card in soup.select("div.result_listing"):
        anchor = card.select_one("a[href*='listing_id=']")
        if not anchor:
            continue
        href = anchor.get("href", "")
        m = _LISTING_ID_RE.search(href)
        if not m:
            continue
        listing_id = m.group(1)
        if listing_id in seen:
            continue
        seen.add(listing_id)

        url = urljoin(BASE, href)
        text = card.get_text(" ", strip=True)
        loc_m = _LOC_RE.search(text)
        location = f"{loc_m.group(1).strip()}, {loc_m.group(2)}" if loc_m else None

        title_m = re.match(
            r"((?:19|20)\d{2}\s+\S+\s+\S+[^A-Z]*[A-Z0-9\-]*)", text
        )
        title = title_m.group(1).strip() if title_m else text[:80]

        img_el = card.find("img")
        img_url = None
        if img_el:
            img_url = (
                img_el.get("data-src")
                or img_el.get("data-original")
                or img_el.get("src")
            )

        model = _model_from_url(href) or search.get("default_model")
        listings.append(
            Listing(
                source=SOURCE,
                url=url,
                make=search["make"],
                year=first_year(text),
                model=model,
                price=first_price(text),
                total_time=first_hours(text),
                location=location,
                title=title,
                description=text[:800],
                image_url=img_url,
                engine=extract_engine(text, model),
                engine_time=extract_engine_time(text),
            )
        )

    return listings
