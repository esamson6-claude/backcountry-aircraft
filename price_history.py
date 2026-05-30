"""Track price changes per listing URL across scrape runs.

Stores a per-URL price log in `data/price_history.json`:

    {
      "<listing-url>": [
        {"date": "2026-05-30", "price": 365000},
        {"date": "2026-06-04", "price": 349000},
        ...
      ]
    }

`update_history(current_rows)` is called once per scrape run; it appends
today's price to each listing's log only when the price has actually
changed. It returns the subset of drops (current < previous), which the
caller can surface in the email digest and the HTML view.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent
HISTORY_PATH = PROJECT_ROOT / "data" / "price_history.json"


def _parse_price(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\$?([\d,]+(?:\.\d{2})?)", text)
    if not m:
        return None
    digits = m.group(1).replace(",", "")
    try:
        val = float(digits)
    except ValueError:
        return None
    if val < 1000:  # reject obvious non-prices
        return None
    return int(val)


def load_history() -> dict[str, list[dict]]:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_history(history: dict) -> None:
    HISTORY_PATH.write_text(json.dumps(history, sort_keys=True, indent=2))


def update_history(current_rows: Iterable[dict]) -> list[dict]:
    """Append today's price for each listing if changed. Return any drops."""
    history = load_history()
    today = date.today().isoformat()
    drops: list[dict] = []

    for row in current_rows:
        url = row.get("url") or ""
        price = _parse_price(row.get("price") or "")
        if not url or price is None:
            continue

        entries = history.setdefault(url, [])
        if not entries:
            entries.append({"date": today, "price": price})
            continue

        last = entries[-1]
        if last["price"] == price:
            continue

        entries.append({"date": today, "price": price})
        if price < last["price"]:
            drops.append({
                "url": url,
                "previous_price": last["price"],
                "current_price": price,
                "previous_date": last["date"],
                "date": today,
                "pct_change": round((price - last["price"]) / last["price"] * 100, 1),
                "delta": price - last["price"],
                "row": row,
            })

    save_history(history)
    return drops


def recent_drops_by_url(days: int = 14) -> dict[str, dict]:
    """Build a {url: {prev_price, current_price, days_ago}} map of recent drops.

    Used by the HTML generator to badge cards. A drop counts if the latest
    history entry is lower than the prior one AND the latest entry's date
    is within `days` days of today.
    """
    history = load_history()
    today = date.fromisoformat(date.today().isoformat())
    out: dict[str, dict] = {}
    for url, entries in history.items():
        if len(entries) < 2:
            continue
        last, prev = entries[-1], entries[-2]
        if last["price"] >= prev["price"]:
            continue
        try:
            last_date = date.fromisoformat(last["date"])
        except ValueError:
            continue
        ago = (today - last_date).days
        if ago > days:
            continue
        out[url] = {
            "previous_price": prev["price"],
            "current_price": last["price"],
            "delta": last["price"] - prev["price"],
            "pct_change": round((last["price"] - prev["price"]) / prev["price"] * 100, 1),
            "days_ago": ago,
        }
    return out
