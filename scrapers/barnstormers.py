"""Scrape Barnstormers.com.

Barnstormers lists aircraft two ways on a category page:

1. "Featured" ads at the top, linked via ``adclick.php?...&adtitle=...``.
2. Regular classified listings in the main list, linked via
   ``/classified-{id}-{Title-Words}.html`` — each row renders as
   ``TITLE • PRICE • STATUS • description • Contact Name`` (bullets are the
   HTML entity ``&#149;``).

We parse BOTH and dedupe by listing id. The ``ad_keyword`` in the search
config filters rows to the make we want (a Barnstormers category can hold
parts and other makes — e.g. a propeller in the Bearhawk category).
"""
from __future__ import annotations

import html as _html
import re

import requests

from .common import (
    UA,
    Listing,
    extract_engine,
    extract_engine_time,
    first_price,
    first_year,
    save_raw,
)

SOURCE = "barnstormers"

# Featured ads: adclick.php?type=..._clicks&id=123&adtitle=Some-Title
_AD_RE = re.compile(
    r"adclick\.php\?type=[a-z_]+&id=(\d+)&adtitle=([^'\"&]+)",
    re.I,
)

# Regular classifieds. Each row is:
#   <a href='/classified-{id}-{slug}.html...'>TITLE</a> •
#   <span class='price'>$105,000</span> • <span class='action_phrase'>STATUS</span> •
#   <span class='body'>description...</span>
_CLASSIFIED_RE = re.compile(
    r"href=['\"]/classified-(\d+)-([^'\"]+?)\.html[^'\"]*['\"][^>]*>\s*([^<]{0,200})</a>",
    re.I,
)
_PRICE_SPAN_RE = re.compile(r"class=['\"]?price['\"]?[^>]*>\s*([^<]+)</span>", re.I)
_BODY_SPAN_RE = re.compile(r"class=['\"]?body['\"]?[^>]*>\s*([^<]+)</span>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def scrape(search: dict) -> list[Listing]:
    html = requests.get(search["url"], headers={"User-Agent": UA}, timeout=30).text
    save_raw(f"{SOURCE}_{search['slug']}", html)

    match_keyword = search["ad_keyword"].lower()
    default_model = search.get("default_model")
    listings: list[Listing] = []
    seen: set[str] = set()

    # ---- 1. Featured ads (adclick format) ----
    for ad_id, adtitle in _AD_RE.findall(html):
        if match_keyword not in adtitle.lower():
            continue
        if ad_id in seen:
            continue
        seen.add(ad_id)

        title = adtitle.replace("-", " ").strip()
        ctx_window = html[max(0, html.find(ad_id) - 200) : html.find(ad_id) + 800]
        listings.append(
            Listing(
                source=SOURCE,
                url=(
                    f"https://www.barnstormers.com/adclick.php?type="
                    f"featured_category_clicks&id={ad_id}&adtitle={adtitle}"
                ),
                make=search["make"],
                year=first_year(title) or first_year(ctx_window),
                model=default_model,
                price=first_price(ctx_window),
                title=title,
                description=ctx_window[:500],
                engine=extract_engine(ctx_window, default_model),
                engine_time=extract_engine_time(ctx_window),
            )
        )

    # ---- 2. Regular classified listings ----
    for m in _CLASSIFIED_RE.finditer(html):
        ad_id, slug, raw_title = m.group(1), m.group(2), m.group(3)
        title = _html.unescape(raw_title).strip() or slug.replace("-", " ").strip()

        if match_keyword not in (title.lower() + " " + slug.lower()):
            continue
        if ad_id in seen:
            continue
        seen.add(ad_id)

        # The price / status / description spans follow the title anchor; pull
        # them from a window of the row (rows are self-contained until the next).
        window = html[m.end() : m.end() + 900]
        price_m = _PRICE_SPAN_RE.search(window)
        body_m = _BODY_SPAN_RE.search(window)
        price = first_price(_html.unescape(price_m.group(1))) if price_m else None
        body = _html.unescape(body_m.group(1)).strip() if body_m else ""
        row_text = _html.unescape(_TAG_RE.sub(" ", window[:600]))

        listings.append(
            Listing(
                source=SOURCE,
                url=f"https://www.barnstormers.com/classified-{ad_id}-{slug}.html",
                make=search["make"],
                year=first_year(title) or first_year(body),
                model=default_model,
                price=price,
                title=title.title() if title.isupper() else title,
                description=(body or row_text)[:500],
                engine=extract_engine(body or row_text, default_model),
                engine_time=extract_engine_time(body or row_text),
            )
        )

    return listings
