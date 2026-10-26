"""The Pine contract, as one text: REFERENCE.md beside this module.

One file serves three readers -- the MCP `get_reference` tool, the web app's
copy-prompt, and `python -m btb.engine reference` -- so what a model is told
cannot drift between them. The web app reads the same file at build time.
"""
from __future__ import annotations

from pathlib import Path

PATH = Path(__file__).with_name("REFERENCE.md")


def text() -> str:
    return PATH.read_text(encoding="utf-8")
