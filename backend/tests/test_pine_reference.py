"""The Pine contract is written once, in btb/pine/REFERENCE.md, and copied into
the web app for its copy-prompt and the MCP server. A model told one thing by
the MCP tool and another by the prompt would be a bug nobody sees, so the copy
is checked here. Fix with: node web/scripts/sync-reference.mjs"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from btb.pine import reference

WEB_COPY = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "pineReference.ts"


def test_web_copy_matches_reference():
    if not WEB_COPY.exists():
        pytest.skip("web app not checked out beside the backend")
    m = re.search(r"PINE_REFERENCE: string = (\".*\");\s*$", WEB_COPY.read_text(encoding="utf-8"), re.S)
    assert m, "pineReference.ts has an unexpected shape; regenerate it"
    assert json.loads(m.group(1)) == reference.text(), \
        "web/src/lib/pineReference.ts is stale: run node web/scripts/sync-reference.mjs"
