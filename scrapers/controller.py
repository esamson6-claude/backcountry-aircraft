"""Scrape Controller.com.

Controller is behind Imperva bot protection that defeats curl_cffi and plain
Playwright. We use ScrapingBee (https://www.scrapingbee.com) as the primary
fetcher when SCRAPINGBEE_API_KEY is set — it handles the CAPTCHA and returns
the real HTML. curl_cffi and Playwright are kept as fallbacks for the day
ScrapingBee credits run out (they will fail until then).
"""
from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urljoin

import requests
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

BASE = "https://www.controller.com"
SOURCE = "controller"
SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"

# Controller sits behind two different challenge systems: Imperva ("Pardon Our
# Interruption") and Cloudflare ("Just a moment..."). Both return HTTP 200 with
# a page that parses to zero listings, so a challenge that isn't recognised here
# looks exactly like "this make has no aircraft for sale" — the listings then
# silently disappear from the CSV.
_INTERSTITIAL_RE = re.compile(
    r"Pardon Our Interruption|Just a moment|Checking your browser|cf-browser-verification",
    re.I,
)


def _fetch_via_scrapingbee(url: str) -> str | None:
    api_key = os.environ.get("SCRAPINGBEE_API_KEY")
    if not api_key:
        return None
    params = {
        "api_key": api_key,
        "url": url,
        "render_js": "True",
        "premium_proxy": "True",
        "country_code": "us",
        "block_resources": "False",
        # Give the JS time to populate listing cards.
        "wait": "8000",
    }
    # ScrapingBee 500s intermittently with "try again"; retry up to 3x. Failed
    # requests aren't charged, so retry cost is zero.
    last_err = ""
    for attempt in range(3):
        try:
            r = requests.get(SCRAPINGBEE_ENDPOINT, params=params, timeout=180)
        except requests.RequestException as e:
            last_err = str(e)
            continue
        # 401 = credits exhausted / bad API key. Hard fail.
        if r.status_code == 401:
            raise ScraperFailure(
                "ScrapingBee returned 401 — credits exhausted or invalid key"
            )
        if r.status_code == 200 and not _INTERSTITIAL_RE.search(r.text):
            return r.text
        last_err = f"status {r.status_code}: {r.text[:150]}"
    print(f"  [controller] ScrapingBee failed after 3 attempts — {last_err}", file=sys.stderr)
    return None


def _fetch_via_curl(url: str) -> str | None:
    r = cr.get(url, impersonate="chrome", timeout=30)
    if r.status_code != 200 or _INTERSTITIAL_RE.search(r.text):
        return None
    return r.text


