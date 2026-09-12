# backcountry-aircraft

Scrapes US aircraft marketplaces for backcountry/STOL aircraft and publishes them
as a filterable website.

**Live site:** https://esamson6-claude.github.io/backcountry-aircraft/
(GitHub Pages serving `docs/index.html`)

## How it runs

**The site updates itself.** `.github/workflows/daily-scrape.yml` runs at
`0 13 * * *` (9 AM ET), scrapes everything, and commits the new data straight to
`main`. No local machine is involved.

**Consequence for local work:** the repo gains ~1 commit/day on its own, so
`git pull` before starting and before pushing, or you'll diverge. Data files
(`data/*.csv`, `data/*.json`, `docs/index.html`) conflict routinely — see
"Resolving data conflicts" below.

API keys live as GitHub Actions secrets (`SCRAPINGBEE_API_KEY`,
`ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `EMAIL_TO`, `EMAIL_FROM`). Local runs need
their own `.env` (see `.env.example`); `.env` is gitignored.

## Pipeline

`scrape.py` is the entry point and runs everything in order:

1. **scrape** — every entry in `SEARCHES` (one make × one source), via `scrapers/`
2. **filter** — `_is_solicitation` (wanted/WTB ads), `_is_sold` (sold / sale
   pending), `_is_foreign` (US-only site), `_is_parts_or_service` (Barnstormers
   only), plus each entry's `post_filter`
3. **dedupe** — by URL, then same-source re-posts by identical photo and by tail number
4. `enrich.py` — clean "City, ST" locations from TAP detail pages
5. `price_history.py` — track price changes; drop badge needs ≥2% AND ≥$1,000
6. `geocode.py` — locations → coordinates for the map view (cached)
7. `image_filter.py` — Claude Haiku vision flags broker headshots (needs `ANTHROPIC_API_KEY`; no-ops without it)
8. `generate_html.py` — writes `data/listings.html` and `docs/index.html`
9. `notify.py` — Resend email for new listings

## The SEARCHES config

The core of the project. Each dict pairs **one make** with **one source**:

```python
{"make": "Cessna 180", "module": "scrapers.controller",
 "slug": "cessna-180", "url": "...", "title_make_pattern": "CESSNA",
 "default_model": "180", "post_filter": _keep_cessna_180}
```

Adding coverage = adding entries. **The matrix is sparse by accident, not design** —
it was built up make by make, so gaps are the normal failure mode. A make that
simply has no entry for a source silently returns nothing; there is no error.
To audit it, print make → sources and look for holes.

Per-source required keys:
- **controller** — `title_make_pattern` (manufacturer token in the listing title;
  used to extract the model that follows it). **Omitting it raises `KeyError`.**
- **barnstormers** — `urls` (list; a make needs several categories, see below) and
  either `ad_keyword` (substring) or `ad_pattern` (regex)
- **aerotrader** — `at_make` + `at_patterns`
- **aircraftforsale** — `sitemap_patterns`
- **trade_a_plane** — `url`

## Source quirks (learned the hard way)

- **Trade-A-Plane** — behind AWS WAF Bot Control, answers HTTP **202** with a JS
  challenge. `curl_cffi` first (free, works from some IPs), then ScrapingBee's
  **stealth** tier at **75 credits** — classic/premium get served the interstitial.
  Flaky: a third of searches can fail in a run. Hard-fails raise `ScraperFailure`
  so previous rows are carried forward.
- **Controller** — behind **two** challenge systems: Imperva ("Pardon Our
  Interruption") *and* Cloudflare ("Just a moment..."). Both return HTTP 200 with
  a page that parses to zero listings, so an unrecognised challenge is
  indistinguishable from "this make has nothing for sale". Fetched via ScrapingBee
  (~25 credits, `render_js` + `premium_proxy`). Does not paginate.
- **Barnstormers** — free (plain `requests`), but **cross-files one airframe under
  several categories** (`Cessna--`, `Antique-Classic--`, `Taildragger--`), so each
  make needs a `urls` list; dedupe is by listing id across a search's pages. Also
  files **parts, accessories and services inside make categories** (a door latch
  under Bellanca), hence `_is_parts_or_service`. Ad titles are seller-written free
  text — misspellings ("DECATHALON") and transposed model codes ("8CKAB") are
  routine, so prefer `ad_pattern` over `ad_keyword`. The `Taildragger--*` tree
  (parent 22200) maps almost 1:1 onto this site's makes.
- **aviat / vanbortel / aircraft.com** — single-dealer sites, a handful of listings
  each. Low counts are correct, not a bug.

## Gotchas that have bitten before

- **Count listings with a CSV reader, never `wc -l`** — descriptions contain newlines.
- **A source returning `[]` is dangerous.** Empty reads as "no aircraft for sale"
  and deletes that make's rows. Real failures must raise `ScraperFailure`, which
  makes `scrape.py` carry previous rows forward.
- **Never write a filter without testing it against `data/listings.csv` first.**
  A parts filter containing `FINANCING` once matched 19 real aircraft, because
  Trade-A-Plane embeds "Get Financing" in listing titles.
- **`generate_html.py` renders every listing**, using `PLACEHOLDER_IMG` when there's
  no photo. It used to skip photo-less listings, which hid all of Barnstormers.
- **Model-designation regexes need care.** `DECATH[AO]L` matches the misspelling
  "DECATHALON" but *not* the correct "DECATHLON".
- **`_is_sold` is case-sensitive on purpose.** Status markers are shouted
  ("SOLD - ", "SALE PENDING"); the false positives are lowercase prose ("bought
  and sold over 70 aircraft", "will be sold with a new set of tires"). Matching
  case-insensitively deletes live aircraft.

## Makes and categories

33 makes. The Champion/Bellanca/American Champion lineage is scraped as one make
(marketplaces file an airframe under whichever badge it wore), then
`_refine_champion_make` relabels the aerobatic types.

**Aerobatic** works two ways, because a listing can only sit under one make chip:
- `make = "Aerobatic"` for aircraft whose identity is aerobatic (Decathlon,
  Citabria, Pitts, Extra, Christen Eagle…)
- `is_aerobatic()` + an "Aerobatic only" site toggle flags *any* listing
  mentioning aerobatics while leaving its make alone, so an aerobatic-capable
  Stearman stays under Stearman. It ignores negations — "no aerobatic time" is a
  selling point on a non-aerobatic airframe.

## Resolving data conflicts

When a local run collides with a daily cloud commit, resolve **per file**, not wholesale:
- `listings.csv` — keep local, but recover any rows only the remote has, putting
  them through the current filters first
- `price_history.json` — **union** by URL and date; this is recorded history, and
  taking one side destroys price observations and the drop badges built on them
- `geocache.json` / `detail_cache.json` — union (prefer a real coordinate over a null)
- `listings.html` / `docs/index.html` — **regenerate** from the merged CSV rather
  than resolving by hand

## Current status (2026-09-12)

1,004 listings, 999 cards, 143 searches across 8 sources, 33 makes.
(1,112 before 108 sold/sale-pending ads were filtered out.)

**Trade-A-Plane reliability** was the largest coverage gap (a third of searches
failing per run) and is now understood: the failures were **ScrapingBee HTTP 500s**
from its stealth pool under load, *not* WAF challenges. `_get` returned
immediately on that case, so the only failure mode needing a long backoff was the
one that never got it. It now falls through to the 3/6/12s retry loop like the
others. Diagnose with the `[trade-a-plane] rejected ...` line, which names the
mode: `api-error` (ScrapingBee), `challenge` (WAF), or `stub` (truncated page).
