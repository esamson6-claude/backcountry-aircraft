# backcountry-aircraft

Aggregates **backcountry / STOL aircraft for sale** from across the major US
marketplaces into one searchable, filterable website — updated automatically
every morning.

**Live site:** https://esamson6-claude.github.io/backcountry-aircraft/

The scraper gathers listings, dedupes them, tracks price changes over time,
geocodes their locations, and renders a single self-contained web page with a
grid view, an interactive map, favorites, and rich filtering. A daily
GitHub Actions job runs the whole pipeline and publishes the result — no
machine of yours needs to be on.

## What it tracks

**Aircraft** (each defined as one or more entries in the `SEARCHES` list in
`scrape.py`): Aviat Husky, Maule, CubCrafters, American Champion (Citabria /
Scout), Bearhawk, BushCaddy, Cessna L-19 Bird Dog, Cessna 170/175, 180, 185,
195, 172, 205/206/207, Just Aircraft, Kitfox, Stinson 108, DHC-2 Beaver,
Helio Courier, Piper PA-18 Super Cub, Kitplanes for Africa, ICP Savannah,
Murphy Rebel, Groppo Trial, Rans (S6/S7/S20), Zenith (CH701/750/801),
Dream Tundra, Glasair & Wag-Aero Sportsman, Found Bush Hawk, Arctic Tern,
Stearman, and Wilga.

**Sources** (one scraper module each, in `scrapers/`):

