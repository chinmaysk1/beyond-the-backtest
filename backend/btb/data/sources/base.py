"""What a data source must provide, and nothing more.

Two methods and two attributes. The seam is deliberately narrow: adding a venue
should be a config change, and the caller should never need to know which one it
is talking to.

    name            str   short id, used as the cache directory
    regular         bool  does this instrument trade on an unbroken grid?
                          (crypto: yes. equities: no -- nights and weekends.)
    fetch(...)      -> canonical DataFrame
    events(symbol)  -> {"dividends": [...], "splits": [...]}   ({} if none)

`regular` exists because the gap check has to be an ERROR for crypto and a WARN
for equities, and that distinction belongs to the instrument, not to the caller
remembering to pass a flag.
"""
from __future__ import annotations

from typing import Protocol

import pandas as pd


class Source(Protocol):
    name: str
    regular: bool

    def fetch(self, symbol: str, timeframe: str, *, since: int | None = None,
              until: int | None = None) -> pd.DataFrame:
        ...

    def events(self, symbol: str) -> dict:
        ...

    def describe(self, symbol: str) -> dict:
        """Provenance worth pinning into the manifest (venue tz, currency, ...)."""
        ...
