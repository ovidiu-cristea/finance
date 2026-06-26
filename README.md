# Financial — Fidelity holdings tracker

Tools for extracting Fidelity account data via [SnapTrade](https://snaptrade.com/)
and maintaining a local SQLite database of holdings, tax lots, orders and
realized gains.

## Overview

The core of the project is a local SQLite database (`holdings.db`) describing
every Fidelity position broken down by tax lot. Tax lots are **seeded manually**
from Fidelity's lot view (SnapTrade does not expose lot data) and thereafter
kept in sync with executed orders. Holdings are also pulled from SnapTrade to
verify the DB stays accurate.

```
Fidelity lot view ──paste──▶ reconcile_lots.py ──▶ holdings.db ◀──verify── reconcile_snaptrade.py ◀── SnapTrade
```

## Prerequisites

- Python 3.12+
- Packages: `pip install snaptrade-python-sdk openpyxl requests yfinance`
  (`yfinance` only for `build_durability_yf.py`, the foreign-ADR fallback)
- **`ConsumerKey.txt`** — SnapTrade consumer key for the personal client
  `PERS-97BRLMMWM55XNVEEORUA` (client id is hardcoded in the scripts; the
  consumer key lives in this file and is passed at runtime).
- **DB Browser for SQLite** — bundled under `DB-Browser/` for viewing the DB.

## Database

`schema.sql` defines the schema; build or upgrade it with `init_db.py`.

| Table | Purpose |
|---|---|
| `accounts` | One row per brokerage account (synced metadata + manual `account_type`/`owner`). |
| `securities` | Instrument reference (symbol, description, kind). |
| `sync_runs` / `holding_snapshots` | Append-only dated holdings history. `latest_holdings` view = most recent run per account. |
| `tax_lots` | Open lots with `remaining_quantity`; seeded manually, then maintained by the sync. |
| `planned_orders` / `planned_order_lots` | Orders recorded at placement time, with specific-lot intent for sells. |
| `executed_orders` | Ledger of orders ingested from SnapTrade (idempotent; carries `needs_review`). |
| `realized_events` | Lot closures (realized gains/losses), manual dividends, lending income. |
| `stock_metrics` | Per-stock price metrics (52wk high, drawdown, 200-day MA + slope, ATR%, days-below-MA) from Massive history; feeds the buy-side guardrails. |
| `durability` | Per-stock fundamentals gate (class ELIGIBLE/HOLD_ONLY/TERMINAL + quality score + raw financials) from Massive; the buy-side whitelist and terminal-risk exit. |
| `strategy_backtest` | Per-stock backtest of the strategy on price history (return vs buy-and-hold, drawdown, harvest frequency); the reference set candidates are compared against. |

> Note: sqlite3 has foreign keys **off** by default — every connection runs
> `PRAGMA foreign_keys = ON` (the scripts do this).

## Scripts

### Core holdings-DB workflow

#### `init_db.py` — create/upgrade the database
Applies `schema.sql`. Safe to re-run (everything is `IF NOT EXISTS`).
```
python init_db.py [db_path]          # default: holdings.db
```

#### `reconcile_lots.py` — seed/reconcile tax lots from a Fidelity paste
Paste Fidelity's per-position lot view into a text file, then load it. The
ticker is **not** in the paste, so pass it explicitly. The account is resolved
from the header line by its last 4 digits. Dry-run by default; `--apply` writes.
```
python reconcile_lots.py <TICKER> <file>            # dry run: show the diff
python reconcile_lots.py <TICKER> <file> --apply     # write the lots
python reconcile_lots.py U lots.txt --apply --account 3349
```

#### `set_targets.py` — set per-lot sell targets (`target_min_profit_pct`)
Walks open tax lots and sets each one's minimum-profit sell target to low (10%)
or high (50%), interactively. Optional `ticker` limits to one symbol (across all
accounts; rebrand-aware). `--only-missing` skips already-targeted lots.
`--keep-last-share` is a non-interactive batch command that clears the target on
any (account, ticker) down to a single open lot of exactly one share — the last
share you never want to sell. Each change commits immediately.
```
python set_targets.py [ticker] [--only-missing] [--db X]   # interactive
python set_targets.py FLNA
python set_targets.py [ticker] --keep-last-share            # batch, non-interactive
```

#### `reconcile_snaptrade.py` — verify positions + record lending income
Pulls current positions from SnapTrade and compares per-symbol share counts to
the open tax lots in the DB (read-only verification). Statuses: `OK`, `MISMATCH`,
`MISSING_IN_DB`, `MISSING_IN_SNAPTRADE`, plus benign `IGNORED` rows —
cash sweeps (`CASH`: SPAXX/FDRXX/…) and Fidelity bookkeeping placeholders such
as securities-lending collateral (`LENDING`, `kind="other"`). Symbols are
normalized through the rebrand registry (see `rebrands.py`).

It also records **fully-paid securities-lending interest** ("INTEREST FULLY
PAID") into `realized_events` as `lending_interest` income — idempotent, deduped
on SnapTrade's `external_reference_id`. Use `--no-lending` to skip, or
`--lending-since YYYY-MM-DD` to set the look-back (default 2024-01-01).
```
python reconcile_snaptrade.py <consumer-key-file> [--account X] [--db X]
python reconcile_snaptrade.py ConsumerKey.txt [--no-lending] [--lending-since 2024-01-01]
```

#### `ingest_orders.py` — apply executed BUY orders to the lot ledger
Reads recent executed orders from SnapTrade. **Buys** create a new lot
(target 10%), deduped by `buy_order_id`; with `--apply` each is confirmed
interactively (`y`/`n`), committed per order. **Sells** are listed only (not
applied here) so you can see which tickers to run `disambiguate_sells.py` on —
sell lot-tracking is done there from Fidelity's post-sale paste. Skips cash sweep
symbols.
```
python ingest_orders.py <consumer-key-file> [--account X] [--days N] [--apply]
python ingest_orders.py ConsumerKey.txt --account 9270 --apply
```

#### `disambiguate_sells.py` — resolve ambiguous sells with a Fidelity paste
For sells `ingest_orders` flags ambiguous (SnapTrade can't say which lot sold),
this compares the DB's open lots to Fidelity's current post-sale lot view (pasted
into a file, same format as `reconcile_lots`) to determine exactly which lots
were disposed — matching lots by acquisition date + per-share cost, handling full
and partial sales. It attributes the ticker's executed sells to those lots,
relieves them, and writes realized gains. Refuses to apply if the disposed total
(paste) doesn't equal the sold total (orders). Dry-run by default; `--apply` writes.
```
python disambiguate_sells.py <consumer-key-file> <TICKER> <paste-file> [--account X] [--days N] [--apply]
python disambiguate_sells.py ConsumerKey.txt QS qs.txt --apply
```

#### `build_metrics.py` — per-stock price metrics from Massive history
For each held ticker, pulls ~`--days` (420) of adjusted daily bars from Massive's
`/v2/aggs/.../range/1/day/...` endpoint and computes/stores into `stock_metrics`:
52-week high, drawdown from it, 200-day MA + slope, ATR%, and consecutive days
closed below the 200-day MA. Idempotent (upsert per symbol); run end-of-day.
Feeds the buy-side guardrails. `--delay` throttles API calls (default 12s;
full run ~12 min — lower if your plan allows). `--ticker` does one symbol.
```
python build_metrics.py <massive-api-key-file> [--ticker QS] [--days 420] [--delay 12] [--db X]
```

#### `build_durability.py` — fundamentals durability whitelist from Massive
For each held ticker, pulls company details (`market_cap`) and the last `--years`
(4) annual financial statements from Massive and classifies survival-ability into
the `durability` table: `ELIGIBLE` (buy/re-enter open), `HOLD_ONLY` (hold, don't
add, don't force-exit), or `TERMINAL` (a hard veto tripped -> stop buying +
recommend exit). Hard vetoes are absolute: micro-cap (< $300M), negative equity,
cash runway < 4 quarters (when burning), or a user-set `manual_flag`
(going-concern / Ch.11 / delisting). Otherwise a configurable quality score
(balance-sheet strength, burn/profitability trend, revenue trend, scale; 0-100,
ELIGIBLE >= 60) ranks the name. Deliberately contrarian — vetoes only genuine
terminal risk, never mere unprofitability. All thresholds/weights live in the
`CONFIG` block at the top of the script. `manual_flag` is read as a veto and
preserved across rebuilds (set it in DB Browser). ETFs/funds are skipped (and any
stale ETF row deleted) - they aren't businesses. Foreign ADRs Massive doesn't
cover show `no financials`; pass `--fallback-yf` to classify those gaps via
yfinance in a second pass after the Massive pass (Massive stays authoritative -
yfinance only fills names Massive couldn't; needs `pip install yfinance`).
Idempotent upsert; refresh quarterly. `--delay` throttles API calls (2 per ticker).
The scoring/veto logic + `CONFIG` live in `durability_core.py` (shared with the
yfinance fetcher); this script only does the Massive HTTP calls.
```
python build_durability.py <massive-api-key-file> [--ticker QS] [--years 4] [--delay 12] [--db X]
python build_durability.py ConsumerKeyMassive.txt --fallback-yf      # full refresh, one command
```

#### `build_durability_yf.py` — durability for foreign ADRs (yfinance fallback)
Massive only parses US-GAAP filings, so European ADRs (RACE, STLA, LVMUY, BUD,
DASTY, HGRAF, ...) come back `no financials` from `build_durability.py`. This
adapter pulls the same line items from Yahoo Finance and feeds the **same**
`durability_core.py` scoring + `durability` table, so a foreign name is
scored/vetoed identically to a US one. Run it standalone, or let
`build_durability.py --fallback-yf` invoke it automatically for the gaps.
Standalone it targets held tickers with no `durability` row yet (the leftovers
from a Massive run) and skips anything Yahoo flags as an ETF/fund. Statements are in the home
currency (shown under `cur`); every metric is a ratio so currency cancels, and
the only absolute threshold (cap floor) uses Yahoo's USD `marketCap`. Needs
`pip install yfinance`.
```
python build_durability_yf.py [--ticker RACE] [--years 4] [--delay 1] [--db X]
```

#### `durability_core.py` — shared durability scoring (module, not a script)
The vendor-agnostic scoring core: `CONFIG` (all veto thresholds + score weights),
`compute()` (vetoes + quality score + class), `upsert()`, and the shared output
formatting. Imported by both `build_durability.py` (Massive) and
`build_durability_yf.py` (yfinance) so a name is scored identically regardless of
source. Tune the whitelist here.

#### `recommend_orders.py` — end-of-day trade recommendations
Suggests, from the DB + an end-of-day price file, what to trade today. **Sells:**
trailing stop-loss per targeted lot — recommends a SELL STOP at `price × (1 −
3.5%)` once that clears the lot's profit target, ratcheting up daily (low-target
lots sell 90%, high-target sell all). **Buys** (two modes): **add** — a BUY when the price is ≥10% below the cheapest
*full* lot (low-target/10% lot); **re-enter** — for a name run up and trimmed
away (no full lot left), a BUY once it's ≥25% below its 52-week high. Both are
**sized** by the largest full lot's **share count** (ever, for re-entry) × a
drawdown factor (full to −20%, tapering to 0 at −50%), blocked by the 200-day-MA
breaker (below a *falling* MA for ≥~6 months) and an optional `--position-cap`.
**Durability gate:** names the `durability` table marks `HOLD_ONLY` or `TERMINAL`
are blocked from adds (shown as `durability=<class>`); `ELIGIBLE`, ETFs (class
`ETF`), and unrated names fall through to the price guardrails.
**Exits:** `TERMINAL` names held with shares surface an `EXIT` recommendation —
the terminal-risk downside exit (e.g. FLNA). `--ignore-durability` turns the gate
+ exits off to show raw price signals. Reads `stock_metrics` (populate with
`build_metrics.py`) and `durability`; fully-exited (zero-share) names are handled
manually. Price source: `--prices-csv` (the Massive VWAP file from
`extract_tickers.py`) or live SnapTrade. Read-only. `--all` shows every
lot/position with its status.
```
python recommend_orders.py --prices-csv stocksVWAP-YYYY-MM-DD.csv [--account X] [--all]
python recommend_orders.py <consumer-key-file> [--position-cap 20000] [--ignore-durability]
```

#### `backtest_strategy.py` — backtest the strategy across held names
For each held ticker, pulls ~`--years` (3) + 1y lookback of Massive daily bars and
replays the volatility-harvesting strategy day-by-day (`strategy_sim.py`), storing
the result in `strategy_backtest`: return on capital vs. buy-and-hold (`edge`), max
drawdown, risk-adjusted return, harvest frequency. This is both the **portfolio
backtest** (how the strategy did on what you own) and the **reference set** the
candidate screener compares against. Every buy/sell/size decision routes through
`strategy_core.py` (same code as the live recommender). `--delay` throttles API
calls (1 per ticker); `--ticker` does one.
```
python backtest_strategy.py <massive-api-key-file> [--ticker QS] [--years 3] [--delay 12]
```

#### `analyze_ticker.py` — screen a candidate ticker
Given one ticker: classifies its **durability** (Massive, yfinance fallback),
**backtests** the strategy on its price history, then drops its row into the
cached portfolio table and names its **closest behavioral analogs** ("behaves like
QS" vs "like INTC") by a z-scored fingerprint (volatility, drawdown, harvest
frequency, direction, edge). Run `backtest_strategy.py` first to populate the
reference set.
```
python analyze_ticker.py <massive-api-key-file> <TICKER> [--years 3]
```

#### `strategy_core.py` / `strategy_sim.py` — strategy math + simulator (modules)
`strategy_core.py` is the pure trigger/sizing logic (`add_size_factor`,
`evaluate_buy`, `evaluate_lot`, `sell_quantity` + the constants), shared by the
live `recommend_orders.py` and the backtest so they can't drift. `strategy_sim.py`
is the day-by-day simulator: `simulate(bars, window_start, params, initial_lots)`
replays average-down buys, trailing-stop sells, and re-entry over a price series
and returns the metric set. Seed a synthetic lot (screening) or real lots
(portfolio backtest).

#### `realized_gains.py` — report realized gains from sell orders
Sums `realized_events` (written by `ingest_orders` / `disambiguate_sells`) per
account and per ticker, with account subtotals and a grand total
(shares / cost basis / proceeds / gain / gain%). Excludes manual dividends.
Filters: `--account`, `--ticker` (rebrand-aware), `--year`. With
`--include-lending`, fully-paid securities-lending income is added as a
`(lending)` line per account.
```
python realized_gains.py [--account X] [--ticker X] [--year YYYY] [--include-lending] [--db X]
```

#### `inspect_position.py` — dump a raw SnapTrade position/instrument record
Prints the full JSON for positions whose symbol matches a substring, including
Fidelity's own `description`/`raw_symbol`/`kind`. The go-to for identifying odd
holdings (delisted tickers, internal placeholder ids, renamed symbols).
```
python inspect_position.py <consumer-key-file> <symbol-substring> [--account X]
python inspect_position.py ConsumerKey.txt SAVA --account 4749
```

#### `rebrands.py` — ticker rebrand registry (module, not a script)
Maps old tickers to current ones so reconciliation and order ingest treat a
renamed security as one position (SnapTrade often lags ticker changes). Add a
line to `REBRANDS` for each rename, e.g. `SAVA → FLNA` (Cassava Sciences →
Filana Therapeutics). Imported by `reconcile_snaptrade.py` and `ingest_orders.py`.

#### `tmp.py` — quick SnapTrade dump (accounts, positions, executed orders)
Prints each account with its balance, positions (with cost basis), and executed
orders from the last 10 days. Scratch/diagnostic tool.
```
python tmp.py <consumer-key-file>
python tmp.py ConsumerKey.txt
```

### Auxiliary / earlier tools

#### `create_portfolio.py` — generate an Excel portfolio workbook
Builds a styled Fidelity tracking workbook (`fidelity_portfolio_YYYYMMDD.xlsx`)
with a summary sheet plus one sheet per account, pre-filled with cost/value/G&L
formulas for up to 200 tax lots. Pre-database approach to the same tracking.
```
python create_portfolio.py
python create_portfolio.py "Individual - Taxable,Z12345678" "Rollover IRA,Y98765432"
```

#### `extract_tickers.py` — list holdings tickers and fetch VWAP
Reads the distinct tickers of open tax lots from the holdings database. With an
API key file, fetches previous-day VWAP/OHLCV per ticker from the Massive API
(rate-limited to 5/min) and writes `stocksVWAP-YYYY-MM-DD.csv` — the end-of-day
price file that `recommend_orders.py --prices-csv` consumes.
```
python extract_tickers.py <holdings.db>                 # just list tickers
python extract_tickers.py <holdings.db> <api_key_file>   # + fetch prices
```

## Typical workflow

1. `python init_db.py` — create `holdings.db` (once).
2. For each position: copy its lot view from Fidelity into a text file and run
   `python reconcile_lots.py <TICKER> <file> --apply`.
3. `python reconcile_snaptrade.py ConsumerKey.txt` — confirm the DB matches
   SnapTrade; fix any `MISMATCH` by re-pasting that position.
4. Browse/inspect with DB Browser for SQLite (`DB-Browser/DB Browser for SQLite.exe`),
   opening `holdings.db`.

## Secrets

`ConsumerKey.txt` (SnapTrade) and `massiveAPIKey` (Massive) are local secret
files — keep them out of version control.