| Source | Module | Notes |
|---|---|---|
| Trade-A-Plane | `trade_a_plane.py` | curl_cffi with Chrome TLS impersonation |
| Controller | `controller.py` | Imperva-protected — scraped via [ScrapingBee](https://www.scrapingbee.com) (needs `SCRAPINGBEE_API_KEY`) |
| AircraftForSale.com | `aircraftforsale.py` | crawled via sitemap patterns |
| AeroTrader | `aerotrader.py` | per-make JSON API |
| Barnstormers | `barnstormers.py` | category pages |
| Aviat Aircraft | `aviat.py` | factory used inventory |
| aircraft.com | `aircraftcom.py` | individual listing URLs (no enumerable index) |
| Van Bortel | `vanbortel.py` | dealer inventory |

Each search applies per-type filters to exclude look-alikes (e.g. keep only
DHC-2 Beavers out of the broader de Havilland family), and a site-wide filter
drops buy-side "Wanted / WTB / ISO" solicitations.

## How the daily pipeline works

`scrape.py` runs these steps in order (each later step is wrapped so a failure
degrades gracefully rather than aborting the run):

1. **Scrape** every (aircraft × source) search, apply filters, and **dedupe by
   listing URL**.
2. **Outage protection** — if a source hard-fails (e.g. ScrapingBee credits
   exhausted), the previous run's rows for that source are carried over so the
   CSV never loses data during an outage.
3. **History** — preserve each listing's original `first_seen` date, stamp
   `last_seen` = today, and write a "new since last run" diff to
   `data/new_listings.md`.
4. **Enrich** (`enrich.py`) — fetch Trade-A-Plane detail pages to clean up
   messy location text (cached per-URL in `data/detail_cache.json`).
5. **Price history** (`price_history.py`) — append today's price to each
   listing's log only when it changed; surface any **price drops**.
6. **Geocode** (`geocode.py`) — resolve locations to map coordinates via
   OpenStreetMap Nominatim (~1 req/sec, cached in `data/geocache.json`).
7. **Render** (`generate_html.py`) — build the website into `docs/index.html`.
8. **Notify** (`notify.py`) — email a digest of new listings + price drops via
   [Resend] (only if `RESEND_API_KEY` is set and there's something to report).

The automation lives in `.github/workflows/daily-scrape.yml` — a cron job at
13:00 UTC (~9 AM ET), plus a manual "Run workflow" trigger. It scrapes,
commits any changes to `data/` and `docs/`, and pushes. GitHub Pages serves
`docs/` as the live site. API keys are stored as **GitHub Actions secrets**,
not in the repo.

## The website

`docs/index.html` is a single self-contained page (no build step, no backend):

- **Grid view** — a card per listing with photo, price, model, engine, airframe
  hours, location, source, and first-seen date. Cards link to the original ad.
- **Map view** — geocoded listings as pins on a Leaflet/OpenStreetMap map.
- **Favorites** — heart any listing; saved in your browser (`localStorage`),
  with a dedicated Favorites view. Hearts stay in sync between grid and map.
- **Filters** — full-text search; price and year ranges; multi-select make and
  source chips; "new in last 7 days" and "recent price drops" toggles.
- **Sorting** — by year, price, or airframe total time.
- **Badges** — green **NEW** on listings first seen within 7 days (high-volume
  Cessna 172 and 205/206/207 are excluded from the "new" count so they don't
  dominate); red **price-drop** badge showing the change.

## Project layout

```
scrape.py            Orchestrator: defines all searches, runs the pipeline
scrapers/            One module per marketplace (+ common.py helpers)
enrich.py            Clean up listing locations from detail pages
geocode.py           Location → lat/lng via Nominatim (cached)
price_history.py     Per-URL price log + drop detection
generate_html.py     Render the CSV into the website (docs/index.html)
notify.py            Email digest via Resend
update.sh            Local: re-scrape, regenerate, commit, and push
.github/workflows/   daily-scrape.yml — the scheduled cloud job
data/                Generated data (see below)
docs/                The published website (GitHub Pages serves this)
```

### Data files (`data/`)

- `listings.csv` — current full snapshot (the source of truth for the site)
- `new_listings.md` — listings new since the previous run
- `price_history.json` — per-URL price log over time
- `geocache.json` — cached location → coordinates
- `detail_cache.json` — cached enriched detail-page fields

CSV columns: `source, make, year, model, price, engine, total_time,
engine_time, location, title, url, image_url, description, first_seen,
last_seen`.

## Running it locally

```bash
cd ~/Projects/backcountry-aircraft
~/.local/bin/uv venv
~/.local/bin/uv pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Copy `.env.example` to `.env` and add your keys (all optional for a basic run):

- `SCRAPINGBEE_API_KEY` — enables Controller.com scraping (free trial = 1000
  credits; each Controller call costs ~3)
- `RESEND_API_KEY`, `EMAIL_TO`, `EMAIL_FROM` — enable the email digest (omit to
  skip emailing; local runs no-op silently without the Resend key)

Then run the full pipeline (scrape → enrich → price history → geocode →
render → notify):

```bash
source .venv/bin/activate
python scrape.py
```

Or regenerate just the website from existing data:

```bash
python generate_html.py
```

`./update.sh` does a scrape + regenerate + commit + push in one step (it
rebases past the daily cron's commit if needed).

### Chromium system libs (one-time, no-sudo workaround)

Playwright's Chromium needs `libnspr4 libnss3 libasound2t64 libxss1`. With sudo,
install them via apt. Without sudo (e.g. locked-down WSL), extract them into a
user directory:

```bash
mkdir -p /tmp/chromium-libs ~/.local/lib/chromium-deps
cd /tmp/chromium-libs
apt download libnspr4 libnss3 libasound2t64 libxss1
for d in *.deb; do dpkg-deb -x "$d" ~/.local/lib/chromium-deps/; done
```

The scrapers auto-detect `~/.local/lib/chromium-deps` and add it to
`LD_LIBRARY_PATH`.

## Adding a new aircraft or source

Append a search dict to the `SEARCHES` list in `scrape.py`. Most scrapers take
a `make`, `module` (the scraper to use), `slug`, a source-specific target
(`url`, `sitemap_patterns`, `at_patterns`, etc.), a `default_model`, and an
optional `post_filter` to narrow results to the exact model you want.

[Resend]: https://resend.com
