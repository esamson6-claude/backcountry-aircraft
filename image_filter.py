"""Classify listing thumbnails so the HTML can hide person-photo listings.

Some listings use a broker headshot or a person as the thumbnail instead of the
aircraft, which looks wrong in the grid. We ask Claude (Haiku, vision) whether
each thumbnail's main subject is a person rather than an aircraft, and cache the
verdict per LISTING URL in data/image_cache.json:

    { "<listing-url>": {"has_human": false}, ... }

The cache is keyed by the listing URL (stable across scrapes), NOT the image URL
— the marketplaces serve image URLs with changing resize/watermark params, so
keying by image would re-classify (and re-bill) nearly every listing each run.

Idempotent — only listings missing from the cache (or that previously errored)
hit the API. No-ops cleanly if ANTHROPIC_API_KEY isn't set, so local runs and the
daily job still build the site without it.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CSV_PATH = PROJECT_ROOT / "data" / "listings.csv"
CACHE_PATH = PROJECT_ROOT / "data" / "image_cache.json"

MODEL = "claude-haiku-4-5"
PROMPT = (
    "This is the thumbnail photo from an aircraft-for-sale listing. Is the MAIN "
    "subject of the photo a person (for example a broker/owner headshot or "
    "portrait) rather than an aircraft? A photo of an aircraft that merely "
    "happens to have a small person in or near it is NOT a person photo."
)
_SCHEMA = {
    "type": "object",
    "properties": {"has_human": {"type": "boolean"}},
    "required": ["has_human"],
    "additionalProperties": False,
}


def load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, sort_keys=True, indent=2))


def classify_image(client, url: str) -> dict:
    """Return {"has_human": bool} or {"error": str} for one image URL."""
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "url", "url": url}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return {"has_human": bool(json.loads(text)["has_human"])}
    except Exception as e:
        return {"error": str(e)[:160]}


def _needs_classify(url: str, cache: dict[str, dict]) -> bool:
    if url not in cache:
        return True
    return "error" in cache[url]  # retry transient failures; keep resolved verdicts


def main() -> int:
    try:
        from scrapers import common  # noqa: F401 — loads .env into os.environ
    except Exception:
        pass
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  image filter: ANTHROPIC_API_KEY not set — skipping", file=sys.stderr)
        return 0
    if not CSV_PATH.exists():
        print(f"{CSV_PATH} not found — run scrape.py first", file=sys.stderr)
        return 1

    try:
        import anthropic
    except ImportError:
        print("  image filter: anthropic package not installed — skipping", file=sys.stderr)
        return 0

    cache = load_cache()
    rows = list(csv.DictReader(CSV_PATH.open(newline="", encoding="utf-8")))

    # Build (listing_url, image_url) work items. Cache key = listing URL (stable);
    # the image URL is still what we send to the model.
    todo = []  # (listing_url, image_url)
    seen = set()
    for r in rows:
        lu = (r.get("url") or "").strip()
        img = (r.get("image_url") or "").strip()
        if not lu or lu in seen or not img.startswith("http"):
            continue
        seen.add(lu)
        if _needs_classify(lu, cache):
            todo.append((lu, img))

    if not todo:
        humans = sum(1 for u in seen if cache.get(u, {}).get("has_human"))
        print(f"  image filter: cache complete ({len(cache)} entries, {humans} person-photos)", file=sys.stderr)
        return 0

    client = anthropic.Anthropic()
    print(f"  image filter: classifying {len(todo)} new listing thumbnail(s)…", file=sys.stderr)
    for i, (lu, img) in enumerate(todo):
        result = classify_image(client, img)
        # Abort the whole step on an account-level failure (no credit, bad key,
        # permission) instead of hammering every image with the same 400/401.
        err = result.get("error", "")
        if err and any(s in err.lower() for s in
                       ("credit balance", "authentication", "permission", "invalid x-api-key")):
            print(f"  image filter: account error — aborting step ({err})", file=sys.stderr)
            save_cache(cache)
            return 0
        cache[lu] = result
        if (i + 1) % 25 == 0:
            save_cache(cache)
            print(f"    progress: {i+1}/{len(todo)}", file=sys.stderr)
        time.sleep(0.2)  # gentle pacing under the Haiku rate limit

    save_cache(cache)
    humans = sum(1 for u in seen if cache.get(u, {}).get("has_human"))
    errors = sum(1 for u in seen if "error" in cache.get(u, {}))
    print(
        f"  image filter: done — {humans} person-photo listing(s) flagged"
        f"{f', {errors} error(s)' if errors else ''}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
