"""Scrape Trade-A-Plane (curl_cffi Chrome impersonation)."""
from __future__ import annotations

import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

from .common import (
    Listing,
    ScraperFailure,
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


# "Showing N - M of T" — TAP's count widget tells us how many listings total
# the search returned, so we know how many pages to fetch.
_COUNT_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*of\s*(\d+)")
PAGE_SIZE = 24  # TAP's fixed listings-per-page
MAX_PAGES = 10  # safety cap


def _is_blocked(r) -> bool:
    return r.status_code != 200 or "captcha" in r.text.lower()[:5000]


def _page_url(base_url: str, page: int) -> str:
    """Build the pagination URL for page N (TAP uses /search?...&s-page=N)."""
    paged = base_url.replace("/filtered/search?", "/search?", 1)
    if "s-page=" in paged:
        paged = re.sub(r"s-page=\d+", f"s-page={page}", paged)
    else:
        paged = paged + ("&" if "?" in paged else "?") + f"s-page={page}"
    return paged


def _parse_page(html: str, search: dict, seen: set[str]) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    out: list[Listing] = []
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
        out.append(
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
    return out


def scrape(search: dict) -> list[Listing]:
    # Page 1 — same URL as before
    r = cr.get(search["url"], impersonate="chrome", timeout=30)
    if _is_blocked(r):
        raise ScraperFailure(
            f"Trade-A-Plane returned {r.status_code} / captcha — rate-limited"
        )
    html = r.text
    save_raw(f"{SOURCE}_{search['slug']}", html)

    seen: set[str] = set()
    listings = _parse_page(html, search, seen)

    # Look up the total count from the "1 - 24 of N" widget so we know how
    # many pages exist. Stop early if cap reached or a page returns no new
    # listings (defensive).
    m = _COUNT_RE.search(html)
    total = int(m.group(3)) if m else len(listings)
    n_pages = min(MAX_PAGES, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    for page in range(2, n_pages + 1):
        time.sleep(2.5)  # be polite — TAP rate-limits aggressively
        page_url = _page_url(search["url"], page)
        try:
            rp = cr.get(page_url, impersonate="chrome", timeout=30)
        except Exception:
            break
        if _is_blocked(rp):
            # Partial data is better than wiping everything — return what we have.
            print(
                f"  [{SOURCE}] {search['make']} page {page}: rate-limited, "
                f"keeping {len(listings)}/{total} listings",
                file=__import__("sys").stderr,
            )
            break
        before = len(listings)
        listings.extend(_parse_page(rp.text, search, seen))
        if len(listings) == before:
            break

    return listings
