"""Train / test / holdout windows, cut from the bars actually held.

Windows come from the series itself -- its first and newest complete bar -- not
from requested dates. A window built from a guess about how much history
exists can land entirely before the first bar, score zero bars, and report no
error; the earlier prototype did exactly that on a 15m series.

    |<------------- train ------------->|<--- test --->|<-- holdout -->|
    first_ts                                                     last_ts

The holdout is the newest data and is excluded from everything by default. It
is touched once, deliberately, on the one configuration that was chosen. A
sweep that could see it would make it just another test window.
"""
from __future__ import annotations

from dataclasses import dataclass

DAY = 86400


@dataclass(frozen=True)
class Window:
    name: str
    start: int                 # inclusive, bar open ts
    end: int                   # inclusive, bar open ts

    def as_dict(self) -> dict:
        return {"name": self.name, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class Split:
    train: Window
    test: Window
    holdout: Window
    thin_sample: bool

    def full(self) -> Window:
        """Train and test together, still excluding the holdout."""
        return Window("full", self.train.start, self.test.end)

    def as_dict(self) -> dict:
        return {"train": self.train.as_dict(), "test": self.test.as_dict(),
                "holdout": self.holdout.as_dict(), "thin_sample": self.thin_sample}


def split(ts, *, holdout_days: float = 365, holdout_max_frac: float = 0.25,
          train_frac: float = 0.7) -> Split:
    """Cut a series' timestamps into the three windows.

    The holdout is `holdout_days`, capped at `holdout_max_frac` of the span so a
    short series still has something to train on. A series under two years is
    flagged `thin_sample`: whatever it says, it says with little evidence.
    """
    import numpy as np

    ts = np.asarray(ts, dtype="int64")
    if len(ts) < 50:
        raise ValueError(f"only {len(ts)} bars; too few to split into windows")
    first, last = int(ts[0]), int(ts[-1])
    span = last - first
    hold = min(holdout_days * DAY, span * holdout_max_frac)
    hold_start = last - hold
    train_end_t = first + (hold_start - first) * train_frac

    # Snap each boundary to a real bar so every window starts and ends on one.
    def at_or_before(t):
        return int(ts[max(0, np.searchsorted(ts, t, side="right") - 1)])

    def after(t):
        return int(ts[min(len(ts) - 1, np.searchsorted(ts, t, side="right"))])

    train = Window("train", first, at_or_before(train_end_t))
    test = Window("test", after(train.end), at_or_before(hold_start))
    holdout = Window("holdout", after(test.end), last)
    return Split(train, test, holdout, thin_sample=span < 2 * 365 * DAY)
