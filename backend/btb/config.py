"""Paths and the few global defaults. One place, no hidden lookups.

Everything here is explicit, and everything that affects a result is written
into the series manifest at ingest time. A value silently inherited from
somewhere else -- a default that is not the tested default, a setting left over
from a previous run -- is the most expensive class of bug in work like this,
because it changes the numbers without changing anything you can see.
"""
from __future__ import annotations

import os
from pathlib import Path

# backend/ -- the Python project root. The repo root is one level above, and
# holds `web/` alongside it.
ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent

# The curated universe: the complete list of what may be fetched. A public app
# must never let a visitor trigger an arbitrary request.
UNIVERSE_FILE = ROOT / "universe.json"

# Network manners. Both sources are free and keyless; do not hammer them.
HTTP_TIMEOUT = int(os.environ.get("BTB_HTTP_TIMEOUT", "30"))
HTTP_UA = "btb/0.1 (senior project; research use)"
