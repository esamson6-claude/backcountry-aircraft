"""Run all source scrapers, dedupe, write CSV, and emit a diff of new listings."""
from __future__ import annotations

import csv
import importlib
import re
import sys
import traceback
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CSV_PATH = DATA_DIR / "listings.csv"
NEW_PATH = DATA_DIR / "new_listings.md"

# Per-listing post-filters. Returns True to keep.
def _keep_husky_180_or_c(l: dict) -> bool:
    """Keep only Aviat Husky A-1B (180) and A-1C variants (180 and 200)."""
    blob = " ".join(
        [(l.get(k) or "").upper() for k in ("model", "description", "title")]
    )
    return bool(re.search(r"\bA-1B\b|\bA-1C\b", blob))


def _keep_dhc2(l: dict) -> bool:
    """DEHAVILLAND+DHC+SERIES includes DHC-1/2/3/6; keep only DHC-2 Beaver."""
    blob = " ".join(
        [(l.get(k) or "").upper() for k in ("model", "description", "title")]
    )
    return "DHC-2" in blob


def _keep_rans_s6_s7_s20(l: dict) -> bool:
    blob = " ".join((l.get(k) or "").upper() for k in ("model", "description", "title"))
    return bool(re.search(r"\bS[-\s]?(?:6|7|20)\b", blob))


def _keep_zenith_701_750_801(l: dict) -> bool:
    blob = " ".join((l.get(k) or "").upper() for k in ("model", "description", "title"))
    return bool(re.search(r"\bCH[-\s]?(?:701|750|801)\b", blob))


def _keep_pa18(l: dict) -> bool:
    blob = " ".join((l.get(k) or "").upper() for k in ("model", "description", "title"))
    return bool(re.search(r"\bPA[-\s]?18\b|SUPER\s*CUB", blob))


def _keep_kitplanes_for_africa(l: dict) -> bool:
    blob = " ".join((l.get(k) or "").upper() for k in ("model", "description", "title"))
    return bool(re.search(r"EXPLORER|SAFARI|KITPLANES", blob))


def _keep_arctic_tern(l: dict) -> bool:
    blob = " ".join((l.get(k) or "").upper() for k in ("model", "description", "title"))
    return "ARCTIC" in blob and "TERN" in blob


# Buy-side solicitations ("Wanted - CESSNA 172 ...", WTB, ISO, "looking to buy")
# are people seeking to purchase, not aircraft for sale. Drop them site-wide.
# Anchored at the START of the title/description so it can't catch a real
# for-sale listing that merely mentions the word somewhere in its blurb
# (those start with a year/price, e.g. "1958 CESSNA 172 $85,000").
_SOLICITATION_RE = re.compile(
    r"^\s*(wanted\b|w\.?\s?t\.?\s?b\.?\b|want(ed)?\s+to\s+buy|wish\s+to\s+buy|"
    r"in\s+search\s+of\b|iso\b|looking\s+(to\s+buy|for\s+a)|we\s+buy\b|cash\s+for\b)",
    re.I,
)

# Unambiguous buy-side phrases that disqualify a listing wherever they appear in
# the TITLE (not just at the start) — catches "Cessna 172 - We Buy Planes!".
# Title-only on purpose: real for-sale listings sometimes carry dealer boilerplate
# like "we buy / consignment" deep in their DESCRIPTION, so we don't scan that
# (the ^-anchored _SOLICITATION_RE still covers descriptions that *open* with it).
_TITLE_SOLICITATION_RE = re.compile(
    r"\b(we\s+buy|we\s+purchase|wanted\b|w\.?\s?t\.?\s?b\.?\b|"
    r"want(ed)?\s+to\s+buy|wish\s+to\s+buy|looking\s+to\s+buy|will\s+buy|"
    r"cash\s+for\b|in\s+search\s+of\b|\biso\b)",
    re.I,
)


# Buy-side dealer ads ("We pay cash for your plane", "Is your Cessna just
# sitting in the hangar?", "ready for lease") use a normal-looking title but a
# solicitation body, so anchoring fails. These phrases are unambiguous and don't
# appear in genuine for-sale write-ups (a real listing's "if you ever wanted..."
# won't match). Applied only when the listing has NO price — a real for-sale
# listing that merely mentions a buy-back program still carries its own price.
_BUYSIDE_RE = re.compile(
    r"\bwe\s+buy\b|we'?re\s+buyers|we\s+are\s+buyers|we\s+pay\s+cash|"
    r"we\s+purchase\b|we'?ll\s+buy\b|sell\s+us\s+your|sell\s+the\s+smart\s+way|"
    r"cash\s+for\s+your|turn\s+your\s+(?:plane|aircraft|airplane)\s+into|"
    r"skip\s+the\s+tire\s+kickers|just\s+sitting\s+in\s+(?:the|your)\s+hangar",
    re.I,
)


def _is_solicitation(l: dict) -> bool:
    """True for buy-side / lease ads (not aircraft genuinely for sale)."""
    title = (l.get("title") or "").strip()
    if _TITLE_SOLICITATION_RE.search(title):
        return True
    if any(_SOLICITATION_RE.match((l.get(k) or "").strip()) for k in ("title", "description")):
        return True
    # Buy-side/lease body phrases, but only when there's no real price.
    has_price = bool(re.search(r"\d{4,}", (l.get("price") or "")))
    if not has_price and _BUYSIDE_RE.search(f"{title} {l.get('description') or ''}"):
        return True
    return False


# Non-US listings — this project targets the US market. Foreign rows carry a
# country name (or a Canadian province code like ", QC") in the location, while
# US rows use "City, ST". We match country names + Canadian province codes only,
# which avoids false positives like "Ontario, CA" (California) or
# "West Columbia, SC" (South Carolina).
_FOREIGN_COUNTRY_RE = re.compile(
    r"\b(canada|australia|united kingdom|england|scotland|wales|ireland|"
    r"germany|france|south africa|new zealand|netherlands|switzerland|austria|"
    r"belgium|sweden|norway|denmark|finland|spain|italy|portugal|poland|"
    r"czech|mexico|brazil|argentina|japan|china|india)\b",
    re.I,
)
# Canadian province/territory codes as the trailing token — none collide with
# USPS state codes.
_CA_PROVINCE_RE = re.compile(
    r",\s*(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\s*$", re.I
)


