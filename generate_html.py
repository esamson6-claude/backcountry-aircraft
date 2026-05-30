"""Render data/listings.csv as a single self-contained HTML page with sort/filter UI."""
from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
DOCS_DIR = PROJECT_ROOT / "docs"
LISTING_DIR = DOCS_DIR / "listing"
DOCS_DIR.mkdir(exist_ok=True)
LISTING_DIR.mkdir(exist_ok=True)
CSV_PATH = DATA_DIR / "listings.csv"
GEOCACHE_PATH = DATA_DIR / "geocache.json"
HTML_PATH = DATA_DIR / "listings.html"
DOCS_HTML_PATH = DOCS_DIR / "index.html"


_SOURCE_LABELS = {
    "trade-a-plane": "Trade-A-Plane",
    "controller": "Controller",
    "aviat": "Aviat Aircraft",
    "aircraftforsale": "AircraftForSale.com",
    "barnstormers": "Barnstormers",
}


def _slug_for(row: dict) -> str:
    """Stable filename slug per listing: '<source>-<id>' where possible."""
    src = (row.get("source") or "x").replace(" ", "-").lower()
    url = row.get("url") or ""
    # Try to extract a stable numeric/alnum ID from the URL
    for pat in (
        r"listing_id=(\d+)",                # TAP
        r"/listing/for-sale/(\d+)/",        # Controller
        r"husky-for-sale-(\d+)",            # AircraftForSale
        r"#(N\w{3,6})",                     # Aviat (N-number)
        r"id=(\d+)",                        # Barnstormers
    ):
        m = re.search(pat, url)
        if m:
            return f"{src}-{m.group(1)}"
    # Fallback: short hash of the URL
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    return f"{src}-{h}"


def _load_geocache() -> dict[str, list[float] | None]:
    if GEOCACHE_PATH.exists():
        try:
            return json.loads(GEOCACHE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


PLACEHOLDER_IMG = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 250'>"
    "<rect width='400' height='250' fill='%23e0e0e0'/>"
    "<text x='50%25' y='50%25' dominant-baseline='middle' text-anchor='middle'"
    " font-family='sans-serif' font-size='18' fill='%23888'>No image</text>"
    "</svg>"
)


def _int_from(text: str) -> int:
    """Pull the first integer from a free-text field (e.g. '1,234 TT' → 1234)."""
    if not text:
        return 0
    m = re.search(r"[\d,]+", text)
    if not m:
        return 0
    digits = m.group(0).replace(",", "")
    try:
        return int(digits)
    except ValueError:
        return 0


def _write_detail_pages(rows: list[dict], drops_by_url: dict) -> dict[int, str]:
    """Write docs/listing/<slug>.html for each row. Return id(row) → relative URL.

    Uses id(row) as the key so the caller can look up the href without
    re-deriving the slug. Stale listing files are removed.
    """
    detail_links: dict[int, str] = {}
    keep_slugs: set[str] = set()

    for r in rows:
        slug = _slug_for(r)
        keep_slugs.add(slug)
        detail_links[id(r)] = f"listing/{slug}.html"
        (LISTING_DIR / f"{slug}.html").write_text(
            _detail_html(r, drops_by_url.get(r.get("url") or "")),
            encoding="utf-8",
        )

    # Prune stale per-listing files (URLs that are no longer in the CSV)
    for p in LISTING_DIR.glob("*.html"):
        if p.stem not in keep_slugs:
            p.unlink(missing_ok=True)

    return detail_links


