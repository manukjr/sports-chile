#!/usr/bin/env python3
"""
run_ci.py — headless wrapper for GitHub Actions.

Differences from running sports.py directly:
  - No interactive date menu (uses CLI arg or today in Santiago time)
  - No browser auto-open
  - Copies the generated file to public/index.html for GitHub Pages

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

# ── 3. Run the real script ────────────────────────────────────────────────────
import asyncio
import sports

asyncio.run(sports.main())

# ── 4. Copy output to public/index.html for GitHub Pages ─────────────────────
date_str = sys.argv[1]
src  = Path(f"deportes_{date_str}.html")
dest_dir = Path("public")
dest_dir.mkdir(exist_ok=True)

if src.exists():
    (dest_dir / "index.html").write_bytes(src.read_bytes())
    (dest_dir / src.name).write_bytes(src.read_bytes())
    print(f"Copied → public/index.html  and  public/{src.name}")
else:
    print(f"WARNING: {src} not found — nothing copied to public/")
