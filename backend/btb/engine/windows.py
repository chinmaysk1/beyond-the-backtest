"""Train / test / holdout windows, cut from the bars actually held.

Windows come from the series itself -- its first and newest complete bar -- not
from requested dates. A window built from a guess about how much history
exists can land entirely before the first bar, score zero bars, and report no
error; the earlier prototype did exactly that on a 15m series.

    |<------------- train ------------->|<--- test --->|<-- holdout -->|
    first_ts                                                     last_ts

The holdout is everything after the test window, up to the newest complete
bar. Nothing that picks or scores settings reads it: the sweep ranks on test,
and the verdict scores the default windows. A backtest runs the chosen
settings over it once, so the page can show how they did on data that came
after everything they were chosen on. Paper trading then carries on from live.

Train and test can be moved (`custom`): train may start later than the first
bar, the boundary can slide, and test may end earlier or later. The holdout
follows: it always starts on the bar after test ends. Test may not end so late
that the holdout keeps fewer than MIN_WINDOW_BARS bars.

The verdict's own holdout is the default one, which the moved windows never
change: the score is always computed on the default train and test.
"""
from __future__ import annotations

from dataclasses import dataclass

DAY = 86400
# The least a moved window may hold. Fewer bars than this and the numbers are
# noise; the default windows are exempt so a short series can still run.
MIN_WINDOW_BARS = 100


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

    def through(self) -> Window:
        """Train start to the newest bar: one continuous run whose curve the
        page draws, with the holdout as its last stretch."""
        return Window("all", self.train.start, self.holdout.end)

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


class WindowError(ValueError):
    """Window overrides that cannot be used. The message names the field."""


def custom(ts, windows: dict | None, **kw) -> Split:
    """The default split with train and test moved to `windows`.

    `windows` is {"train": [start, end], "test": [start, end]}, unix seconds.
    Starts snap forward to the first bar at or after them and ends back to the
    last bar at or before, so any timestamp inside the series lands on a real
    bar. Test must begin on the bar right after train ends: a gap between them
    would be data neither window is judged on, for no reason.

    The holdout follows test: it starts on the next bar and runs to the newest.
    None or empty returns `split()`, and so do overrides that snap to exactly
    the default windows -- a reset is the default run, and dedupes with it.
    """
    import numpy as np

    base = split(ts, **kw)
    if not windows:
        return base
    if not isinstance(windows, dict):
        raise WindowError("windows: must be an object with train and test")
    extra = set(windows) - {"train", "test"}
    if extra:
        raise WindowError(f"windows.{sorted(extra)[0]}: unknown window; only train "
                          "and test can be moved (the holdout follows test)")
    ts = np.asarray(ts, dtype="int64")
    first, last_open = int(ts[0]), movable(ts, base)["end"]   # latest test may end

    def pair(name):
        v = windows.get(name)
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise WindowError(f"windows.{name}: must be [start, end] in unix seconds")
        out = []
        for i, t in enumerate(v):
            if isinstance(t, bool) or not isinstance(t, (int, float)) or t != t                     or t in (float("inf"), float("-inf")) or int(t) != t:
                raise WindowError(f"windows.{name}[{i}]: must be a whole number of seconds")
            out.append(int(t))
        return out

    raw = {"train": pair("train"), "test": pair("test")}
    for name, (a, b) in raw.items():
        if a > b:
            raise WindowError(f"windows.{name}: start is after end")
        if a < first:
            raise WindowError(f"windows.{name}[0]: before the first bar ({_day(first)})")
    if raw["test"][1] > last_open and int(np.searchsorted(ts, raw["test"][1], side="right")) - 1 \
            > int(np.searchsorted(ts, last_open, side="right")) - 1:
        raise WindowError(f"windows.test[1]: leaves the holdout fewer than {MIN_WINDOW_BARS} "
                          f"bars; test must end by {_day(last_open)}")
    if raw["train"][1] >= raw["test"][0]:
        raise WindowError("windows.test[0]: test must start after train ends")

    # Snap: starts forward, ends back. Every end is already at or before the
    # last bar test may take, so each lands on a bar in range or empties.
    def first_at_or_after(t):
        return int(np.searchsorted(ts, t, side="left"))

    def last_at_or_before(t):
        return int(np.searchsorted(ts, t, side="right")) - 1

    idx = {n: (first_at_or_after(a), last_at_or_before(b)) for n, (a, b) in raw.items()}
    for name, (i0, i1) in idx.items():
        if i1 < i0:
            raise WindowError(f"windows.{name}: contains no bars")
    if idx["test"][0] != idx["train"][1] + 1:
        raise WindowError(f"windows.test[0]: test must start on the bar after train ends "
                          f"({_day(int(ts[idx['train'][1] + 1]))}); there are "
                          f"{idx['test'][0] - idx['train'][1] - 1} bars between them")
    train = Window("train", int(ts[idx["train"][0]]), int(ts[idx["train"][1]]))
    test = Window("test", int(ts[idx["test"][0]]), int(ts[idx["test"][1]]))
    if (train, test) == (base.train, base.test):
        return base
    for name, (i0, i1) in idx.items():
        if i1 - i0 + 1 < MIN_WINDOW_BARS:
            raise WindowError(f"windows.{name}: {i1 - i0 + 1} bars; a moved window needs "
                              f"at least {MIN_WINDOW_BARS}")
    # Belt and braces: the holdout keeps its minimum, whatever got through above.
    assert test.end <= last_open
    holdout = Window("holdout", int(ts[idx["test"][1] + 1]), int(ts[-1]))
    return Split(train, test, holdout,
                 thin_sample=base.thin_sample or test.end - train.start < 2 * 365 * DAY)


def movable(ts, base: Split) -> dict:
    """What the UI may move train and test within: the first bar through the
    latest bar test may end on, how many bars that is, and the least a moved
    window may hold. Lets the chart draw and clamp handles before asking.

    Test may run into the default holdout as long as MIN_WINDOW_BARS bars stay
    after it. A series whose default holdout is already shorter than that keeps
    the default test end as the limit."""
    import numpy as np

    ts = np.asarray(ts, dtype="int64")
    end = max(base.test.end, int(ts[-1 - MIN_WINDOW_BARS])) if len(ts) > MIN_WINDOW_BARS + 1 \
        else base.test.end
    bars = int(np.searchsorted(ts, end, side="right"))
    return {"start": int(ts[0]), "end": end, "bars": bars, "min_bars": MIN_WINDOW_BARS}


def _day(t: int) -> str:
    import time
    return time.strftime("%Y-%m-%d", time.gmtime(t))
