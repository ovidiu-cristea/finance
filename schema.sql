-- Local holdings database for Fidelity accounts tracked via SnapTrade.
--
-- Sync model:
--   * SnapTrade pulls write `accounts`, `securities`, `sync_runs` and append
--     dated rows to `holding_snapshots`.
--   * Executed orders are ingested into `executed_orders` (idempotent on the
--     brokerage order id). BUYs auto-open a `tax_lots` row. SELLs are matched
--     to a pre-recorded `planned_orders` intent; a clean match applies the
--     intent's `planned_order_lots` to `tax_lots` and writes `realized_events`.
--   * Anything that can't be matched exactly is flagged needs_review and left
--     for manual reconciliation (UI to come later).
--
-- Foreign keys are enforced per-connection: callers must run
--   PRAGMA foreign_keys = ON;
-- after opening the database (sqlite3 defaults it off).

-- ---------------------------------------------------------------------------
-- Reference / synced metadata
-- ---------------------------------------------------------------------------

-- One row per brokerage account. SnapTrade fields are synced; account_type /
-- owner / notes are filled in manually.
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,   -- SnapTrade account id
    institution   TEXT,               -- synced
    name          TEXT,               -- synced
    number        TEXT,               -- synced (masked)
    status        TEXT,               -- synced
    account_type  TEXT,               -- manual: taxable / traditional-IRA / roth-IRA / HSA ...
    owner         TEXT,               -- manual
    notes         TEXT,               -- manual
    updated_at    TEXT                -- ISO timestamp of last sync touch
);

-- Instrument reference data, upserted on each sync so snapshots stay skinny.
CREATE TABLE IF NOT EXISTS securities (
    symbol        TEXT PRIMARY KEY,   -- e.g. AAPL
    raw_symbol    TEXT,
    description   TEXT,
    kind          TEXT                -- equity / etf / option / cash ...
);

-- ---------------------------------------------------------------------------
-- Holdings history (append-only)
-- ---------------------------------------------------------------------------

-- One row per pull from SnapTrade. Snapshots reference the run that produced them.
CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT NOT NULL,            -- ISO timestamp of the pull
    data_freshness  TEXT,                     -- from SnapTrade response, if present
    source          TEXT NOT NULL DEFAULT 'snaptrade'
);

-- Append-only dated holdings. Never updated in place.
CREATE TABLE IF NOT EXISTS holding_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES sync_runs(id),
    account_id    TEXT    NOT NULL REFERENCES accounts(id),
    symbol        TEXT    NOT NULL REFERENCES securities(symbol),
    units         REAL,
    price         REAL,                        -- per-share market price (synced)
    cost_basis_ps REAL,                        -- SnapTrade avg cost per share, may be NULL
    currency      TEXT,
    market_value  REAL,                        -- units * price
    UNIQUE (run_id, account_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_acct_sym
    ON holding_snapshots (account_id, symbol);

-- ---------------------------------------------------------------------------
-- Tax lots (seeded manually, then maintained by the sync)
-- ---------------------------------------------------------------------------

-- Open positions broken down by acquisition lot. Seeded manually from Fidelity
-- for positions that predate sync coverage; thereafter each executed BUY opens
-- a new lot and each matched SELL relieves remaining_quantity from lots.
CREATE TABLE IF NOT EXISTS tax_lots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id         TEXT NOT NULL REFERENCES accounts(id),
    symbol             TEXT NOT NULL REFERENCES securities(symbol),
    open_date          TEXT,                   -- ISO date the lot was acquired
    original_quantity  REAL NOT NULL,
    remaining_quantity REAL NOT NULL,          -- decremented as lots are sold
    price_per_share    REAL,                   -- acquisition price per share
    cost_basis         REAL,                   -- total cost of the original lot
    source             TEXT NOT NULL DEFAULT 'seed',   -- 'seed' | 'sync'
    buy_order_id       TEXT,                   -- brokerage_order_id that opened it (NULL for seeds)
    status             TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'closed'
    notes              TEXT,
    target_min_profit_pct REAL                 -- manual: min % profit to sell this lot (e.g. 10 = 10%)
);

CREATE INDEX IF NOT EXISTS idx_taxlots_acct_sym
    ON tax_lots (account_id, symbol, status);