def _is_foreign(l: dict) -> bool:
    """True for clearly non-US listings (dropped — this is a US-market site).

    Unknown/blank locations are kept (return False) so we don't over-filter.
    """
    loc = (l.get("location") or "").strip()
    if not loc:
        return False
    return bool(_FOREIGN_COUNTRY_RE.search(loc) or _CA_PROVINCE_RE.search(loc))


def _keep_bird_dog(l: dict) -> bool:
    """Cessna L-19 / O-1 Bird Dog (also marketed as Cessna 305A).

    Catches: L-19, L19, L-19A/E, O-1, O-1A/E/F/G, "Bird Dog", 305A.
    """
    blob = " ".join((l.get(k) or "").upper() for k in ("model", "description", "title"))
    return bool(
        re.search(r"\bL[-\s]?19|\bO[-\s]?1[A-Z]?\b|BIRD\s*DOG|\b305A?\b", blob)
    )


def _keep_cessna_170_175(l: dict) -> bool:
    """Keep only Cessna 170/175 piston singles.

    Controller's /cessna/170 and /cessna/175 URLs fall back to the full Cessna
    inventory (incl. Citation jets), and title_make_pattern="CESSNA" doesn't
    filter. We check title/description (NOT model — jets fall back to the
    "170/175" default_model) and require a real 170/175 token while excluding
    Citations.
    """
    blob = " ".join((l.get(k) or "").upper() for k in ("title", "description"))
    if "CITATION" in blob:
        return False
    return bool(re.search(r"\b170[A-C]?\b|\b175[A-C]?\b|SKYLARK", blob))


def _keep_wilga(l: dict) -> bool:
    """PZL-104 Wilga family (Wilga 35/80, PZL-104MA Wilga 2000, 104MF, 2000 Patrol).

    Used on the broad source entries (TAP make=PZL, AircraftForSale /pzl) to
    exclude other PZL types (gliders, Koliber, Iskra) that share the make.
    """
    blob = " ".join((l.get(k) or "").upper() for k in ("model", "description", "title"))
    return "WILGA" in blob or bool(re.search(r"PZL[-\s]?104", blob))




