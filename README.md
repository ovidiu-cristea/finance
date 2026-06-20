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
- Packages: `pip install snaptrade-python-sdk openpyxl requests`
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
| `realized_events` | Lot closures (realized gains/losses) and manual dividends. |

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

#### `reconcile_snaptrade.py` — verify the DB against live SnapTrade positions
Pulls current positions from SnapTrade and compares per-symbol share counts to
the open tax lots in the DB. Read-only. Statuses: `OK`, `MISMATCH`,
`MISSING_IN_DB`, `MISSING_IN_SNAPTRADE`, plus benign `IGNORED` rows —
cash sweeps (`CASH`: SPAXX/FDRXX/…) and Fidelity bookkeeping placeholders such
as securities-lending collateral (`LENDING`, `kind="other"`). Symbols are
normalized through the rebrand registry (see `rebrands.py`).
```
python reconcile_snaptrade.py <consumer-key-file> [--account X] [--db X]
python reconcile_snaptrade.py ConsumerKey.txt
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

#### `realized_gains.py` — report realized gains from sell orders
Sums `realized_events` (written by `ingest_orders` / `disambiguate_sells`) per
account and per ticker, with account subtotals and a grand total
(shares / cost basis / proceeds / gain / gain%). Excludes manual dividends.
Filters: `--account`, `--ticker` (rebrand-aware), `--year`.
```
python realized_gains.py [--account X] [--ticker X] [--year YYYY] [--db X]
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
