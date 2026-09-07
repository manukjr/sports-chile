#!/bin/bash
# push_today.sh — scrape today's sports and push to GitHub Pages
#
# Add to crontab (runs every day at 08:00):
#   crontab -e
#   0 8 * * * /Users/MBM/PycharmProjects/edhec/scraper/sports-chile/push_today.sh >> /tmp/sports-chile.log 2>&1

set -e

REPO="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$REPO"

echo "──────────────────────────────────────────"
echo "$(date '+%Y-%m-%d %H:%M:%S') — Starting sports scraper"

# Generate HTML → docs/index.html
/Library/Frameworks/Python.framework/Versions/3.10/bin/python3.10 run_ci.py

# Push to GitHub if docs/index.html changed
git add docs/index.html

if git diff --cached --quiet; then
    echo "No changes — nothing to push."
else
    DATE=$(/Library/Frameworks/Python.framework/Versions/3.10/bin/python3.10 \
        -c "from datetime import datetime; from zoneinfo import ZoneInfo; \
            print(datetime.now(ZoneInfo('America/Santiago')).strftime('%Y-%m-%d'))")
    git commit -m "Deportes $DATE"
    git push
    echo "Pushed deportes $DATE to GitHub."
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') — Done."
