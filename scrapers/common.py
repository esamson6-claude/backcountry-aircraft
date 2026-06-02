"""Shared types and helpers for all scrapers."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

# Load .env from project root into os.environ (zero-dep).
_env_path = PROJECT_ROOT / ".env"
if _env_path.is_file():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        os.environ.setdefault(key, val)

# Chromium needs libnspr4/libnss3/libasound2t64/libxss1 from the system. On Ubuntu
# 24.04 WSL without sudo these were extracted by hand to ~/.local/lib/chromium-deps.
# Add that to LD_LIBRARY_PATH so Playwright can find them.
_CHROMIUM_LIB_DIR = Path.home() / ".local/lib/chromium-deps/usr/lib/x86_64-linux-gnu"
if _CHROMIUM_LIB_DIR.is_dir():
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [p for p in existing.split(":") if p]
    if str(_CHROMIUM_LIB_DIR) not in parts:
        parts.insert(0, str(_CHROMIUM_LIB_DIR))
        os.environ["LD_LIBRARY_PATH"] = ":".join(parts)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ScraperFailure(Exception):
    """Raised when a source is unreachable (e.g. paid API credits exhausted).

    scrape.py catches this and preserves the previous CSV rows for the
    affected (source, make) pair, so a temporary outage doesn't wipe data.
    """


@dataclass
class Listing:
    source: str
    url: str
    make: Optional[str] = None  # e.g. "Aviat Husky", "Maule M-5"
    year: Optional[int] = None
    model: Optional[str] = None
    price: Optional[str] = None
    total_time: Optional[str] = None
    location: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None  # used for text filtering (e.g. paint scheme)
    image_url: Optional[str] = None  # primary thumbnail for the HTML view
    engine: Optional[str] = None  # e.g. "Lycoming O-360 180hp"
    engine_time: Optional[str] = None  # e.g. "275 SMOH"

    def as_row(self) -> dict:
        return asdict(self)


def hp_from_model(model: Optional[str]) -> Optional[int]:
    """Extract the horsepower suffix from a model name (e.g. 'A-1C-180' → 180)."""
    if not model:
        return None
    m = re.search(r"-(\d{3})[A-Z]?\b", model)
    if not m:
        return None
    hp = int(m.group(1))
    return hp if 100 <= hp <= 400 else None


_ENGINE_MAKE_RE = re.compile(
    r"\b(LYCOMING|CONTINENTAL|FRANKLIN|ROTAX)\b(?:\s+([A-Z0-9\-]{2,15}))?",
    re.I,
)


def extract_engine(text: Optional[str], model: Optional[str]) -> Optional[str]:
    """Build an engine string from free text + the model's HP suffix."""
    hp = hp_from_model(model)
    if not text:
        return f"{hp} hp" if hp else None
    m = _ENGINE_MAKE_RE.search(text)
    if not m:
        return f"{hp} hp" if hp else None
    make = m.group(1).title()
    sub = (m.group(2) or "").upper().strip("-").strip()
    parts = [make]
    if sub and not sub.isdigit():
        parts.append(sub)
    if hp:
        parts.append(f"{hp} hp")
    return " ".join(parts)


# --- Hours extraction (airframe vs engine) -------------------------------
#
# Sellers write airframe and engine hours with inconsistent terminology:
#
#   Airframe   "1,234 TT", "TT: 1234", "1234 TTAF", "AFTT: 1234 hrs",
#              "ACTT 1234.5", "TTSN 1234" (in non-engine context),
#              "1234 hrs", "Total Time : 1234"
#
#   Engine     "275 SMOH", "Engine 1 Time : 281 SNEW", "300 SOH",
#              "Engine Time: 500", "TBO 2000" (a limit, ignored),
#              "TTSN" inside an "Engine: ..." context = engine total time
#
# The previous version mis-classified `SMOH` as airframe (because it was in
# the airframe regex) and `TTSN` as engine (in the engine regex). Both are
# now cleanly separated and either NN-suffix or suffix-NN orderings are
# accepted. `extract_times_from(text)` returns a normalized pair and applies
# a sanity-swap when engine > airframe.

# Each pattern is structured so that group(1) is ALWAYS the number and
# group(2) is ALWAYS the suffix (or empty). This avoids the bug where
# `Engine 1 Time : 281.61 SNEW` would otherwise capture the "1" from
# "Engine 1 Time" instead of "281".
_NUM = r"(\d{1,3}(?:,\d{3})+|\d{1,5})(?:\.\d+)?"
_NUM_BOUNDARY = r"(?:^|[\s,(])"

