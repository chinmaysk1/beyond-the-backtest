-- Beyond the Backtest -- database schema.
--
-- Two principles decide almost everything below.
--
-- 1. `bars` holds RAW PRINTS. No adjustment is ever applied on the way in.
--    Storing split-adjusted prices looks immutable but is not: when a symbol
--    splits, every adjusted price before it silently changes meaning, without
--    a single row being written. Storing what actually traded, and computing
--    the adjustment at read time from `events`, makes each row a permanent
--    fact. That is what lets this database be the single source of truth with
--    no snapshot tier beside it.
--
-- 2. Exactly one row per series is ever rewritten: the forming bar. Everything
--    else is insert-only. See the note above `bars` for how that is bounded.

-- ---------------------------------------------------------------------------
-- Market data
-- ---------------------------------------------------------------------------

-- Raw OHLCV. ts is UTC epoch SECONDS and is the bar's OPEN time.
--
-- The primary key is the merge rule. `INSERT ... ON CONFLICT DO UPDATE` means
-- a re-read of a bar replaces the earlier read of that same bar, atomically and
-- without a lock held in application code. In practice that only ever fires on
-- the newest bar, because older bars are never re-fetched with different
-- values -- but if a venue does correct a bad print, the correction lands
-- rather than being silently dropped.
CREATE TABLE IF NOT EXISTS bars (
    source     text   NOT NULL,
    symbol     text   NOT NULL,
    timeframe  text   NOT NULL,
    ts         bigint NOT NULL,
    open       double precision NOT NULL,
    high       double precision NOT NULL,
    low        double precision NOT NULL,
    close      double precision NOT NULL,
    volume     double precision NOT NULL,
    -- TRUE while this bar's period has not yet elapsed. Research reads exclude
    -- these; a partial bar's "close" is just the current price, and letting one
    -- into a backtest hands the strategy a look at an unfinished future.
    partial    boolean NOT NULL DEFAULT false,
    PRIMARY KEY (source, symbol, timeframe, ts)
);

-- The read path is almost always "one series, ordered, within a window".
CREATE INDEX IF NOT EXISTS bars_series_ts
    ON bars (symbol, timeframe, source, ts);

-- Corporate actions, kept out of `bars` so that table stays append-only.
--
-- Amounts and ratios are stored exactly as the venue reports them. Whether a
-- dividend is itself split-adjusted is a property of the source and is recorded
-- in `series.meta`, not guessed at here.
CREATE TABLE IF NOT EXISTS events (
    source  text   NOT NULL,
    symbol  text   NOT NULL,
    ts      bigint NOT NULL,          -- ex-date, at the session open, UTC
    kind    text   NOT NULL CHECK (kind IN ('split', 'dividend')),
    ratio   double precision,         -- split: 7 for a 7:1
    amount  double precision,         -- dividend: per share, quote currency
    label   text,
    PRIMARY KEY (source, symbol, ts, kind)
);

-- The manifest, as a table. What used to be a JSON sidecar per Parquet file.
-- "Which series are stale?" is now one query instead of a directory walk.
CREATE TABLE IF NOT EXISTS series (
    source         text NOT NULL,
    symbol         text NOT NULL,
    timeframe      text NOT NULL,
    asset_class    text,                    -- 'crypto' | 'equity'
    first_ts       bigint,
    last_ts        bigint,                  -- newest COMPLETE bar
    row_count      integer NOT NULL DEFAULT 0,
    fetched_at     timestamptz,             -- last time new bars arrived
    checked_at     timestamptz,             -- last time we asked, new or not
    venue_timezone text,
    -- Whether the venue's own OHLC arrives adjusted. Yahoo ships split-adjusted
    -- prices, so storing raw prints means UN-adjusting on the way in; this
    -- records that it was done, because getting it wrong is invisible.
    feed_adjustment text,
    integrity      jsonb,                   -- the audit report as of last write
    meta           jsonb,
    PRIMARY KEY (source, symbol, timeframe)
);

-- The curated universe. A public app must not let anyone fetch anything.
CREATE TABLE IF NOT EXISTS universe (
    symbol      text PRIMARY KEY,
    source      text NOT NULL,
    asset_class text NOT NULL,
    sector      text,
    name        text,
    timeframes  text[] NOT NULL,
    since       bigint,                     -- pinned start, for reproducibility
    active      boolean NOT NULL DEFAULT true
);

-- ---------------------------------------------------------------------------
-- Application
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id         uuid PRIMARY KEY,
    email      text UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Login is by username and password, held here rather than delegated to an
-- OAuth provider.
--
-- `password_hash` stores an argon2id digest -- NOT an encoding, and not a fast
-- hash. base64 is reversible, so anyone who reads this table reads every
-- password (including whatever the user reuses elsewhere); SHA-256 is
-- irreversible but a GPU tries billions of guesses a second against it. argon2id
-- is deliberately slow and memory-hard, and its parameters live inside each
-- digest, so they can be raised later without invalidating existing rows.
--
-- Added by ALTER rather than folded into the CREATE above, because the table
-- already exists in the deployed database and this file is applied repeatedly.
ALTER TABLE users ADD COLUMN IF NOT EXISTS username      text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;
ALTER TABLE users ALTER COLUMN email DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS users_username_key ON users (username);

-- A strategy is a validated JSON spec, never code. spec_sha makes a result
-- traceable to the exact spec that produced it even after the user edits it.
CREATE TABLE IF NOT EXISTS strategies (
    id         uuid PRIMARY KEY,
    user_id    uuid REFERENCES users(id),
    name       text NOT NULL,
    spec       jsonb NOT NULL,
    spec_sha   text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- The seam between the web app and the Python worker. The app inserts and
-- returns; the worker claims with SELECT ... FOR UPDATE SKIP LOCKED. No
-- synchronous call ever crosses the runtime boundary.
CREATE TABLE IF NOT EXISTS jobs (
    id          bigserial PRIMARY KEY,
    user_id     uuid REFERENCES users(id),
    kind        text NOT NULL,              -- 'sweep' | 'refresh' | 'backfill'
    payload     jsonb NOT NULL,
    status      text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','done','failed','cancelled')),
    priority    integer NOT NULL DEFAULT 100,
    claimed_by  text,
    claimed_at  timestamptz,
    finished_at timestamptz,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_queue
    ON jobs (status, priority, id) WHERE status = 'queued';

-- One row per parameter combination evaluated.
--
-- `as_of` is what makes a result reproducible. Immutable rows are not enough on
-- their own: the same query next year returns more bars because more bars
-- exist. Re-running with the recorded as_of reproduces the number exactly.
CREATE TABLE IF NOT EXISTS runs (
    id           bigserial PRIMARY KEY,
    job_id       bigint REFERENCES jobs(id),
    strategy_id  uuid REFERENCES strategies(id),
    symbol       text NOT NULL,
    timeframe    text NOT NULL,
    params       jsonb NOT NULL,
    window_start bigint NOT NULL,
    window_end   bigint NOT NULL,
    as_of        bigint NOT NULL,
    split        text,                      -- 'train' | 'test' | 'holdout'
    metrics      jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS runs_lookup
    ON runs (strategy_id, symbol, timeframe, split);

-- Resuming a sweep depends on recognising work already done. Without this, an
-- interrupted sweep silently re-runs everything it already has.
CREATE UNIQUE INDEX IF NOT EXISTS runs_dedupe
    ON runs (strategy_id, symbol, timeframe, split, window_start, window_end,
             md5(params::text));
