"""The built-in strategy library: the JSON files beside this module.

Files are the source of truth; the `strategies` table is a copy the web app can
read without reaching into the backend's filesystem. `sync()` loads every spec,
validates it, and inserts any version the table does not have yet.

A strategy's id is derived from its name and spec hash, so re-syncing an
unchanged file is a no-op and editing one creates a new version rather than
mutating the old -- every stored run stays attached to the exact spec that
produced it.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from . import spec as S

LIB_DIR = Path(__file__).with_name("library")
_NS = uuid.UUID("5b1f0c8e-9a3d-4c6e-8f21-7d4b2a9e6c13")


def strategy_id(name: str, sha: str) -> str:
    return str(uuid.uuid5(_NS, f"{name}:{sha}"))


def load_all() -> dict[str, dict]:
    """Every library spec, by name. Raises on the first invalid one."""
    out: dict[str, dict] = {}
    for f in sorted(LIB_DIR.glob("*.json")):
        spec = json.loads(f.read_text(encoding="utf-8"))
        errs = S.validate(spec)
        if errs:
            raise S.SpecError([f"{f.name}: {e}" for e in errs])
        if spec["name"] != f.stem:
            raise S.SpecError([f"{f.name}: name {spec['name']!r} must match the file name"])
        out[spec["name"]] = spec
    return out


def get(name: str) -> dict:
    lib = load_all()
    if name not in lib:
        raise KeyError(f"no strategy {name!r}; have {sorted(lib)}")
    return lib[name]


def sync(conn) -> list[tuple[str, str]]:
    from .. import db

    done = []
    for name, spec in load_all().items():
        sha = S.spec_sha(spec)
        sid = strategy_id(name, sha)
        db.upsert_strategy(conn, sid, name, spec, sha)
        done.append((name, sid))
    conn.commit()
    return done