def _detail_html(r: dict, drop: dict | None) -> str:
    title = html.escape(
        f"{r.get('year') or '?'} {r.get('make') or ''} {r.get('model') or ''}".strip()
    )
    price = html.escape(r.get("price") or "Price n/a")
    description = (r.get("description") or "").strip()
    description_html = html.escape(description).replace("\n", "<br>")
    source = (r.get("source") or "").lower()
    source_label = html.escape(_SOURCE_LABELS.get(source, source.title() or "Source"))
    source_url = html.escape(r.get("url") or "#", quote=True)
    img = html.escape(r.get("image_url") or "", quote=True) or PLACEHOLDER_IMG
    first_seen = html.escape(r.get("first_seen") or "")
    last_seen = html.escape(r.get("last_seen") or "")

    spec_pairs = [
        ("Make", r.get("make")),
        ("Year", str(r.get("year")) if r.get("year") else None),
        ("Model", r.get("model")),
        ("Engine", r.get("engine")),
        ("Airframe TT", r.get("total_time")),
        ("Engine TT", r.get("engine_time")),
        ("Location", r.get("location")),
        ("First seen", first_seen),
        ("Last seen", last_seen),
    ]
    spec_rows = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in spec_pairs
        if v
    )

    drop_html = ""
    if drop:
        drop_html = (
            f'<div class="drop">↓ {drop["pct_change"]}% '
            f'&nbsp;${drop["previous_price"]:,} → ${drop["current_price"]:,} '
            f'<span class="drop-days">({drop["days_ago"]} day{"s" if drop["days_ago"] != 1 else ""} ago)</span>'
            f'</div>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; --bg:#f5f5f7; --card:#fff; --fg:#222; --muted:#666;
           --border:#e2e2e6; --accent:#0366d6; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#1a1a1c; --card:#26262a; --fg:#eee; --muted:#aaa; --border:#3a3a40;
             --accent:#58a6ff; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font: 14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          background:var(--bg); color:var(--fg); }}
  header {{ padding:14px 20px; border-bottom:1px solid var(--border); background:var(--card); }}
  .back {{ color:var(--accent); text-decoration:none; font-size:13px; }}
  main {{ max-width: 880px; margin: 0 auto; padding: 20px; }}
  h1 {{ margin:0 0 4px 0; font-size:24px; font-weight:600; }}
  .price {{ color:var(--accent); font-weight:700; font-size:22px; margin-bottom:8px; }}
  .drop {{ display:inline-block; padding:5px 12px; border-radius:6px;
           background:#fdecea; color:#a40000; font-size:13px; font-weight:600;
           margin-bottom:16px; }}
  .drop-days {{ font-weight:400; opacity:0.8; }}
  @media (prefers-color-scheme: dark) {{
    .drop {{ background:#4a1f1f; color:#ff9999; }}
  }}
  .hero {{ width:100%; aspect-ratio: 16/9; object-fit:cover;
           border-radius:10px; border:1px solid var(--border); margin: 14px 0 20px; background:#0001; }}
  .cta {{ display:inline-block; background:var(--accent); color:#fff !important; padding:10px 20px;
          border-radius:8px; font-weight:600; text-decoration:none; margin-bottom:20px; }}
  .cta:hover {{ opacity:0.9; }}
  table.specs {{ width:100%; border-collapse:collapse; background:var(--card);
                 border:1px solid var(--border); border-radius:8px; overflow:hidden; }}
  table.specs th, table.specs td {{ padding:8px 12px; text-align:left;
                                    border-bottom:1px solid var(--border); font-size:13px; }}
  table.specs tr:last-child th, table.specs tr:last-child td {{ border-bottom:0; }}
  table.specs th {{ width:120px; color:var(--muted); font-weight:500; text-transform:uppercase;
                    letter-spacing:0.4px; font-size:11px; vertical-align:top; }}
  .description {{ margin-top:24px; padding:18px; background:var(--card);
                  border:1px solid var(--border); border-radius:8px; }}
  .description h2 {{ margin:0 0 10px 0; font-size:15px; color:var(--muted);
                     text-transform:uppercase; letter-spacing:0.5px; font-weight:600; }}
  .description-text {{ font-size:14px; line-height:1.55; color:var(--fg); white-space:pre-wrap; }}
  footer {{ margin-top:30px; padding-top:12px; border-top:1px solid var(--border);
            font-size:12px; color:var(--muted); }}
</style>
</head>
<body>
<header>
  <a class="back" href="../">← All listings</a>
</header>
<main>
  <h1>{title}</h1>
  <div class="price">{price}</div>
  {drop_html}
  <img class="hero" src="{img}" alt="{title}" onerror="this.src='{PLACEHOLDER_IMG}'">
  <p><a class="cta" href="{source_url}" target="_blank" rel="noopener">
    View original on {source_label} →
  </a></p>
  <table class="specs">{spec_rows}</table>
  {('<div class="description"><h2>Description</h2><div class="description-text">' + description_html + '</div></div>') if description else ''}
  <footer>
    Aggregated from {source_label}. Click "View original" for current pricing and full details.
  </footer>
</main>
</body>
</html>
"""


def render() -> Path:
    if not CSV_PATH.exists():
        raise SystemExit(f"{CSV_PATH} not found — run scrape.py first")
    rows = list(csv.DictReader(CSV_PATH.open(newline="", encoding="utf-8")))
    geocache = _load_geocache()

    try:
        import price_history
        drops_by_url = price_history.recent_drops_by_url(days=21)
    except Exception:
        drops_by_url = {}

    makes = sorted({r.get("make") or "Unknown" for r in rows})
    sources = sorted({r.get("source") or "" for r in rows if r.get("source")})

    # Default sort: make, then year desc, then price desc
    rows.sort(
        key=lambda r: (
            r.get("make") or "",
            -int(r.get("year") or 0),
            -_int_from(r.get("price") or ""),
        )
    )

    # Remove any per-listing detail pages from previous runs — cards now link
    # straight to the source marketplace.
    if LISTING_DIR.exists():
        for p in LISTING_DIR.glob("*.html"):
            p.unlink(missing_ok=True)

    cards_html: list[str] = []
    for r in rows:
        title = html.escape(
            f"{r.get('year') or '?'} {r.get('make') or ''} {r.get('model') or ''}".strip()
        )
        price = html.escape(r.get("price") or "Price n/a")
        airframe = html.escape(r.get("total_time") or "")
        eng_time = html.escape(r.get("engine_time") or "")
        engine = html.escape(r.get("engine") or "")
        model = html.escape(r.get("model") or "")
        loc = html.escape(r.get("location") or "")
        source = html.escape(r.get("source") or "")
        make = html.escape(r.get("make") or "Unknown")
        url = html.escape(r.get("url") or "#", quote=True)
        img = html.escape(r.get("image_url") or "", quote=True) or PLACEHOLDER_IMG
        first_seen = html.escape(r.get("first_seen") or "")

        # Numeric data attributes for sort/filter
        year_n = int(r.get("year") or 0)
        price_n = _int_from(r.get("price") or "")
        hours_n = _int_from(r.get("total_time") or "")
        # Geocoded coordinates (may be None if Nominatim couldn't resolve)
        coords = geocache.get((r.get("location") or "").strip())
        lat_attr = f' data-lat="{coords[0]}"' if coords else ""
        lng_attr = f' data-lng="{coords[1]}"' if coords else ""
        # Recent price drop?
        drop = drops_by_url.get(r.get("url") or "")
        drop_html = ""
        drop_attr = ""
        if drop:
            drop_attr = ' data-drop="1"'
            drop_html = (
                f'<div class="drop">↓ {drop["pct_change"]}%'
                f' &nbsp;${drop["previous_price"]:,} → ${drop["current_price"]:,}'
                f'</div>'
            )
        # Searchable text blob
        search_blob = html.escape(
            " ".join([
                r.get("title") or "",
                r.get("model") or "",
                r.get("location") or "",
                r.get("description") or "",
                r.get("engine") or "",
            ]).lower(),
            quote=True,
        )

        spec_rows = []
        if model:
            spec_rows.append(f'<div class="spec"><span>Model</span><span>{model}</span></div>')
        if engine:
            spec_rows.append(f'<div class="spec"><span>Engine</span><span>{engine}</span></div>')
        if airframe:
            spec_rows.append(f'<div class="spec"><span>Airframe TT</span><span>{airframe}</span></div>')
        if eng_time:
            spec_rows.append(f'<div class="spec"><span>Engine TT</span><span>{eng_time}</span></div>')

        cards_html.append(
            f"""<a class="card" href="{url}" target="_blank" rel="noopener"
   data-make="{make}" data-source="{source}"
   data-year="{year_n}" data-price="{price_n}" data-hours="{hours_n}"
   data-search="{search_blob}"{lat_attr}{lng_attr}{drop_attr}
   data-title="{title}" data-price-text="{price}" data-loc="{loc}" data-img="{img}">
  <div class="thumb"><img loading="lazy" src="{img}" alt="{title}" onerror="this.src='{PLACEHOLDER_IMG}'"></div>
  <div class="body">
    <div class="title">{title}</div>
    <div class="price">{price}</div>
    {drop_html}
    <div class="specs">{''.join(spec_rows)}</div>
    <div class="meta">
      <span class="loc">{loc}</span>
    </div>
    <div class="footer">
      <span class="source">{source}</span>
      <span class="seen">first seen {first_seen}</span>
    </div>
  </div>
</a>"""
        )

    make_buttons = "".join(
        f'<button class="chip make-chip" data-make="{html.escape(m, quote=True)}">{html.escape(m)}</button>'
        for m in makes
    )
    source_buttons = "".join(
        f'<button class="chip source-chip" data-source="{html.escape(s, quote=True)}">{html.escape(s)}</button>'
        for s in sources
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aircraft listings — {date.today().isoformat()}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<style>
  :root {{ color-scheme: light dark; --bg:#f5f5f7; --card:#fff; --fg:#222; --muted:#666;
           --border:#e2e2e6; --accent:#0366d6; --chip-bg:transparent; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#1a1a1c; --card:#26262a; --fg:#eee; --muted:#aaa; --border:#3a3a40;
             --accent:#58a6ff; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font: 14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          background:var(--bg); color:var(--fg); }}
  header {{ padding:14px 20px; border-bottom:1px solid var(--border); background:var(--card);
            position:sticky; top:0; z-index:2; }}
  h1 {{ margin:0 0 4px 0; font-size:18px; font-weight:600; }}
  .subhead {{ color:var(--muted); font-size:12px; margin-bottom:10px; }}
  .filter-toggle {{ display:none; padding:6px 14px; border:1px solid var(--border);
                    border-radius:6px; background:var(--bg); color:var(--fg); font:inherit;
                    cursor:pointer; }}
  #filter-panel {{ display:block; }}
  @media (max-width: 759px) {{
    /* On mobile the controls are tall; don't pin the entire header to the
       viewport top — let it scroll away. The toggle still lets users
       reopen the panel from anywhere on the page via the floating button. */
    header {{ position:static; padding:10px 14px; }}
    .filter-toggle {{ display:inline-flex; align-items:center; gap:6px; }}
    #filter-panel {{ display:none; margin-top:10px; }}
    #filter-panel.open {{ display:block; }}
    /* Floating "Filters" button visible while scrolling listings */
    #floating-filter {{ position:fixed; right:14px; bottom:14px; z-index:5;
                        background:var(--accent); color:#fff; border:0; padding:10px 16px;
                        border-radius:999px; font:inherit; font-weight:600;
                        box-shadow:0 4px 12px rgba(0,0,0,0.25); cursor:pointer; }}
  }}
  @media (min-width: 760px) {{
    #floating-filter {{ display:none; }}
  }}
  .controls {{ display:grid; gap:10px;
               grid-template-columns: 1fr; }}
  @media (min-width: 760px) {{
    .controls {{ grid-template-columns: 2fr 1fr 1fr 1fr; align-items:end; }}
  }}
  .control-group {{ display:flex; flex-direction:column; gap:4px; }}
  .control-group label {{ font-size:11px; color:var(--muted); text-transform:uppercase;
                          letter-spacing:0.5px; }}
  .control-group input, .control-group select {{
    padding:6px 10px; border:1px solid var(--border); border-radius:6px;
    background:var(--bg); color:var(--fg); font:inherit; }}
  .range-row {{ display:flex; gap:6px; }}
  .range-row input {{ width:0; flex:1; min-width:0; }}
  .chips {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:10px; }}
  .chip {{ padding:5px 12px; border-radius:999px; border:1px solid var(--border);
           background:var(--chip-bg); color:var(--fg); font:inherit; font-size:12px; cursor:pointer; }}
  .chip:hover {{ border-color:var(--accent); }}
  .chip.active {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
  .chip-row-label {{ font-size:11px; color:var(--muted); text-transform:uppercase;
                     letter-spacing:0.5px; align-self:center; margin-right:4px; }}
  #count {{ font-weight:600; }}
  main {{ padding:20px; display:grid; gap:16px;
          grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }}
  .card {{ display:flex; flex-direction:column; background:var(--card); border:1px solid var(--border);
           border-radius:10px; overflow:hidden; text-decoration:none; color:inherit;
           transition: transform .1s, box-shadow .15s; }}
  .card:hover {{ transform: translateY(-2px); box-shadow: 0 8px 20px rgba(0,0,0,0.08); }}
  .card.hidden {{ display:none; }}
  .thumb {{ aspect-ratio: 16/10; background:#0001; overflow:hidden; }}
  .thumb img {{ width:100%; height:100%; object-fit:cover; display:block; }}
  .body {{ padding:12px 14px; display:flex; flex-direction:column; gap:6px; }}
  .title {{ font-weight:600; font-size:15px; }}
  .price {{ color:var(--accent); font-weight:600; font-size:16px; }}
  .drop {{ display:inline-block; padding:3px 8px; border-radius:4px;
           background:#fdecea; color:#a40000; font-size:12px; font-weight:600;
           margin-top:2px; }}
  @media (prefers-color-scheme: dark) {{
    .drop {{ background:#4a1f1f; color:#ff9999; }}
  }}
  .specs {{ display:grid; grid-template-columns: auto 1fr; gap:2px 10px; font-size:12px;
            color:var(--muted); margin-top:2px; }}
  .spec {{ display:contents; }}
  .spec > span:first-child {{ text-transform:uppercase; letter-spacing:0.4px; font-size:10px;
                              opacity:0.7; align-self:center; }}
  .spec > span:last-child {{ color:var(--fg); font-size:12.5px; }}
  .meta {{ color:var(--muted); font-size:12px; display:flex; gap:10px; flex-wrap:wrap; }}
  .footer {{ display:flex; justify-content:space-between; font-size:11px; color:var(--muted);
             margin-top:auto; padding-top:6px; border-top:1px solid var(--border); }}
  .source {{ text-transform:uppercase; letter-spacing:0.5px; }}
  #empty {{ padding:40px 20px; text-align:center; color:var(--muted); display:none; }}
  .view-toggle {{ display:flex; gap:4px; background:var(--bg); padding:3px; border-radius:8px;
                  border:1px solid var(--border); }}
  .view-toggle button {{ padding:5px 14px; border:0; background:transparent; color:var(--fg);
                         font:inherit; cursor:pointer; border-radius:5px; }}
  .view-toggle button.active {{ background:var(--accent); color:#fff; }}
  #map {{ height: calc(100vh - 250px); min-height: 500px; margin:0 20px 20px 20px;
          border-radius: 10px; border:1px solid var(--border); display:none; }}
  body.map-view #grid, body.map-view #empty {{ display:none; }}
  body.map-view #map {{ display:block; }}
  .leaflet-popup-content {{ margin:8px 10px; }}
  .leaflet-popup-content .popup-thumb {{ width:200px; height:120px; object-fit:cover;
                                          border-radius:4px; display:block; margin-bottom:6px; }}
  .leaflet-popup-content .popup-title {{ font-weight:600; font-size:13px; margin-bottom:2px; }}
  .leaflet-popup-content .popup-price {{ color:#0366d6; font-weight:600; font-size:13px; }}
  .leaflet-popup-content .popup-loc {{ font-size:11px; color:#666; }}
  .leaflet-popup-content a {{ color:#0366d6; text-decoration:none; font-size:11px; }}
</style>
</head>
<body>
<header>
  <div style="display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap;">
    <div>
      <h1>Aircraft listings — <span id="count">{len(rows)}</span> shown</h1>
      <div class="subhead">Updated {date.today().isoformat()} · click any card to open the listing</div>
    </div>
    <div style="display:flex; gap:8px; align-items:center;">
      <button class="filter-toggle" id="filter-toggle" aria-expanded="false">Filters ▾</button>
      <div class="view-toggle">
        <button id="view-grid" class="active">Grid</button>
        <button id="view-map">Map</button>
      </div>
    </div>
  </div>
  <div id="filter-panel">
  <div class="controls">
    <div class="control-group">
      <label for="search">Search</label>
      <input id="search" type="search" placeholder="Title, model, location, description…">
    </div>
    <div class="control-group">
      <label for="sort">Sort by</label>
      <select id="sort">
        <option value="default">Default (make / newest / highest price)</option>
        <option value="year-desc">Year — newest first</option>
        <option value="year-asc">Year — oldest first</option>
        <option value="price-asc">Price — low to high</option>
        <option value="price-desc">Price — high to low</option>
        <option value="hours-asc">Airframe TT — low to high</option>
        <option value="hours-desc">Airframe TT — high to low</option>
      </select>
    </div>
    <div class="control-group">
      <label>Price range ($)</label>
      <div class="range-row">
        <input id="price-min" type="number" placeholder="Min" min="0">
        <input id="price-max" type="number" placeholder="Max" min="0">
      </div>
    </div>
    <div class="control-group">
      <label>Year range</label>
      <div class="range-row">
        <input id="year-min" type="number" placeholder="From" min="1900" max="2030">
        <input id="year-max" type="number" placeholder="To" min="1900" max="2030">
      </div>
    </div>
  </div>
  <div style="margin-top:10px;">
    <label style="font-size:12px; color:var(--muted); cursor:pointer;">
      <input id="drops-only" type="checkbox" style="vertical-align:middle;">
      Show only listings with recent price drops
    </label>
  </div>
  <div class="chips">
    <span class="chip-row-label">Make:</span>
    <button class="chip make-chip active" data-make="__all__">All</button>
    {make_buttons}
  </div>
  <div class="chips">
    <span class="chip-row-label">Source:</span>
    <button class="chip source-chip active" data-source="__all__">All</button>
    {source_buttons}
  </div>
  </div><!-- /#filter-panel -->
</header>
<button id="floating-filter" type="button">☰ Filters</button>
<main id="grid">
  {''.join(cards_html)}
</main>
<div id="empty">No listings match the current filters.</div>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
  const grid = document.getElementById('grid');
  const cards = Array.from(grid.querySelectorAll('.card'));
  const countEl = document.getElementById('count');
  const emptyEl = document.getElementById('empty');

  // Mobile filter-panel toggle (button in header + floating button at bottom-right)
  const filterPanel = document.getElementById('filter-panel');
  const filterToggle = document.getElementById('filter-toggle');
  const floatingFilter = document.getElementById('floating-filter');
  function toggleFilters() {{
    const open = !filterPanel.classList.contains('open');
    filterPanel.classList.toggle('open', open);
    filterToggle.setAttribute('aria-expanded', String(open));
    filterToggle.textContent = open ? 'Filters ▴' : 'Filters ▾';
    if (open) filterPanel.scrollIntoView({{behavior: 'smooth', block: 'start'}});
  }}
  filterToggle.addEventListener('click', toggleFilters);
  floatingFilter.addEventListener('click', toggleFilters);

  const searchEl = document.getElementById('search');
  const sortEl = document.getElementById('sort');
  const priceMinEl = document.getElementById('price-min');
  const priceMaxEl = document.getElementById('price-max');
  const yearMinEl = document.getElementById('year-min');
  const yearMaxEl = document.getElementById('year-max');
  const dropsOnlyEl = document.getElementById('drops-only');

  let activeMake = '__all__';
  let activeSource = '__all__';

  function num(v) {{ const n = parseInt(v, 10); return Number.isFinite(n) ? n : null; }}

  function apply() {{
    const q = searchEl.value.trim().toLowerCase();
    const pMin = num(priceMinEl.value), pMax = num(priceMaxEl.value);
    const yMin = num(yearMinEl.value), yMax = num(yearMaxEl.value);

    let visible = [];
    for (const c of cards) {{
      const make = c.dataset.make;
      const source = c.dataset.source;
      const year = parseInt(c.dataset.year, 10) || 0;
      const price = parseInt(c.dataset.price, 10) || 0;
      const search = c.dataset.search;

      let show = true;
      if (activeMake !== '__all__' && make !== activeMake) show = false;
      if (activeSource !== '__all__' && source !== activeSource) show = false;
      if (q && !search.includes(q)) show = false;
      if (pMin !== null && (price === 0 || price < pMin)) show = false;
      if (pMax !== null && price > pMax) show = false;
      if (yMin !== null && (year === 0 || year < yMin)) show = false;
      if (yMax !== null && year > yMax) show = false;
      if (dropsOnlyEl.checked && c.dataset.drop !== '1') show = false;

      c.classList.toggle('hidden', !show);
      if (show) visible.push(c);
    }}

    // Sort
    const sortKey = sortEl.value;
    if (sortKey !== 'default') {{
      const cmp = {{
        'year-desc':  (a,b) => (+b.dataset.year) - (+a.dataset.year),
        'year-asc':   (a,b) => (+a.dataset.year) - (+b.dataset.year),
        'price-desc': (a,b) => (+b.dataset.price) - (+a.dataset.price),
        'price-asc':  (a,b) => ((+a.dataset.price || Infinity) - (+b.dataset.price || Infinity)),
        'hours-desc': (a,b) => (+b.dataset.hours) - (+a.dataset.hours),
        'hours-asc':  (a,b) => ((+a.dataset.hours || Infinity) - (+b.dataset.hours || Infinity)),
      }}[sortKey];
      visible.sort(cmp);
      // Re-attach in sorted order (DOM order = visual order)
      for (const c of visible) grid.appendChild(c);
    }}

    countEl.textContent = visible.length;
    emptyEl.style.display = visible.length === 0 ? 'block' : 'none';
  }}

  document.querySelectorAll('.make-chip').forEach(b => b.addEventListener('click', () => {{
    document.querySelectorAll('.make-chip').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    activeMake = b.dataset.make;
    apply();
  }}));
  document.querySelectorAll('.source-chip').forEach(b => b.addEventListener('click', () => {{
    document.querySelectorAll('.source-chip').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    activeSource = b.dataset.source;
    apply();
  }}));
  for (const el of [searchEl, priceMinEl, priceMaxEl, yearMinEl, yearMaxEl, sortEl, dropsOnlyEl]) {{
    el.addEventListener('input', apply);
    el.addEventListener('change', apply);
  }}

  // ---- Map view (Leaflet) ----
  let map = null;
  let markerLayer = null;
  function initMap() {{
    if (map) return;
    map = L.map('map', {{ scrollWheelZoom: true }}).setView([39.8, -98.5], 4);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 18,
    }}).addTo(map);
    markerLayer = L.layerGroup().addTo(map);
  }}
  function renderMarkers() {{
    if (!markerLayer) return;
    markerLayer.clearLayers();
    const visibleCards = cards.filter(c => !c.classList.contains('hidden'));
    let bounds = [];
    for (const c of visibleCards) {{
      const lat = parseFloat(c.dataset.lat);
      const lng = parseFloat(c.dataset.lng);
      if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
      const html = '<img class="popup-thumb" src="' + c.dataset.img + '" alt="">' +
                   '<div class="popup-title">' + c.dataset.title + '</div>' +
                   '<div class="popup-price">' + c.dataset.priceText + '</div>' +
                   '<div class="popup-loc">' + c.dataset.loc + '</div>' +
                   '<a href="' + c.href + '" target="_blank" rel="noopener">View listing →</a>';
      L.marker([lat, lng]).bindPopup(html, {{minWidth: 220}}).addTo(markerLayer);
      bounds.push([lat, lng]);
    }}
    if (bounds.length > 0) map.fitBounds(bounds, {{padding: [30, 30], maxZoom: 10}});
  }}
  document.getElementById('view-grid').addEventListener('click', () => {{
    document.body.classList.remove('map-view');
    document.getElementById('view-grid').classList.add('active');
    document.getElementById('view-map').classList.remove('active');
  }});
  document.getElementById('view-map').addEventListener('click', () => {{
    document.body.classList.add('map-view');
    document.getElementById('view-map').classList.add('active');
    document.getElementById('view-grid').classList.remove('active');
    initMap();
    setTimeout(() => {{ map.invalidateSize(); renderMarkers(); }}, 50);
  }});
  // Re-render markers whenever filters change (only if map is currently shown)
  const origApply = apply;
  apply = function() {{
    origApply();
    if (document.body.classList.contains('map-view')) renderMarkers();
  }};
  // Note: the apply reassignment above doesn't affect existing listeners since
  // they captured the original. Use a wrapper instead.
  function applyAndMap() {{ apply(); }}
  // Already wired above with `apply`. Add a separate listener to re-render markers on filter change.
  for (const el of [searchEl, priceMinEl, priceMaxEl, yearMinEl, yearMaxEl, sortEl, dropsOnlyEl]) {{
    el.addEventListener('input', () => {{ if (document.body.classList.contains('map-view')) renderMarkers(); }});
  }}
  document.querySelectorAll('.chip').forEach(b => b.addEventListener('click', () => {{
    if (document.body.classList.contains('map-view')) renderMarkers();
  }}));
</script>
</body>
</html>
"""

    HTML_PATH.write_text(page, encoding="utf-8")
    DOCS_HTML_PATH.write_text(page, encoding="utf-8")
    return HTML_PATH


if __name__ == "__main__":
    p = render()
    print(f"Wrote {p}")