def _fetch_via_playwright(url: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_selector("a[href*='/listing/']", timeout=15000)
            except Exception:
                pass
            for _ in range(4):
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(500)
            html = page.content()
            browser.close()
            if _INTERSTITIAL_RE.search(html):
                return None
            return html
    except Exception as e:
        print(f"  [controller] Playwright fallback failed: {type(e).__name__}", file=sys.stderr)
        return None


def scrape(search: dict) -> list[Listing]:
    url = search["url"]
    html = (
        _fetch_via_scrapingbee(url)
        or _fetch_via_curl(url)
        or _fetch_via_playwright(url)
    )
    if html is None:
        if not os.environ.get("SCRAPINGBEE_API_KEY"):
            print(
                "  [controller] blocked by Imperva — set SCRAPINGBEE_API_KEY "
                "in .env to enable this source (free trial at scrapingbee.com)",
                file=sys.stderr,
            )
            return []
        # Key is set, so this is a real failure (challenge page or exhausted
        # credits), not an unconfigured source. Raise rather than returning []:
        # an empty list reads as "no listings for this make" and would drop
        # every previously-seen aircraft, while ScraperFailure makes scrape.py
        # carry the previous rows forward — same contract Trade-A-Plane uses.
        raise ScraperFailure(
            f"Controller fetch failed for {url} (bot challenge or credits)"
        )

    save_raw(f"{SOURCE}_{search['slug']}", html)

    # Combine both data sources — DOM cards have total_time/location, JSON-LD
    # is more stable across renders. Merge by URL, preferring the DOM record
    # (it has more fields) and filling missing fields from the JSON-LD record.
    dom = {l.url: l for l in _parse_dom_cards(html, search)}
    for jl in _parse_offers_jsonld(html, search):
        if jl.url in dom:
            merged = dom[jl.url]
            for field in ("year", "model", "price", "total_time", "location", "title", "description", "image_url", "engine", "engine_time"):
                if not getattr(merged, field) and getattr(jl, field):
                    setattr(merged, field, getattr(jl, field))
        else:
            dom[jl.url] = jl
    return list(dom.values())


def _parse_offers_jsonld(html: str, search: dict) -> list[Listing]:
    """Parse the inline JSON-LD `offers` array (the SEO/structured-data source).

    More stable across ScrapingBee renders than DOM scraping. Each offer's
    `itemOffered` is a schema.org Product with name/model/description, plus
    sibling `price` and `availableAtOrFrom` (location).
    """
    m = re.search(r'"offers":\s*\[', html)
    if not m:
        return []
    # Brace-walk to find the matching ]
    start = m.end() - 1
    depth = 0
    end = None
    for j, c in enumerate(html[start:], start=start):
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end is None:
        return []
    try:
        offers = json.loads(html[start:end])
    except json.JSONDecodeError:
        return []

    listings: list[Listing] = []
    seen: set[str] = set()
    year_re = re.compile(r"((?:19|20)\d{2})")
    for offer in offers:
        item = offer.get("itemOffered") or {}
        raw_url = offer.get("url") or item.get("@id")
        if not raw_url:
            continue
        url = urljoin(BASE, raw_url)
        if url in seen:
            continue
        seen.add(url)
        img_field = item.get("image")
        if isinstance(img_field, list) and img_field:
            img_url = img_field[0]
        elif isinstance(img_field, str):
            img_url = img_field
        else:
            img_url = None
        name = item.get("name") or ""
        year_m = year_re.search(name)
        loc = offer.get("availableAtOrFrom") or {}
        if isinstance(loc, dict):
            addr = loc.get("address") or {}
            city = addr.get("addressLocality") if isinstance(addr, dict) else None
            region = addr.get("addressRegion") if isinstance(addr, dict) else None
            location = ", ".join(p for p in (city, region) if p) or None
        else:
            location = None
        price = offer.get("price")
        price_str = f"${int(price):,}" if isinstance(price, (int, float)) and price else None
        model = item.get("model") or search.get("default_model")
        desc = item.get("description") or ""
        listings.append(
            Listing(
                source=SOURCE,
                url=url,
                make=search["make"],
                year=int(year_m.group(1)) if year_m else None,
                model=model,
                price=price_str,
                total_time=None,  # not in JSON-LD; would need detail page
                location=location,
                title=name or None,
                description=desc[:800] or None,
                image_url=img_url,
                engine=extract_engine(desc, model),
                engine_time=extract_engine_time(desc),
            )
        )
    return listings


def _parse_dom_cards(html: str, search: dict) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    listings: list[Listing] = []
    seen: set[str] = set()

    make_pat = search["title_make_pattern"]
    # Capture the next 1-2 alphanumeric model tokens, but stop before category
    # words like "Piston", "Single", "Amphibious", etc.
    title_re = re.compile(
        rf"((?:19|20)\d{{2}})\s+{make_pat}\s+"
        rf"((?!Piston|Single|Amphibious|Aircraft|Floatplanes)[A-Z0-9\-]+"
        rf"(?:\s+(?!Piston|Single|Amphibious|Aircraft|Floatplanes)[A-Z0-9\-]+)?)",
        re.I,
    )
    price_re = re.compile(r"USD\s*\$\s*([\d,]+)")
    tt_re = re.compile(r"Total Time\s*:\s*([\d,]+)")
    loc_re = re.compile(r"Location\s*:\s*([^\n]+?)\s*(?:Email|Phone|Seller|\(|$)")

    for card in soup.select("div.listing-data-selector"):
        a = card.select_one("a[href*='/listing/for-sale/']")
        if not a:
            continue
        href = a.get("href", "")
        listing_url = urljoin(BASE, href.split("?")[0])
        if listing_url in seen:
            continue
        seen.add(listing_url)

        text = card.get_text(" ", strip=True)

        title_m = title_re.search(text)
        year = int(title_m.group(1)) if title_m else None
        model = (
            title_m.group(2).strip().title() if title_m else search.get("default_model")
        )

        price_m = price_re.search(text)
        tt_m = tt_re.search(text)
        loc_m = loc_re.search(text)

        img_el = card.find("img")
        img_url = None
        if img_el:
            img_url = img_el.get("data-src") or img_el.get("src")

        listings.append(
            Listing(
                source=SOURCE,
                url=listing_url,
                make=search["make"],
                year=year,
                model=model,
                price=f"${price_m.group(1)}" if price_m else None,
                total_time=f"{tt_m.group(1)} TT" if tt_m else None,
                location=loc_m.group(1).strip() if loc_m else None,
                title=title_m.group(0) if title_m else None,
                description=text[:800],
                image_url=img_url,
                engine=extract_engine(text, model),
                engine_time=extract_engine_time(text),
            )
        )
    return listings
