"""The built-in strategy library: the .pine files beside this module.

Files are the source of truth; the `strategies` table is a copy the web app can
read without reaching into the backend's filesystem. `sync()` checks every
script and inserts any version the table does not have yet.

A row's id derives from its name and source hash, so re-syncing an unchanged
file is a no-op and editing one creates a new version rather than mutating
the old -- every stored run stays attached to the exact source that produced
it.

Each script's grid reproduces the JSON spec it replaced exactly, so a library
strategy is scored over the same N settings as before.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from . import pragmas as PR
from .check import CheckResult, check
from .program import PineError

LIB_DIR = Path(__file__).with_name("library")
_NS = uuid.UUID("5b1f0c8e-9a3d-4c6e-8f21-7d4b2a9e6c13")


def strategy_id(name: str, sha: str) -> str:
    return str(uuid.uuid5(_NS, f"pine:{name}:{sha}"))


def sources() -> dict[str, str]:
    """Every library script's source, by name (the file's stem)."""
    return {f.stem: f.read_text(encoding="utf-8") for f in sorted(LIB_DIR.glob("*.pine"))}


def spec_of(name: str, r: CheckResult) -> dict:
    """What the strategies row holds in `spec` for a Pine script: not code,
    but the description the picker and the results pages read -- the same
    keys the JSON spec had, so those pages need no Pine of their own."""
    m = PR.meta(r.source)
    return {
        "name": name, "title": r.title or name, "language": "pine",
        "description": m.get("description", ""), "style": m.get("style", "other"),
        "timeframes": m.get("timeframes", []),
        "defaults": PR.defaults(r.inputs),
        "labels": {i.name: i.title for i in r.inputs if i.name},
        "grid": r.grid.values,
        "constraints": [" ".join(c) for c in r.grid.constraints],
        "combinations": r.grid.n,
        "costs": r.costs.as_dict(),
    }


def load_all() -> dict[str, CheckResult]:
    """Every library script, checked. Raises on the first that does not pass."""
    out = {}
    for name, src in sources().items():
        r = check(src)
        if not r.ok:
            raise PineError([f"{name}.pine: {m}" for m in r.errors])
        out[name] = r
    return out


def sync(conn) -> list[tuple[str, str]]:
    from .. import db

    done = []
    for name, r in load_all().items():
        sid = strategy_id(name, r.sha)
        db.upsert_pine_strategy(conn, sid, name, spec_of(name, r), r.source, r.sha)
        done.append((name, sid))
    conn.commit()
    return done
