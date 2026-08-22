"""Scrape Trade-A-Plane (curl_cffi Chrome impersonation)."""
from __future__ import annotations

import re
import sys
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

from .common import (
    Listing,
    ScraperFailure,
    extract_engine,
    extract_engine_time,
    fetch_via_scrapingbee,
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
# TAP's "Results Shown" dropdown offers 12/24/48/72/96; 24 is the default. We
# ask for the largest, because every page now costs a stealth-proxy request —
# at 96 nearly every search fits in a single fetch instead of two or three.
PAGE_SIZE = 96
MAX_PAGES = 10  # safety cap


def _is_challenge(html: str) -> bool:
    """True if `html` is a bot-challenge interstitial rather than real content.

    TAP moved behind AWS WAF Bot Control, which answers with HTTP 202 and a
    tiny JS challenge page instead of an error status — so status alone no
    longer tells us we were blocked.

    Match on `gokuProps`, the config blob unique to the interstitial. Do NOT
    match "awswaf": successful pages load the WAF's token script in their own
    <head>, so that substring is present on perfectly good responses too.
    """
    head = html.lower()[:5000]
    return "captcha" in head or "gokuprops" in head


def _is_blocked(r) -> bool:
    return r.status_code != 200 or _is_challenge(r.text)


_SINGLE_LISTING_RE = re.compile(
    r'<link rel="canonical" href="[^"]*listing_id=\d+|og:url"[^>]*content="[^"]*listing_id=\d+'
)


def _is_single_listing(html: str) -> bool:
    """True if a search redirected to one listing's detail page.

    TAP does this when a make has exactly one active listing. Such a page has
    no count widget, so it must be recognised separately or _is_complete()
    would write it off as a truncated response.
    """
    return _SINGLE_LISTING_RE.search(html) is not None


def _is_complete(html: str) -> bool:
    """True if `html` is a fully-rendered search page (or a single-listing one).

    ScrapingBee intermittently returns a 200 containing only TAP's <head> —
    no listings, no footer, ~2KB. That is indistinguishable from "this search
    has no matches" unless we look for the "N - M of T" counter, which every
    real search page carries (a genuinely empty search renders "0 - 0 of 0").
    Without this check a stub response reads as zero listings, and since zero
    isn't a hard failure it would silently delete that make's rows.
    """
    return _COUNT_RE.search(html) is not None or _is_single_listing(html)


def _get(url: str) -> str | None:
    """Fetch a TAP page, falling back to ScrapingBee's stealth tier.

    Direct curl_cffi is free and still works from some IPs, so we always try
    it first. Solving the WAF challenge needs the 75-credit stealth tier —
    "classic"/"premium" get served the interstitial. render_js is off because
    the stealth proxy clears the challenge itself, which is much faster and
    costs the same.
    """
    try:
        r = cr.get(url, impersonate="chrome", timeout=30)
        if not _is_blocked(r) and _is_complete(r.text):
            return r.text
    except Exception:
        pass

    # Stubs arrive in short bursts, so backing off further each time beats
    # retrying immediately. They're billed as 200s, so cap the loop.
    rejects: list[str] = []
    for attempt in range(4):
        if attempt:
            time.sleep(3 * 2 ** (attempt - 1))  # 3s, 6s, 12s
        html = fetch_via_scrapingbee(url, tier="stealth")
        if html is None:
            print(f"  [{SOURCE}] ScrapingBee gave up on {url}", file=sys.stderr)
            return None
        if _is_challenge(html):
            rejects.append("challenge")
        elif not _is_complete(html):
            rejects.append(f"stub({len(html)}b)")
        else:
            return html
    # Which one it was decides the fix (rotate impersonation vs. retry harder),
    # and this is the only place that can tell them apart.
    print(f"  [{SOURCE}] rejected {', '.join(rejects)} for {url}", file=sys.stderr)
    return None


def _page_url(base_url: str, page: int) -> str:
    """Build the URL for page N — /search?...&s-page_size=96[&s-page=N].

    Mirrors what TAP's own "Results Shown" dropdown links to. Page 1 goes
    through here too so that every request asks for the same page size;
    otherwise the count widget would describe a 24-per-page result set while
    we paginate a 96-per-page one.
    """
    paged = base_url.replace("/filtered/search?", "/search?", 1)
    if "s-page_size=" in paged:
        paged = re.sub(r"s-page_size=\d+", f"s-page_size={PAGE_SIZE}", paged)
    else:
        paged = paged + ("&" if "?" in paged else "?") + f"s-page_size={PAGE_SIZE}"
    if page > 1:
        paged += f"&s-page={page}"
    return paged


_DETAIL_LOC_RE = re.compile(r"located in (.+?)\s+([A-Z]{2})\s+from", re.I)


def _parse_single_listing(html: str, search: dict) -> list[Listing]:
    """Parse a search that redirected to one listing's detail page.

    When a make has exactly one active listing, TAP skips the results page
    and serves the listing itself — no `result_listing` cards and no count
    widget. Rather than treat that as an unreachable source (which would pin
    the make to stale rows forever), read the one listing out of the page's
    meta tags.
    """
    soup = BeautifulSoup(html, "lxml")

    def _meta(key: str) -> str | None:
        tag = soup.find("meta", property=key) or soup.find("meta", attrs={"name": key})
        return tag.get("content") if tag else None

    canonical = soup.find("link", rel="canonical")
    href = (canonical.get("href") if canonical else None) or _meta("og:url") or ""
    m = _LISTING_ID_RE.search(href)
    if not m:
        return []
    url = f"{BASE}/search?listing_id={m.group(1)}&s-type=aircraft"

    title = (_meta("og:title") or "").strip()
    # og:title is "<year> <make> <model> ... for sale - <listing_id>"
    title = re.sub(r"\s*-\s*\d+$", "", title)
    title = re.sub(r"\s+for sale\s*$", "", title, flags=re.I).strip()

    desc = _meta("description") or ""
    loc_m = _DETAIL_LOC_RE.search(desc)
    location = f"{loc_m.group(1).strip()}, {loc_m.group(2).upper()}" if loc_m else None

    text = soup.get_text(" ", strip=True)
    model = _model_from_url(href) or search.get("default_model")

    return [
        Listing(
            source=SOURCE,
            url=url,
            make=search["make"],
            year=first_year(title),
            model=model,
            price=first_price(text),
            total_time=first_hours(text),
            location=location,
            title=title[:200] or None,
            description=text[:800],
            image_url=_meta("og:image"),
            engine=extract_engine(text, model),
            engine_time=extract_engine_time(text),
        )
    ]


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
    # Page 1 — 96 results per page, so most searches need only this one fetch.
    html = _get(_page_url(search["url"], 1))
    if html is None:
        raise ScraperFailure(
            "Trade-A-Plane bot-challenge not cleared (direct + ScrapingBee stealth)"
        )
    save_raw(f"{SOURCE}_{search['slug']}", html)

    # A one-result search lands on the listing itself — nothing to paginate.
    if _is_single_listing(html):
        return _parse_single_listing(html, search)

    seen: set[str] = set()
    listings = _parse_page(html, search, seen)

    # Look up the total count from the "1 - 96 of N" widget so we know how
    # many pages exist. Stop early if cap reached or a page returns no new
    # listings (defensive).
    m = _COUNT_RE.search(html)
    total = int(m.group(3)) if m else len(listings)
    n_pages = min(MAX_PAGES, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    for page in range(2, n_pages + 1):
        time.sleep(2.5)  # be polite — TAP rate-limits aggressively
        page_url = _page_url(search["url"], page)
        page_html = _get(page_url)
        if page_html is None:
            # Partial data is better than wiping everything — return what we have.
            print(
                f"  [{SOURCE}] {search['make']} page {page}: rate-limited, "
                f"keeping {len(listings)}/{total} listings",
                file=sys.stderr,
            )
            break
        before = len(listings)
        listings.extend(_parse_page(page_html, search, seen))
        if len(listings) == before:
            break

    return listings