_AIRFRAME_PATTERNS = (
    # "Total Time : 1234" (Controller DOM cards) — most specific, try first
    re.compile(rf"Total\s+Time\s*:\s*{_NUM}()", re.I),
    # "TT: 1234" / "AFTT 1234.5" / "ACTT: 1234" / "TTSN: 1234" — label-then-number
    re.compile(
        rf"\b(TT|TTAF|AFTT|ACTT|TTSN)\s*:?\s*{_NUM}\b".replace(
            "(TT|TTAF|AFTT|ACTT|TTSN)", "(?P<lbl1>TT|TTAF|AFTT|ACTT|TTSN)"
        ),
        re.I,
    ),
    # "1,234 hrs TT" / "1234 TTAF" / "2611 hrs"
    re.compile(
        rf"{_NUM_BOUNDARY}{_NUM}\s*(hrs?|hours?|TT|TTAF|AFTT|ACTT|TTSN)\b",
        re.I,
    ),
)

_ENGINE_PATTERNS = (
    # "Engine [N] Time : 281.61 SNEW" — most specific, capture from the
    # post-Time number to avoid catching the engine index.
    re.compile(
        rf"Engine(?:\s*\d+)?\s*Time\s*:?\s*{_NUM}\s*(SNEW|SMOH|SOH|TTSN)?",
        re.I,
    ),
    # "Engine: IO-360 TTSN: 1082.9" / "Engine ... SMOH: 500" — engine context
    # then a label-number pair within ~80 chars.
    re.compile(
        rf"Engine[^.\n]{{0,80}}?(TTSN|SMOH|SOH)\s*:?\s*{_NUM}",
        re.I,
    ),
    # "SMOH: 200" / "SOH 1234" — label-then-number, unambiguous engine marker
    re.compile(rf"\b(SMOH|SOH|SHOT)\s*:?\s*{_NUM}\b", re.I),
    # "275 SMOH" / "1234 SOH" — number-then-label
    re.compile(rf"{_NUM_BOUNDARY}{_NUM}\s*(SMOH|SOH|SHOT)\b", re.I),
)


def _extract_num_and_suffix(
    pat: re.Pattern, text: str
) -> Optional[tuple[str, str]]:
    """Run `pat` against `text`. Pulls the canonical (number, suffix) pair
    by inspecting which capture group holds digits vs letters.
    """
    m = pat.search(text)
    if not m:
        return None
    groups = [g for g in m.groups() if g]
    num, suf = None, ""
    for g in groups:
        if re.fullmatch(r"[\d,]+", g):
            num = g
        elif re.fullmatch(r"[A-Za-z]+", g):
            suf = g.upper()
    if num is None:
        return None
    # Re-attach decimals: scan the full match for "<num>.<dec>"
    full = m.group(0)
    dec_m = re.search(rf"{re.escape(num)}\.(\d+)", full)
    if dec_m:
        num = f"{num}.{dec_m.group(1)}"
    return num, suf


def _parse_hours_value(num_str: Optional[str]) -> Optional[int]:
    if not num_str:
        return None
    try:
        n = int(num_str.replace(",", "").split(".")[0])
    except ValueError:
        return None
    return n if 0 < n <= 25000 else None


def _first_hit(patterns, text: str) -> Optional[tuple[str, str]]:
    for pat in patterns:
        hit = _extract_num_and_suffix(pat, text)
        if hit and _parse_hours_value(hit[0]) is not None:
            return hit
    return None


def _format(hit: Optional[tuple[str, str]]) -> Optional[str]:
    if not hit:
        return None
    num, suf = hit
    if suf in ("HR", "HRS", "HOUR", "HOURS"):
        suf = "hrs"
    return f"{num} {suf}".strip()


def extract_times_from(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return (airframe_total_time, engine_time) from free text.

    No auto-swap — we report exactly what the seller wrote even when the
    numbers are internally inconsistent. The clean label/number capture
    (label dictates which field) is what prevents accidental reversals.
    """
    if not text:
        return None, None
    return _format(_first_hit(_AIRFRAME_PATTERNS, text)), _format(
        _first_hit(_ENGINE_PATTERNS, text)
    )


# Legacy callers — kept for backwards-compat. New code should use extract_times_from.
def extract_engine_time(text: Optional[str]) -> Optional[str]:
    return extract_times_from(text)[1]


def first_hours(text: str) -> Optional[str]:
    return extract_times_from(text)[0]


_PRICE_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def first_price(text: str) -> Optional[str]:
    m = _PRICE_RE.search(text or "")
    return m.group(0) if m else None


def first_year(text: str) -> Optional[int]:
    m = _YEAR_RE.search(text or "")
    return int(m.group(0)) if m else None


def save_raw(name: str, html: str) -> None:
    (RAW_DIR / f"{name}.html").write_text(html, encoding="utf-8")
