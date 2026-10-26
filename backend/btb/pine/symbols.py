"""Which of our markets a `request.security` on another symbol means.

A TradingView script names symbols the way TradingView does --
"BITSTAMP:ETHUSD", "AMEX:SPY", "ETHUSD" -- and timeframes as Pine strings
("240", "1D"). This maps both onto the curated universe and the timeframes we
store, so a script can read another market we hold. Anything else is refused
by name: a request we cannot feed would otherwise read `na` on every bar and
quietly turn the strategy into a different one.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

UNIVERSE_FILE = Path(__file__).resolve().parents[2] / "universe.json"

# Pine timeframe strings -> ours. Only timeframes we store.
TIMEFRAMES = {"5": "5m", "15": "15m", "30": "30m", "60": "1h", "1H": "1h", "240": "4h",
              "4H": "4h", "D": "1d", "1D": "1d"}
TF_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def _norm(s: str) -> str:
    s = s.strip().upper()
    s = s.split(":", 1)[1] if ":" in s else s
    return s.replace("/", "").replace("-", "").replace("_", "")


@lru_cache(maxsize=1)
def _universe() -> dict[str, tuple[str, str]]:
    spec = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
    out = {}
    for asset_class, group in spec.items():
        if asset_class.startswith("_"):
            continue
        for s in group["symbols"]:
            out[_norm(s["symbol"])] = (s["symbol"], asset_class)
    return out


def resolve(key: str) -> tuple[str, str] | None:
    """(our symbol, asset class) for a TradingView-style symbol, or None."""
    return _universe().get(_norm(key))


def timeframe(pine_tf: str) -> str | None:
    return TIMEFRAMES.get(pine_tf.strip())