# Each search is one (source, make, url) combination.
SEARCHES: list[dict] = [
    # ---- Aviat Husky ----
    {
        "make": "Aviat Husky",
        "module": "scrapers.aviat",
        "slug": "aviat-husky",
        "url": None,  # ignored; aviat.py hardcodes its own URL
        "default_model": "Husky",
        "post_filter": _keep_husky_180_or_c,
    },
    {
        "make": "Aviat Husky",
        "module": "scrapers.aircraftforsale",
        "slug": "aviat-husky",
        "sitemap_patterns": ["/aviat/a-1"],
        "default_model": "Husky",
        "post_filter": _keep_husky_180_or_c,
    },
    {
        "make": "Aviat Husky",
        "module": "scrapers.barnstormers",
        "slug": "aviat-husky",
        "url": "https://www.barnstormers.com/category-24045-Aviat-Aircraft.html",
        "ad_keyword": "husky",
        "default_model": "Husky",
        "post_filter": _keep_husky_180_or_c,
    },
    {
        "make": "Aviat Husky",
        "module": "scrapers.trade_a_plane",
        "slug": "aviat-husky",
        "url": (
            "https://www.trade-a-plane.com/filtered/search?make=AVIAT"
            "&model_group=AVIAT+HUSKY+SERIES&s-type=aircraft"
        ),
        "default_model": "Husky",
        "post_filter": _keep_husky_180_or_c,
    },
    {
        "make": "Aviat Husky",
        "module": "scrapers.controller",
        "slug": "aviat-husky",
        "url": "https://www.controller.com/listings/for-sale/aviat/husky/aircraft",
        "title_make_pattern": "AVIAT\\s+HUSKY",
        "default_model": "Husky",
        "post_filter": _keep_husky_180_or_c,
    },
    # ---- Maule (all models) ----
    {
        "make": "Maule",
        "module": "scrapers.aircraftforsale",
        "slug": "maule",
        "sitemap_patterns": ["/maule/"],
        "default_model": "Maule",
    },
    {
        "make": "Maule",
        "module": "scrapers.trade_a_plane",
        "slug": "maule",
        "url": "https://www.trade-a-plane.com/filtered/search?make=MAULE&s-type=aircraft",
        "default_model": "Maule",
    },
    {
        "make": "Maule",
        "module": "scrapers.controller",
        "slug": "maule",
        "url": "https://www.controller.com/listings/for-sale/maule/aircraft",
        "title_make_pattern": "MAULE",
        "default_model": "Maule",
    },

    # ---- CubCrafters (XCub, CarbonCub EX, Top Cub, Sport Cub, Carbon Cub LSA) ----
    {
        "make": "CubCrafters",
        "module": "scrapers.trade_a_plane",
        "slug": "cubcrafters",
        "url": "https://www.trade-a-plane.com/filtered/search?make=CUBCRAFTERS&s-type=aircraft",
        "default_model": "CubCrafters",
    },
    {
        "make": "CubCrafters",
        "module": "scrapers.aircraftforsale",
        "slug": "cubcrafters",
        "sitemap_patterns": ["/cubcrafters/", "/cub-crafters/"],
        "default_model": "CubCrafters",
    },

    # ---- American Champion (7GCBC Citabria + 8GCBC Scout) ----
    {
        "make": "American Champion",
        "module": "scrapers.trade_a_plane",
        "slug": "american-champion",
        "url": "https://www.trade-a-plane.com/filtered/search?make=AMERICAN+CHAMPION&s-type=aircraft",
        "default_model": "American Champion",
    },
    {
        "make": "American Champion",
        "module": "scrapers.aircraftforsale",
        "slug": "american-champion",
        "sitemap_patterns": ["/american-champion/", "/aeronca/", "/champion/"],
        "default_model": "American Champion",
    },

    # ---- Bearhawk (LSA + Patrol) ----
    {
        "make": "Bearhawk",
        "module": "scrapers.trade_a_plane",
        "slug": "bearhawk",
        "url": "https://www.trade-a-plane.com/filtered/search?make=BEARHAWK&s-type=aircraft",
        "default_model": "Bearhawk",
    },
    {
        "make": "Bearhawk",
        "module": "scrapers.aircraftforsale",
        "slug": "bearhawk",
        "sitemap_patterns": ["/bearhawk/"],
        "default_model": "Bearhawk",
    },
    {
        # Barnstormers is the primary marketplace for homebuilt/experimental
        # aircraft like Bearhawk. Category 18715 is the dedicated Bearhawk page.
        "make": "Bearhawk",
        "module": "scrapers.barnstormers",
        "slug": "bearhawk",
        "url": "https://www.barnstormers.com/category-18715-Experimental--Bearhawk.html",
        "ad_keyword": "bearhawk",
        "default_model": "Bearhawk",
    },
    {
        "make": "Bearhawk",
        "module": "scrapers.controller",
        "slug": "bearhawk",
        "url": "https://www.controller.com/listings/for-sale/bearhawk/aircraft",
        "title_make_pattern": "BEARHAWK",
        "default_model": "Bearhawk",
    },

    # ---- BushCaddy L162 ----
    {
        "make": "BushCaddy",
        "module": "scrapers.trade_a_plane",
        "slug": "bushcaddy",
        "url": "https://www.trade-a-plane.com/filtered/search?make=BUSH+CADDY&s-type=aircraft",
        "default_model": "BushCaddy",
    },
    {
        "make": "BushCaddy",
        "module": "scrapers.aircraftforsale",
        "slug": "bushcaddy",
        "sitemap_patterns": ["/bushcaddy/", "/bush-caddy/"],
        "default_model": "BushCaddy",
    },

    # ---- Cessna L-19 Bird Dog (O-1, 305A — military observation) ----
    {
        "make": "Cessna L-19 Bird Dog",
        "module": "scrapers.trade_a_plane",
        "slug": "cessna-l19",
        "url": "https://www.trade-a-plane.com/filtered/search?make=CESSNA&model_group=CESSNA+L19+SERIES&s-type=aircraft",
        "default_model": "L-19 Bird Dog",
    },
    {
        "make": "Cessna L-19 Bird Dog",
        "module": "scrapers.aerotrader",
        "slug": "cessna-l19",
        "at_make": "Cessna",
        "at_patterns": ["l-19", "l19", "bird-dog", "birddog", "o-1", "-305a-", "-305-"],
        "default_model": "L-19 Bird Dog",
        "post_filter": _keep_bird_dog,
    },
    {
        "make": "Cessna L-19 Bird Dog",
        "module": "scrapers.vanbortel",
        "slug": "cessna-l19",
        "vb_patterns": ["cessna-l-19", "cessna-l19", "cessna-o-1", "cessna-305", "bird-dog"],
        "default_model": "L-19 Bird Dog",
    },
    {
        "make": "Cessna L-19 Bird Dog",
        "module": "scrapers.aircraftcom",
        "slug": "cessna-l19",
        # aircraft.com listings discovered manually. Append more URLs here
        # as they're found (the site has no enumerable category surface).
        "urls": [
            "https://www.aircraft.com/aircraft/197823883/n432wc-1951-cessna-l19-305a-bird-dog",
        ],
        "default_model": "L-19 Bird Dog",
    },

    # ---- Cessna 170/175 ----
    {
        "make": "Cessna 170/175",
        "module": "scrapers.trade_a_plane",
        "slug": "cessna-170",
        "url": "https://www.trade-a-plane.com/filtered/search?make=CESSNA&model_group=CESSNA+170+SERIES&s-type=aircraft",
        "default_model": "170",
    },
    {
        "make": "Cessna 170/175",
        "module": "scrapers.trade_a_plane",
        "slug": "cessna-175",
        "url": "https://www.trade-a-plane.com/filtered/search?make=CESSNA&model_group=CESSNA+175+SERIES&s-type=aircraft",
        "default_model": "175",
    },
    {
        "make": "Cessna 170/175",
        "module": "scrapers.controller",
        "slug": "cessna-170",
        "url": "https://www.controller.com/listings/for-sale/cessna/170/aircraft",
        "title_make_pattern": "CESSNA",
        "default_model": "170",
        "post_filter": _keep_cessna_170_175,
    },
    {
        "make": "Cessna 170/175",
        "module": "scrapers.controller",
        "slug": "cessna-175",
        "url": "https://www.controller.com/listings/for-sale/cessna/175/aircraft",
        "title_make_pattern": "CESSNA",
        "default_model": "175",
        "post_filter": _keep_cessna_170_175,
    },
    {
        "make": "Cessna 170/175",
        "module": "scrapers.aircraftforsale",
        "slug": "cessna-170-175",
        "sitemap_patterns": ["/cessna/170", "/cessna/175"],
        "default_model": "170/175",
    },
    {
        "make": "Cessna 170/175",
        "module": "scrapers.vanbortel",
        "slug": "cessna-170-175",
        "vb_patterns": ["cessna-170", "cessna-175"],
        "default_model": "170/175",
    },
    {
        "make": "Cessna 170/175",
        "module": "scrapers.aerotrader",
        "slug": "cessna-170-175",
        "at_make": "Cessna",
        "at_patterns": ["cessna-170", "cessna+170", "cessna-175", "cessna+175"],
        "default_model": "170/175",
    },

    # ---- Cessna 180 ----
    {
        "make": "Cessna 180",
        "module": "scrapers.trade_a_plane",
        "slug": "cessna-180",
        "url": "https://www.trade-a-plane.com/filtered/search?make=CESSNA&model_group=CESSNA+180+SERIES&s-type=aircraft",
        "default_model": "180",
    },
    {
        "make": "Cessna 180",
        "module": "scrapers.aircraftforsale",
        "slug": "cessna-180",
        "sitemap_patterns": ["/cessna/180"],
        "default_model": "180",
    },

    # ---- Cessna 185 ----
    {
        "make": "Cessna 185",
        "module": "scrapers.trade_a_plane",
        "slug": "cessna-185",
        "url": "https://www.trade-a-plane.com/filtered/search?make=CESSNA&model_group=CESSNA+185+SERIES&s-type=aircraft",
        "default_model": "185",
    },
    {
        "make": "Cessna 185",
        "module": "scrapers.aircraftforsale",
        "slug": "cessna-185",
        "sitemap_patterns": ["/cessna/185"],
        "default_model": "185",
    },
    {
        "make": "Cessna 185",
        "module": "scrapers.vanbortel",
        "slug": "cessna-185",
        "vb_patterns": ["-a185", "cessna-185", "cessna-a185"],
        "default_model": "185",
    },

    # ---- Cessna 190/195 ----
    {
        "make": "Cessna 195",
        "module": "scrapers.trade_a_plane",
        "slug": "cessna-195",
        "url": "https://www.trade-a-plane.com/filtered/search?make=CESSNA&model_group=CESSNA+195+SERIES&s-type=aircraft",
        "default_model": "195",
    },
    {
        "make": "Cessna 195",
        "module": "scrapers.aircraftforsale",
        "slug": "cessna-195",
        "sitemap_patterns": ["/cessna/195", "/cessna/190"],
        "default_model": "195",
    },

    # ---- Just Aircraft (Highlander + SuperSTOL) ----
    {
        "make": "Just Aircraft",
        "module": "scrapers.trade_a_plane",
        "slug": "just-aircraft",
        "url": "https://www.trade-a-plane.com/filtered/search?make=JUST+AIRCRAFT&s-type=aircraft",
        "default_model": "Just Aircraft",
    },
    {
        "make": "Just Aircraft",
        "module": "scrapers.aircraftforsale",
        "slug": "just-aircraft",
        "sitemap_patterns": ["/just-aircraft/", "/just/"],
        "default_model": "Just Aircraft",
    },

    # ---- Kitfox (Super Sport 7 LSA) ----
    {
        "make": "Kitfox",
        "module": "scrapers.trade_a_plane",
        "slug": "kitfox",
        "url": "https://www.trade-a-plane.com/filtered/search?make=KITFOX&s-type=aircraft",
        "default_model": "Kitfox",
    },
    {
        "make": "Kitfox",
        "module": "scrapers.aircraftforsale",
        "slug": "kitfox",
        "sitemap_patterns": ["/kitfox/"],
        "default_model": "Kitfox",
    },

    # ---- Stinson 108 ----
    {
        "make": "Stinson 108",
        "module": "scrapers.trade_a_plane",
        "slug": "stinson-108",
        "url": "https://www.trade-a-plane.com/filtered/search?make=STINSON&s-type=aircraft",
        "default_model": "Stinson 108",
    },
    {
        "make": "Stinson 108",
        "module": "scrapers.aircraftforsale",
        "slug": "stinson-108",
        "sitemap_patterns": ["/stinson/108"],
        "default_model": "Stinson 108",
    },

    # ---- de Havilland DHC-2 Beaver ----
    {
        "make": "DHC-2 Beaver",
        "module": "scrapers.trade_a_plane",
        "slug": "dhc-2",
        "url": "https://www.trade-a-plane.com/filtered/search?make=DEHAVILLAND&model_group=DEHAVILLAND+DHC+SERIES&s-type=aircraft",
        "default_model": "DHC-2",
        "post_filter": _keep_dhc2,
    },
    {
        "make": "DHC-2 Beaver",
        "module": "scrapers.aircraftforsale",
        "slug": "dhc-2",
        "sitemap_patterns": ["/havilland/dhc-2", "/de-havilland/dhc-2", "/dhc-2/"],
        "default_model": "DHC-2",
    },

    # ---- Helio Courier ----
    {
        "make": "Helio Courier",
        "module": "scrapers.trade_a_plane",
        "slug": "helio",
        "url": "https://www.trade-a-plane.com/filtered/search?make=HELIO&s-type=aircraft",
        "default_model": "Courier",
    },
    {
        "make": "Helio Courier",
        "module": "scrapers.aircraftforsale",
        "slug": "helio",
        "sitemap_patterns": ["/helio/"],
        "default_model": "Courier",
    },

    # ---- Piper PA-18 Super Cub ----
    {
        "make": "Piper PA-18 Super Cub",
        "module": "scrapers.trade_a_plane",
        "slug": "piper-pa18",
        "url": "https://www.trade-a-plane.com/filtered/search?make=PIPER&model_group=PIPER+PA-18+SERIES&s-type=aircraft",
        "default_model": "PA-18 Super Cub",
    },
    {
        "make": "Piper PA-18 Super Cub",
        "module": "scrapers.controller",
        "slug": "piper-supercub",
        "url": "https://www.controller.com/listings/for-sale/piper/super-cub/aircraft",
        "title_make_pattern": "PIPER",
        "default_model": "PA-18 Super Cub",
    },
    {
        "make": "Piper PA-18 Super Cub",
        "module": "scrapers.aircraftforsale",
        "slug": "piper-pa18",
        "sitemap_patterns": ["/piper/pa-18", "/piper/super-cub"],
        "default_model": "PA-18 Super Cub",
    },

    # ---- Kitplanes for Africa (Explorer, Safari, Safari XL) ----
    # Drake Aircraft Adventures is the US dealer but only has spec pages,
    # no individual listings, so we scrape the secondary marketplaces.
    {
        "make": "Kitplanes for Africa",
        "module": "scrapers.trade_a_plane",
        "slug": "kitplanes-africa",
        "url": "https://www.trade-a-plane.com/filtered/search?make=KITPLANES+FOR+AFRICA&s-type=aircraft",
        "default_model": "Explorer/Safari",
        "post_filter": _keep_kitplanes_for_africa,
    },
    {
        "make": "Kitplanes for Africa",
        "module": "scrapers.controller",
        "slug": "kitplanes-africa",
        "url": "https://www.controller.com/listings/for-sale/kitplanes-for-africa/aircraft",
        "title_make_pattern": "KITPLANES",
        "default_model": "Explorer/Safari",
    },

    # ---- ICP Savannah ----
    {
        "make": "ICP Savannah",
        "module": "scrapers.trade_a_plane",
        "slug": "icp",
        "url": "https://www.trade-a-plane.com/filtered/search?make=ICP&s-type=aircraft",
        "default_model": "Savannah",
    },
    {
        "make": "ICP Savannah",
        "module": "scrapers.controller",
        "slug": "icp",
        "url": "https://www.controller.com/listings/for-sale/icp/aircraft",
        "title_make_pattern": "ICP",
        "default_model": "Savannah",
    },

    # ---- Murphy Rebel ----
    {
        "make": "Murphy Rebel",
        "module": "scrapers.trade_a_plane",
        "slug": "murphy",
        "url": "https://www.trade-a-plane.com/filtered/search?make=MURPHY&s-type=aircraft",
        "default_model": "Rebel",
    },
    {
        "make": "Murphy Rebel",
        "module": "scrapers.controller",
        "slug": "murphy",
        "url": "https://www.controller.com/listings/for-sale/murphy/aircraft",
        "title_make_pattern": "MURPHY",
        "default_model": "Rebel",
    },

    # ---- Nando Groppo Trial ----
    {
        "make": "Nando Groppo Trial",
        "module": "scrapers.trade_a_plane",
        "slug": "groppo",
        "url": "https://www.trade-a-plane.com/filtered/search?make=GROPPO&s-type=aircraft",
        "default_model": "Trial",
    },

    # ---- Rans (S6 Coyote, S7 Courier, S20 Raven) ----
    {
        "make": "Rans",
        "module": "scrapers.trade_a_plane",
        "slug": "rans",
        "url": "https://www.trade-a-plane.com/filtered/search?make=RANS&s-type=aircraft",
        "default_model": "Rans",
        "post_filter": _keep_rans_s6_s7_s20,
    },
    {
        "make": "Rans",
        "module": "scrapers.controller",
        "slug": "rans",
        "url": "https://www.controller.com/listings/for-sale/rans/aircraft",
        "title_make_pattern": "RANS",
        "default_model": "Rans",
        "post_filter": _keep_rans_s6_s7_s20,
    },

    # ---- Zenith (CH701, CH750, CH801) ----
    {
        "make": "Zenith",
        "module": "scrapers.trade_a_plane",
        "slug": "zenith",
        "url": "https://www.trade-a-plane.com/filtered/search?make=ZENITH&s-type=aircraft",
        "default_model": "Zenith",
        "post_filter": _keep_zenith_701_750_801,
    },
    {
        "make": "Zenith",
        "module": "scrapers.controller",
        "slug": "zenith",
        "url": "https://www.controller.com/listings/for-sale/zenith/aircraft",
        "title_make_pattern": "ZENITH",
        "default_model": "Zenith",
        "post_filter": _keep_zenith_701_750_801,
    },

    # ---- Cessna 172 Skyhawk ----
    {
        "make": "Cessna 172",
        "module": "scrapers.trade_a_plane",
        "slug": "cessna-172",
        "url": "https://www.trade-a-plane.com/filtered/search?make=CESSNA&model_group=CESSNA+172+SERIES&s-type=aircraft",
        "default_model": "172",
    },
    {
        "make": "Cessna 172",
        "module": "scrapers.controller",
        "slug": "cessna-172",
        "url": "https://www.controller.com/listings/for-sale/cessna/172/aircraft",
        "title_make_pattern": "CESSNA",
        "default_model": "172",
    },
    {
        "make": "Cessna 172",
        "module": "scrapers.aircraftforsale",
        "slug": "cessna-172",
        "sitemap_patterns": ["/cessna/172"],
        "default_model": "172",
    },

    # ---- Cessna 205/206/207 ----
    {
        "make": "Cessna 205/206/207",
        "module": "scrapers.trade_a_plane",
        "slug": "cessna-206",
        "url": "https://www.trade-a-plane.com/filtered/search?make=CESSNA&model_group=CESSNA+206+SERIES&s-type=aircraft",
        "default_model": "206",
    },
    {
        "make": "Cessna 205/206/207",
        "module": "scrapers.controller",
        "slug": "cessna-206",
        "url": "https://www.controller.com/listings/for-sale/cessna/206/aircraft",
        "title_make_pattern": "CESSNA",
        "default_model": "206",
    },
    {
        "make": "Cessna 205/206/207",
        "module": "scrapers.aircraftforsale",
        "slug": "cessna-205-207",
        "sitemap_patterns": ["/cessna/205", "/cessna/206", "/cessna/207"],
        "default_model": "205/206/207",
    },
    {
        "make": "Cessna 205/206/207",
        "module": "scrapers.vanbortel",
        "slug": "cessna-205-207",
        "vb_patterns": ["cessna-205", "cessna-206", "cessna-207",
                        "cessna-t205", "cessna-t206", "cessna-t207"],
        "default_model": "205/206/207",
    },

    # ---- Dream Aircraft Tundra ----
    {
        "make": "Dream Aircraft Tundra",
        "module": "scrapers.trade_a_plane",
        "slug": "dream-tundra",
        "url": "https://www.trade-a-plane.com/filtered/search?make=DREAM+AIRCRAFT&s-type=aircraft",
        "default_model": "Tundra",
    },

    # ---- Glasair Sportsman 2+2 ----
    {
        "make": "Glasair Sportsman 2+2",
        "module": "scrapers.trade_a_plane",
        "slug": "glasair",
        "url": "https://www.trade-a-plane.com/filtered/search?make=GLASAIR&s-type=aircraft",
        "default_model": "Sportsman 2+2",
    },

    # ---- Wag-Aero Sportsman 2+2 ----
    {
        "make": "Wag-Aero Sportsman 2+2",
        "module": "scrapers.trade_a_plane",
        "slug": "wagaero",
        "url": "https://www.trade-a-plane.com/filtered/search?make=WAG-AERO&s-type=aircraft",
        "default_model": "Sportsman 2+2",
    },
    {
        "make": "Wag-Aero Sportsman 2+2",
        "module": "scrapers.controller",
        "slug": "wagaero",
        "url": "https://www.controller.com/listings/for-sale/wag-aero/aircraft",
        "title_make_pattern": "WAG.?AERO",
        "default_model": "Sportsman 2+2",
    },

    # ---- Found Bush Hawk ----
    {
        "make": "Found Bush Hawk",
        "module": "scrapers.trade_a_plane",
        "slug": "found-bushhawk",
        "url": "https://www.trade-a-plane.com/filtered/search?make=FOUND&s-type=aircraft",
        "default_model": "Bush Hawk",
    },
    {
        "make": "Found Bush Hawk",
        "module": "scrapers.controller",
        "slug": "found-bushhawk",
        "url": "https://www.controller.com/listings/for-sale/found/aircraft",
        "title_make_pattern": "FOUND",
        "default_model": "Bush Hawk",
    },

    # ---- Arctic Tern ----
    {
        "make": "Arctic Tern",
        "module": "scrapers.trade_a_plane",
        "slug": "arctic-tern",
        "url": "https://www.trade-a-plane.com/filtered/search?make=INTERSTATE&s-type=aircraft",
        "default_model": "Arctic Tern",
        "post_filter": _keep_arctic_tern,
    },

    # ---- Stearman (Boeing Model 75: PT-13/17/18/27, N2S series, A75/B75/D75/E75) ----
    # TAP files the whole family under make "BOEING/STEARMAN" — clean, no jets.
    # Controller is deliberately omitted: its "Boeing" inventory is all airliners
    # (737/747/767/777/BBJ) with no Stearmans.
    {
        "make": "Stearman",
        "module": "scrapers.trade_a_plane",
        "slug": "stearman",
        "url": "https://www.trade-a-plane.com/filtered/search?make=BOEING%2FSTEARMAN&s-type=aircraft",
        "default_model": "Stearman",
    },
    {
        "make": "Stearman",
        "module": "scrapers.aircraftforsale",
        "slug": "stearman",
        "sitemap_patterns": ["/boeing-stearman/", "/stearman/"],
        "default_model": "Stearman",
    },

    # ---- Wilga (PZL-104 Wilga 35/80, PZL-104MA Wilga 2000, 104MF, 2000 Patrol) ----
    # Rare in the US; no current listings on any source. TAP make=PZL and
    # AircraftForSale /pzl are broad (cover other PZL types) so they carry a
    # _keep_wilga filter; Controller /pzl/wilga and the AeroTrader slug are
    # already Wilga-specific.
    {
        "make": "Wilga",
        "module": "scrapers.trade_a_plane",
        "slug": "wilga",
        "url": "https://www.trade-a-plane.com/filtered/search?make=PZL&s-type=aircraft",
        "default_model": "Wilga",
        "post_filter": _keep_wilga,
    },
    {
        "make": "Wilga",
        "module": "scrapers.aircraftforsale",
        "slug": "wilga",
        "sitemap_patterns": ["/pzl", "/wilga"],
        "default_model": "Wilga",
        "post_filter": _keep_wilga,
    },
    {
        "make": "Wilga",
        "module": "scrapers.controller",
        "slug": "wilga",
        "url": "https://www.controller.com/listings/for-sale/pzl/wilga/aircraft",
        "title_make_pattern": "PZL",
        "default_model": "Wilga",
    },

    # ---- AeroTrader.com — small marketplace, queried per make via JSON API ----
    # Each entry sets `at_make` (the AeroTrader taxonomy make string) — the per-make
    # API result is fetched once per make (lru_cache) and `at_patterns` then filters
    # to the specific model on the detail-URL slug. Kit/experimental brands AeroTrader
    # files under its catch-all "Other" make use at_make="Other".
    {"make": "Aviat Husky", "module": "scrapers.aerotrader", "slug": "aviat-husky",
     "at_make": "Aviat",
     "at_patterns": ["aviat-husky", "aviat+husky"], "default_model": "Husky",
     "post_filter": _keep_husky_180_or_c},
    {"make": "Maule", "module": "scrapers.aerotrader", "slug": "maule",
     "at_make": "Maule",
     "at_patterns": ["-maule-", "/maule"], "default_model": "Maule"},
    {"make": "CubCrafters", "module": "scrapers.aerotrader", "slug": "cubcrafters",
     "at_make": "Other",
     "at_patterns": ["cubcrafters", "cub-crafters"], "default_model": "CubCrafters"},
    {"make": "American Champion", "module": "scrapers.aerotrader", "slug": "american-champion",
     "at_make": "American Champion",
     "at_patterns": ["american-champion", "american+champion"], "default_model": "American Champion"},
    {"make": "Bearhawk", "module": "scrapers.aerotrader", "slug": "bearhawk",
     "at_make": "Other",
     "at_patterns": ["bearhawk"], "default_model": "Bearhawk"},
    {"make": "BushCaddy", "module": "scrapers.aerotrader", "slug": "bushcaddy",
     "at_make": "Other",
     "at_patterns": ["bushcaddy", "bush-caddy"], "default_model": "BushCaddy"},
    {"make": "Cessna 180", "module": "scrapers.aerotrader", "slug": "cessna-180",
     "at_make": "Cessna",
     "at_patterns": ["cessna-180", "cessna+180"], "default_model": "180"},
    {"make": "Cessna 185", "module": "scrapers.aerotrader", "slug": "cessna-185",
     "at_make": "Cessna",
     "at_patterns": ["cessna-185", "cessna+185", "cessna-a185", "cessna+a185"], "default_model": "185"},
    {"make": "Cessna 195", "module": "scrapers.aerotrader", "slug": "cessna-195",
     "at_make": "Cessna",
     "at_patterns": ["cessna-195", "cessna+195", "cessna-190", "cessna+190"], "default_model": "195"},
    {"make": "Cessna 172", "module": "scrapers.aerotrader", "slug": "cessna-172",
     "at_make": "Cessna",
     "at_patterns": ["cessna-172", "cessna+172"], "default_model": "172"},
    {"make": "Cessna 205/206/207", "module": "scrapers.aerotrader", "slug": "cessna-205-207",
     "at_make": "Cessna",
     "at_patterns": ["cessna-205", "cessna-206", "cessna-207",
                     "cessna+205", "cessna+206", "cessna+207"], "default_model": "205/206/207"},
    {"make": "Just Aircraft", "module": "scrapers.aerotrader", "slug": "just-aircraft",
     "at_make": "Other",
     "at_patterns": ["just-aircraft", "highlander", "superstol"], "default_model": "Just Aircraft"},
    {"make": "Kitfox", "module": "scrapers.aerotrader", "slug": "kitfox",
     "at_make": "Kitfox",
     "at_patterns": ["kitfox"], "default_model": "Kitfox"},
    {"make": "Stinson 108", "module": "scrapers.aerotrader", "slug": "stinson-108",
     "at_make": "Stinson",
     "at_patterns": ["stinson-108", "stinson+108"], "default_model": "Stinson 108"},
    {"make": "DHC-2 Beaver", "module": "scrapers.aerotrader", "slug": "dhc-2",
     "at_make": "De Havilland",
     "at_patterns": ["dhc-2", "dhc+2", "de+havilland-dhc", "de-havilland-dhc"], "default_model": "DHC-2"},
    {"make": "Helio Courier", "module": "scrapers.aerotrader", "slug": "helio",
     "at_make": "Helio",
     "at_patterns": ["helio", "courier"], "default_model": "Courier"},
    {"make": "Piper PA-18 Super Cub", "module": "scrapers.aerotrader", "slug": "piper-supercub",
     "at_make": "Piper",
     "at_patterns": ["piper-pa-18", "piper+pa-18", "super-cub", "super+cub"], "default_model": "PA-18 Super Cub"},
    {"make": "ICP Savannah", "module": "scrapers.aerotrader", "slug": "icp",
     "at_make": "Other",
     "at_patterns": ["icp-savannah", "icp+savannah", "savannah"], "default_model": "Savannah"},
    {"make": "Murphy Rebel", "module": "scrapers.aerotrader", "slug": "murphy",
     "at_make": "Other",
     "at_patterns": ["murphy-rebel", "murphy+rebel"], "default_model": "Rebel"},
    {"make": "Rans", "module": "scrapers.aerotrader", "slug": "rans",
     "at_make": "Rans",
     "at_patterns": ["-rans-", "/rans-"], "default_model": "Rans",
     "post_filter": _keep_rans_s6_s7_s20},
    {"make": "Zenith", "module": "scrapers.aerotrader", "slug": "zenith",
     "at_make": "Zenith",
     "at_patterns": ["-zenith-", "/zenith-", "zenair"], "default_model": "Zenith",
     "post_filter": _keep_zenith_701_750_801},
    {"make": "Glasair Sportsman 2+2", "module": "scrapers.aerotrader", "slug": "glasair",
     "at_make": "Other",
     "at_patterns": ["glasair-sportsman", "glasair+sportsman", "glasair-aviation"], "default_model": "Sportsman 2+2"},
    {"make": "Wag-Aero Sportsman 2+2", "module": "scrapers.aerotrader", "slug": "wagaero",
     "at_make": "Wag-Aero",
     "at_patterns": ["wag-aero", "wagaero"], "default_model": "Sportsman 2+2"},
    {"make": "Found Bush Hawk", "module": "scrapers.aerotrader", "slug": "found-bushhawk",
     "at_make": "Other",
     "at_patterns": ["found-bushhawk", "found+bushhawk", "bush-hawk"], "default_model": "Bush Hawk"},
    {"make": "Stearman", "module": "scrapers.aerotrader", "slug": "stearman",
     "at_make": "Boeing",
     "at_patterns": ["stearman", "n2s"], "default_model": "Stearman"},
    {"make": "Wilga", "module": "scrapers.aerotrader", "slug": "wilga",
     "at_make": "PZL",
     "at_patterns": ["wilga", "pzl-104", "pzl%2f104"], "default_model": "Wilga",
     "post_filter": _keep_wilga},
]

