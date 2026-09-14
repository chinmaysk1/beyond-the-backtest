"""Import a CSV of bars that came from somewhere else.

Two jobs. It lets a dataset include history the live sources cannot reach -- a
vendor export, an archive, a hand-collected series -- and it is the path by
which any future engine cross-check runs on *identical bars* rather than on two
feeds that merely look similar.

The only accommodation made for other people's files is the timestamp column:
`ts` is canonical, `time` and `date` are accepted and renamed. Everything else
has to arrive as open/high/low/close/volume, and goes through the same
`normalise` and the same integrity check as a live fetch. An imported series is
not more trusted for having been imported.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..bars import COLUMNS, normalise

ALIASES = {"time": "ts", "date": "ts", "timestamp": "ts", "vol": "volume"}


class CsvSource:
    regular = True
    corporate_actions = False

    def __init__(self, path, name: str = "csv", regular: bool = True):
        self.path = Path(path)
        self.name = name
        self.regular = regular

    def fetch(self, symbol: str, timeframe: str, *, since=None, until=None) -> pd.DataFrame:
        df = pd.read_csv(self.path)
        df = df.rename(columns={c: ALIASES[c.lower()] for c in df.columns
                                if c.lower() in ALIASES and c != "ts"})
        missing = [c for c in COLUMNS if c not in df.columns and c != "volume"]
        if missing:
            raise ValueError(f"{self.path.name} is missing columns {missing}; "
                             f"it has {list(df.columns)}")
        if "volume" not in df.columns:
            df["volume"] = 0.0

        out = normalise(df[COLUMNS].itertuples(index=False, name=None))
        if since is not None:
            out = out[out["ts"] >= since]
        if until is not None:
            out = out[out["ts"] <= until]
        return out.reset_index(drop=True)

    def events(self, symbol: str) -> dict:
        return {"dividends": [], "splits": []}

    def describe(self, symbol: str) -> dict:
        return {"venue": "file", "venue_timezone": "UTC", "adjustment": "unknown",
                "path": str(self.path)}