-- ---------------------------------------------------------------------------
-- Planned orders (recorded at placement time, with lot intent for sells)
-- ---------------------------------------------------------------------------

-- An order placed at Fidelity, recorded locally. For sells this captures the
-- specific-lot intent that Fidelity does not expose after the fact, plus the
-- limit/stop price the sync uses to confirm a match against an execution.
CREATE TABLE IF NOT EXISTS planned_orders (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id         TEXT NOT NULL REFERENCES accounts(id),
    symbol             TEXT NOT NULL REFERENCES securities(symbol),
    action             TEXT NOT NULL,          -- BUY / SELL
    quantity           REAL NOT NULL,
    order_type         TEXT,                   -- Limit / StopLimit / ...
    limit_price        REAL,
    stop_price         REAL,
    placed_date        TEXT,                   -- ISO date the order was placed
    status             TEXT NOT NULL DEFAULT 'pending',  -- pending | matched | cancelled | expired
    brokerage_order_id TEXT,                   -- set when matched to an execution
    notes              TEXT
);

CREATE INDEX IF NOT EXISTS idx_planned_pending
    ON planned_orders (account_id, symbol, status);

-- The specific lots a planned SELL intends to dispose, with share counts.
CREATE TABLE IF NOT EXISTS planned_order_lots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    planned_order_id INTEGER NOT NULL REFERENCES planned_orders(id),
    lot_id           INTEGER NOT NULL REFERENCES tax_lots(id),
    quantity         REAL NOT NULL             -- shares to relieve from this lot
);

CREATE INDEX IF NOT EXISTS idx_planned_lots_order
    ON planned_order_lots (planned_order_id);

-- ---------------------------------------------------------------------------
-- Executed orders (ingested from SnapTrade; idempotency + review queue)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS executed_orders (
    brokerage_order_id TEXT PRIMARY KEY,       -- from SnapTrade, unique per order
    account_id         TEXT NOT NULL REFERENCES accounts(id),
    symbol             TEXT REFERENCES securities(symbol),
    action             TEXT,                   -- BUY / SELL / ...
    total_quantity     REAL,
    filled_quantity    REAL,
    execution_price    REAL,
    order_type         TEXT,
    time_placed        TEXT,
    time_executed      TEXT,
    status             TEXT,                   -- SnapTrade order status
    first_seen_run_id  INTEGER REFERENCES sync_runs(id),
    planned_order_id   INTEGER REFERENCES planned_orders(id),  -- set when matched
    applied            INTEGER NOT NULL DEFAULT 0,  -- 1 once lots have been applied
    needs_review       INTEGER NOT NULL DEFAULT 0,  -- 1 when auto-match failed
    notes              TEXT
);

CREATE INDEX IF NOT EXISTS idx_executed_review
    ON executed_orders (needs_review, applied);

-- ---------------------------------------------------------------------------
-- Realized events (sync-generated lot closures + manual dividends)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS realized_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT NOT NULL REFERENCES accounts(id),
    symbol       TEXT REFERENCES securities(symbol),  -- NULL allowed (e.g. cash dividend)
    event_date   TEXT,
    event_type   TEXT NOT NULL,                -- dividend | realized_gain | realized_loss
    quantity     REAL,                         -- shares disposed (NULL for dividends)
    lot_id       INTEGER REFERENCES tax_lots(id),         -- lot relieved (NULL for dividends)
    sell_order_id TEXT REFERENCES executed_orders(brokerage_order_id),
    cost_basis   REAL,                         -- cost of the shares relieved
    proceeds     REAL,                         -- proceeds allocated to this lot
    amount       REAL,                         -- signed P&L (proceeds - cost) or dividend amount
    notes        TEXT
);

CREATE INDEX IF NOT EXISTS idx_realized_acct_sym
    ON realized_events (account_id, symbol);

-- ---------------------------------------------------------------------------
-- Convenience view
-- ---------------------------------------------------------------------------

-- Current holdings = rows from the most recent run for each account, so a
-- position that was fully sold correctly disappears from this view.
CREATE VIEW IF NOT EXISTS latest_holdings AS
SELECT h.*
FROM holding_snapshots h
JOIN (
    SELECT account_id, MAX(run_id) AS max_run
    FROM holding_snapshots
    GROUP BY account_id
) latest
  ON h.account_id = latest.account_id
 AND h.run_id     = latest.max_run;