FIELDS = [
    "source",
    "make",
    "year",
    "model",
    "price",
    "engine",
    "total_time",
    "engine_time",
    "location",
    "title",
    "url",
    "image_url",
    "description",
    "first_seen",
    "last_seen",
]


def load_previous() -> dict[str, dict]:
    if not CSV_PATH.exists():
        return {}
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return {row["url"]: row for row in csv.DictReader(f)}


def run_all() -> tuple[list[dict], set[tuple[str, str]]]:
    """Run every search. Returns (rows, hard_failed_source_make_pairs)."""
    from scrapers.common import ScraperFailure

    rows: list[dict] = []
    hard_failed: set[tuple[str, str]] = set()
    for search in SEARCHES:
        label = f"{search['module'].split('.')[-1]} ({search['make']})"
        try:
            mod = importlib.import_module(search["module"])
            scraped = mod.scrape(search)
        except ScraperFailure as e:
            source = search["module"].split(".")[-1].replace("_", "-")
            hard_failed.add((source, search["make"]))
            print(f"  {label}: HARD FAIL — {e} (preserving previous rows)", file=sys.stderr)
            continue
        except Exception as e:
            print(f"  {label}: FAILED — {e}", file=sys.stderr)
            traceback.print_exc()
            continue

        filt = search.get("post_filter")
        kept = []
        for l in scraped:
            row = l.as_row()
            if filt is not None and not filt(row):
                continue
            if _is_solicitation(row):  # drop "Wanted/WTB/ISO" buy-side ads
                continue
            if _is_foreign(row):  # drop non-US listings — this is a US-market site
                continue
            kept.append(row)
        dropped = len(scraped) - len(kept)
        suffix = f" ({dropped} filtered out)" if dropped else ""
        print(f"  {label}: {len(kept)} listings{suffix}", file=sys.stderr)
        rows.extend(kept)
    return rows, hard_failed


