"""Scrape Barnstormers.com.

Barnstormers lists aircraft two ways on a category page:

1. "Featured" ads at the top, linked via ``adclick.php?...&adtitle=...``.
2. Regular classified listings in the main list, linked via
   ``/classified-{id}-{Title-Words}.html`` — each row renders as
   ``TITLE • PRICE • STATUS • description • Contact Name`` (bullets are the
   HTML entity ``&#149;``).

We parse BOTH and dedupe by listing id. The ``ad_keyword`` (substring) or
``ad_pattern`` (regex) in the search config filters rows to the make we want —
a Barnstormers category holds parts, services and other makes too (a propeller
in the Bearhawk category, a ferry-pilot ad under Bellanca).

A search may name several categories via ``urls``; Barnstormers cross-files the
same airframe under Cessna--, Antique-Classic-- and Taildragger--, so listings
are deduped by id across all of a search's pages.
"""
from __future__ import annotations

import html as _html
import re
import sys

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
# Each classified row ends with a <table class='thumbtable'> holding the ad's
# photos. Roughly half of Barnstormers ads have one; the rest are text-only.
_THUMB_RE = re.compile(
    r"thumbtable.*?<img[^>]+src=['\"]"
    r"(https://barnstormers\.s3\.amazonaws\.com/media/listing_images/thumbnail/[^'\"]+)",
    re.S,
)

def _matcher(search: dict):
    """Return a predicate deciding whether an ad title belongs to this make.

    ``ad_pattern`` (a regex) handles makes whose listings are spelled
    inconsistently — live Decathlon ads appear as "DECATHALON", and the model
    designation 8KCAB is routinely transposed to "8CKAB". ``ad_keyword`` keeps
    the plain substring test the original single-category searches rely on.
    """
    pattern = search.get("ad_pattern")
    if pattern:
        return re.compile(pattern, re.I).search
    keyword = search["ad_keyword"].lower()
    return lambda text: keyword in text.lower()


def _parse_category(
    page: str, search: dict, matches, seen: set[str]
) -> list[Listing]:
    """Parse one category page. `seen` is shared across pages and mutated."""
    default_model = search.get("default_model")
    listings: list[Listing] = []

    # ---- 1. Featured ads (adclick format) ----
    for ad_id, adtitle in _AD_RE.findall(page):
        if not matches(adtitle):
            continue
        if ad_id in seen:
            continue
        seen.add(ad_id)

        title = adtitle.replace("-", " ").strip()
        ctx_window = page[max(0, page.find(ad_id) - 200) : page.find(ad_id) + 800]
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
    rows_found = list(_CLASSIFIED_RE.finditer(page))
    for idx, m in enumerate(rows_found):
        ad_id, slug, raw_title = m.group(1), m.group(2), m.group(3)
        title = _html.unescape(raw_title).strip() or slug.replace("-", " ").strip()

        if not matches(title + " " + slug):
            continue
        if ad_id in seen:
            continue
        seen.add(ad_id)

        # The price / status / description spans follow the title anchor; pull
        # them from a window of the row (rows are self-contained until the next).
        window = page[m.end() : m.end() + 900]
        # The thumbnail sits further down the row than the text spans, so scan
        # to the START OF THE NEXT ROW rather than a fixed offset — a fixed
        # window would attach the following listing's photo to this one.
        row_end = rows_found[idx + 1].start() if idx + 1 < len(rows_found) else len(page)
        thumb_m = _THUMB_RE.search(page[m.end() : row_end])
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
                image_url=thumb_m.group(1) if thumb_m else None,
                engine=extract_engine(body or row_text, default_model),
                engine_time=extract_engine_time(body or row_text),
            )
        )

    return listings


def scrape(search: dict) -> list[Listing]:
    """Scrape every category page configured for this search.

    Barnstormers cross-files one airframe under several categories (a Skywagon
    sits under Cessna--, Antique-Classic-- and Taildragger--), so `urls` is the
    normal case; `seen` spans all pages so each listing is kept exactly once.
    """
    urls = search.get("urls") or [search["url"]]
    matches = _matcher(search)
    seen: set[str] = set()
    listings: list[Listing] = []

    for i, url in enumerate(urls):
        try:
            page = requests.get(url, headers={"User-Agent": UA}, timeout=30).text
        except requests.RequestException as e:
            # One dead category shouldn't lose the other categories' listings.
            print(f"    barnstormers {search['slug']}: {url} failed — {e}", file=sys.stderr)
            continue
        suffix = search["slug"] if len(urls) == 1 else f"{search['slug']}_{i}"
        save_raw(f"{SOURCE}_{suffix}", page)
        listings.extend(_parse_category(page, search, matches, seen))

    return listings
