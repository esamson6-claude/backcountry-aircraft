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
        "url": "https://aircraftforsale.com/aircraft/search?manufacturer=Aviat&model=A-1",
        "href_substring": "husky",
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
        "url": "https://aircraftforsale.com/aircraft/search?manufacturer=Maule",
        "href_substring": "maule",
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
        "url": "https://aircraftforsale.com/aircraft/search?manufacturer=CubCrafters",
        "href_substring": "cub-crafters",
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
        "url": "https://aircraftforsale.com/aircraft/search?manufacturer=American+Champion",
        "href_substring": "american-champion",
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
        "url": "https://aircraftforsale.com/aircraft/search?manufacturer=Bearhawk",
        "href_substring": "bearhawk",
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
        "url": "https://aircraftforsale.com/aircraft/search?manufacturer=Cessna&model=180",
        "href_substring": "cessna/180",
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
        "url": "https://aircraftforsale.com/aircraft/search?manufacturer=Cessna&model=185",
        "href_substring": "cessna/185",
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
        "url": "https://aircraftforsale.com/aircraft/search?manufacturer=Cessna&model=195",
        "href_substring": "cessna/195",
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

    # ---- Kitfox (Super Sport 7 LSA) ----
    {
        "make": "Kitfox",
        "module": "scrapers.trade_a_plane",
        "slug": "kitfox",
        "url": "https://www.trade-a-plane.com/filtered/search?make=KITFOX&s-type=aircraft",
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

    # ---- de Havilland DHC-2 Beaver ----
    {
        "make": "DHC-2 Beaver",
        "module": "scrapers.trade_a_plane",
        "slug": "dhc-2",
        "url": "https://www.trade-a-plane.com/filtered/search?make=DEHAVILLAND&model_group=DEHAVILLAND+DHC+SERIES&s-type=aircraft",
        "default_model": "DHC-2",
        "post_filter": _keep_dhc2,
    },

    # ---- Helio Courier ----
    {
        "make": "Helio Courier",
        "module": "scrapers.trade_a_plane",
        "slug": "helio",
        "url": "https://www.trade-a-plane.com/filtered/search?make=HELIO&s-type=aircraft",
        "default_model": "Courier",
    },
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
        kept = [l.as_row() for l in scraped if (filt is None or filt(l.as_row()))]
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

    # Geocode any new locations (cached, idempotent, ~1 req/sec)
    try:
        import geocode
        geocode.main()
    except Exception as e:
        print(f"  geocode failed: {e}", file=sys.stderr)

    # Render the HTML view
    try:
        from generate_html import render
        html_path = render()
        print(f"  HTML -> {html_path.name}", file=sys.stderr)
    except Exception as e:
        print(f"  HTML generation failed: {e}", file=sys.stderr)

    # Email digest of new listings (no-op if RESEND_API_KEY not set or none new)
    try:
        import notify
        notify.send(
            (current[u] for u in new_urls),
            project_url="https://esamson6-claude.github.io/backcountry-aircraft/",
        )
    except Exception as e:
        print(f"  notify failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