def dedupe(rows: list[dict]) -> dict[str, dict]:
    by_url: dict[str, dict] = {}
    for r in rows:
        url = r["url"]
        if url not in by_url:
            by_url[url] = r
    return by_url


def dedupe_same_source_images(current: dict[str, dict]) -> int:
    """Drop listings from the SAME source that share an identical photo (a true
    re-post of one aircraft). Cross-source duplicates are kept on purpose, and
    rows with no/!=image are never touched. Mutates `current` in place; returns
    the number removed."""
    seen: set[tuple[str, str]] = set()
    drop_urls: list[str] = []
    for url, row in current.items():
        img = (row.get("image_url") or "").strip()
        if not img:
            continue
        key = (row.get("source") or "", img)
        if key in seen:
            drop_urls.append(url)
        else:
            seen.add(key)
    for url in drop_urls:
        del current[url]
    return len(drop_urls)


# Tail registration ("Reg# N12345") is the authoritative same-aircraft signal.
# Require a digit right after the N so "Reg# Not Listed" doesn't parse as "NOT".
_REG_RE = re.compile(r"Reg#\s*(N\d[0-9A-Z]{0,4})\b", re.I)


def _registration(row: dict) -> str | None:
    m = _REG_RE.search(f"{row.get('title') or ''} {row.get('description') or ''}")
    return m.group(1).upper() if m else None


