"""Send an email digest of newly-found aircraft listings via Resend.

Called from scrape.py at the end of each run. Only sends an email if
there are listings new since the last run AND RESEND_API_KEY is set in
the environment. Silently no-ops otherwise (so local dev runs don't try
to send).

Required env:
  RESEND_API_KEY  — from https://resend.com/api-keys
  EMAIL_TO        — recipient (defaults to edward@samson.law)
  EMAIL_FROM      — sender (defaults to onboarding@resend.dev, no domain
                    verification needed but the FROM name is generic)
"""
from __future__ import annotations

import html
import os
import sys
from typing import Iterable

import requests

RESEND_URL = "https://api.resend.com/emails"


def _render(listings: list[dict], project_url: str) -> tuple[str, str]:
    subject = (
        f"{len(listings)} new aircraft listing"
        + ("s" if len(listings) != 1 else "")
    )
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        "<div style=\"font: 14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;",
        " max-width:640px; margin:0 auto; color:#222;\">",
        f"<h2 style='margin:0 0 4px 0;'>{html.escape(subject)}</h2>",
        f"<p style='color:#666; margin:0 0 16px 0;'>",
        f"View all listings: <a href='{html.escape(project_url, quote=True)}'>{html.escape(project_url)}</a>",
        "</p>",
    ]
    for l in listings:
        title = html.escape(
            f"{l.get('year') or '?'} {l.get('make') or ''} {l.get('model') or ''}".strip()
        )
        price = html.escape(l.get("price") or "Price n/a")
        loc = html.escape(l.get("location") or "")
        url = html.escape(l.get("url") or "#", quote=True)
        img = html.escape(l.get("image_url") or "", quote=True)
        src = html.escape(l.get("source") or "")
        engine = html.escape(l.get("engine") or "")
        tt = html.escape(l.get("total_time") or "")
        img_html = (
            f"<a href='{url}' style='float:left; margin-right:12px;'>"
            f"<img src='{img}' alt='' width='160' height='100' "
            f"style='border-radius:6px; object-fit:cover;'></a>"
        ) if img else ""
        parts.append(
            f"<div style='border-top:1px solid #ddd; padding:12px 0; overflow:hidden;'>"
            f"{img_html}"
            f"<div style='font-weight:600;'>"
            f"<a href='{url}' style='color:#0366d6; text-decoration:none;'>{title}</a>"
            f"</div>"
            f"<div style='color:#0366d6; font-weight:600; font-size:15px;'>{price}</div>"
            f"<div style='color:#555; font-size:12px;'>"
            + " · ".join(p for p in [engine, tt, loc] if p)
            + f"</div>"
            f"<div style='color:#999; font-size:11px; margin-top:4px;'>via {src}</div>"
            f"</div>"
        )
    parts.append("</div>")
    return subject, "".join(parts)


def send(new_listings: Iterable[dict], project_url: str = "") -> None:
    listings = list(new_listings)
    if not listings:
        return

    api_key = os.environ.get("RESEND_API_KEY")
    to_addr = os.environ.get("EMAIL_TO", "edward@samson.law").strip()
    from_addr = os.environ.get(
        "EMAIL_FROM",
        "Aircraft Listings <onboarding@resend.dev>",
    ).strip()

    if not api_key:
        print(
            "  notify: RESEND_API_KEY not set — skipping email",
            file=sys.stderr,
        )
        return

    subject, html_body = _render(listings, project_url)
    try:
        r = requests.post(
            RESEND_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_addr,
                "to": [to_addr],
                "subject": subject,
                "html": html_body,
            },
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"  notify: send failed: {e}", file=sys.stderr)
        return

    if r.status_code >= 300:
        print(
            f"  notify: Resend returned {r.status_code} — {r.text[:200]}",
            file=sys.stderr,
        )
        return

    print(
        f"  notify: emailed {len(listings)} new listing(s) to {to_addr}",
        file=sys.stderr,
    )
