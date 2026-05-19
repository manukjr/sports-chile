#!/usr/bin/env python3
"""
run_ci.py — headless wrapper for GitHub Actions.

Differences from running sports_ci.py directly:
  - No interactive date menu (uses CLI arg or today in Santiago time)
  - No browser auto-open
  - Copies the generated file to docs/index.html for GitHub Pages

Required env vars (passed as GitHub Actions secrets):
    API_FOOTBALL_KEY   — api-sports.io
    RAPIDAPI_KEY       — RapidAPI (Matchstat Tennis)

Usage:
    python run_ci.py              # today in CLT
    python run_ci.py 2026-06-24   # specific date
"""

import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ── 1. Suppress browser auto-open ────────────────────────────────────────────
webbrowser.open = lambda *a, **kw: None

# ── 2. Inject today's date if no arg given ────────────────────────────────────
if len(sys.argv) < 2:
    today = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")
    sys.argv.append(today)

# ── 3. Run the CI script (api-football + Matchstat, datacenter-friendly) ─────
import asyncio
import sports_ci

asyncio.run(sports_ci.main())

# ── 4. Copy output to docs/index.html for GitHub Pages ──────────────────────
date_str = sys.argv[1]
src  = Path(f"deportes_{date_str}.html")
dest_dir = Path("docs")
dest_dir.mkdir(exist_ok=True)

if src.exists():
    (dest_dir / "index.html").write_bytes(src.read_bytes())
    print(f"Copied → docs/index.html")
else:
    print(f"WARNING: {src} not found — nothing copied to docs/")