def dedupe_same_source_registration(current: dict[str, dict]) -> int:
    """Collapse SAME-source listings that share a tail number (a true re-post of
    one aircraft), keeping the priced copy. Cross-source duplicates are kept (so
    you can compare the same plane across marketplaces), and listings with
    different tails — even same year/model — are left alone. Mutates `current`;
    returns the number removed."""
    best: dict[tuple[str, str], str] = {}
    drop_urls: list[str] = []
    for url, row in current.items():
        reg = _registration(row)
        if not reg:
            continue
        key = (row.get("source") or "", reg)
        if key not in best:
            best[key] = url
            continue
        kept = current[best[key]]
        cur_priced = bool((row.get("price") or "").strip())
        kept_priced = bool((kept.get("price") or "").strip())
        if cur_priced and not kept_priced:  # prefer the copy that shows a price
            drop_urls.append(best[key])
            best[key] = url
        else:
            drop_urls.append(url)
    for url in drop_urls:
        current.pop(url, None)
    return len(drop_urls)


def main() -> int:
    print("Scraping sources…", file=sys.stderr)
    previous = load_previous()
    rows, hard_failed = run_all()
    current = dedupe(rows)

    # For (source, make) pairs that hard-failed (e.g. ScrapingBee credits gone),
    # carry over previous rows so the CSV doesn't lose data during outages.
    if hard_failed:
        carried = 0
        for url, row in previous.items():
            key = (row.get("source", ""), row.get("make", ""))
            if key in hard_failed and url not in current:
                current[url] = row
                carried += 1
        if carried:
            print(f"  Carried over {carried} rows from previous run (hard-failed sources)", file=sys.stderr)

    # Collapse same-source re-posts (keeps cross-source duplicates). Done on the
    # full merged set, before the diff. Two signals: identical photo, and shared
    # tail number (Reg#) — the latter keeps the priced copy.
    removed_img = dedupe_same_source_images(current)
    removed_reg = dedupe_same_source_registration(current)
    if removed_img or removed_reg:
        print(f"  Removed {removed_img + removed_reg} same-source duplicate(s) "
              f"({removed_img} by photo, {removed_reg} by tail number)", file=sys.stderr)

    today = date.today().isoformat()
    new_urls = [u for u in current if u not in previous]

    merged: list[dict] = []
    for url, row in current.items():
        prev = previous.get(url)
        merged.append(
            {
                **{k: row.get(k, "") for k in FIELDS if k not in ("first_seen", "last_seen")},
                "first_seen": prev["first_seen"] if prev else today,
                "last_seen": today,
            }
        )

    merged.sort(
        key=lambda r: (r.get("make") or "", r["source"], str(r.get("year") or ""), r["url"])
    )

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for row in merged:
            w.writerow(row)

    lines = [f"# New listings — {today}", ""]
    if not new_urls:
        lines.append("_No new listings since last run._")
    else:
        lines.append(f"{len(new_urls)} new listings:")
        lines.append("")
        for url in new_urls:
            r = current[url]
            lines.append(
                f"- **{r.get('year') or '?'} {r.get('make') or ''} "
                f"{r.get('model') or ''}** — {r.get('price') or 'price n/a'} "
                f"— {r.get('total_time') or ''} "
                f"— {r.get('location') or ''} "
                f"— _{r['source']}_  \n  {url}"
            )
    NEW_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    gone = [u for u in previous if u not in current]
    print(
        f"Done. {len(current)} total, {len(new_urls)} new, {len(gone)} removed. "
        f"-> {CSV_PATH.name}, {NEW_PATH.name}",
        file=sys.stderr,
    )

    # Enrich each TAP listing with clean location from its detail page
    # (cached per-URL — only new URLs fetch fresh).
    try:
        import enrich
        enrich.main()
    except Exception as e:
        print(f"  enrich failed: {e}", file=sys.stderr)

    # Track per-URL price changes, surface any drops since the last run.
    drops: list[dict] = []
    try:
        import price_history
        drops = price_history.update_history(current.values())
        if drops:
            print(f"  price history: {len(drops)} price drop(s)", file=sys.stderr)
    except Exception as e:
        print(f"  price tracking failed: {e}", file=sys.stderr)

    # Geocode any new locations (cached, idempotent, ~1 req/sec)
    try:
        import geocode
        geocode.main()
    except Exception as e:
        print(f"  geocode failed: {e}", file=sys.stderr)

    # Classify thumbnails (cached) so the HTML can hide person-photo listings.
    # No-ops if ANTHROPIC_API_KEY isn't set.
    try:
        import image_filter
        image_filter.main()
    except Exception as e:
        print(f"  image filter failed: {e}", file=sys.stderr)

    # Render the HTML view
    try:
        from generate_html import render
        html_path = render()
        print(f"  HTML -> {html_path.name}", file=sys.stderr)
    except Exception as e:
        print(f"  HTML generation failed: {e}", file=sys.stderr)

    # Email digest of new listings + price drops
    # (no-op if RESEND_API_KEY not set or nothing to report)
    try:
        import notify
        notify.send(
            (current[u] for u in new_urls),
            drops=drops,
            project_url="https://esamson6-claude.github.io/backcountry-aircraft/",
        )
    except Exception as e:
        print(f"  notify failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
