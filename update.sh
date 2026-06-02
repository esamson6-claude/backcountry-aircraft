#!/usr/bin/env bash
# Re-scrape, regenerate the HTML, commit any changes, and push.
# Usage: ./update.sh
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

.venv/bin/python scrape.py

git add data/ docs/
if git diff --staged --quiet; then
  echo "No changes to commit."
  exit 0
fi

git commit -m "Update listings: $(date -u +%Y-%m-%d)"

# The daily GitHub Actions cron writes to the same files. If it ran since
# our last pull, our push will be rejected. Pull --rebase preferring our
# local data ("-X theirs" during rebase = keep the commit being rebased),
# then push.
if ! git push 2>/dev/null; then
  echo "Push rejected — fast-forwarding past remote daily-cron commit…"
  git pull --rebase -X theirs
  git push
fi
echo "Pushed. Pages will rebuild in ~1 minute → https://esamson6-claude.github.io/backcountry-aircraft/"
