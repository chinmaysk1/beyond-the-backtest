"""Source registry -- the one place that maps a name to an implementation.

The venue is a config value, not a rewrite. Adding Alpaca (for equities, once a
key exists) or a second crypto exchange means one new class and one line here.
"""
from __future__ import annotations

from .base import Source
from .crypto import CryptoSource, DEFAULT_VENUE
from .equities import EquitySource
from .csvfile import CsvSource

__all__ = ["Source", "CryptoSource", "EquitySource", "CsvSource", "make_source"]


def make_source(kind: str, **kw) -> Source:
    """kind: 'crypto' | 'equity' | 'csv'."""
    k = kind.lower()
    if k in ("crypto", "ccxt"):
        return CryptoSource(kw.get("venue") or DEFAULT_VENUE)
    if k in ("equity", "equities", "stock", "yahoo"):
        return EquitySource()      # always raw; adjust at read time
    if k == "csv":
        return CsvSource(kw["path"], name=kw.get("name", "csv"))
    raise ValueError(f"unknown source kind {kind!r}; expected crypto, equity or csv")
