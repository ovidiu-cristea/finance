"""Set tax-lot sell targets: walk every open tax lot and set
target_min_profit_pct interactively to a low (10%) or high (50%) value.

Usage:
    python set_targets.py [ticker] [--db holdings.db] [--only-missing]

If a ticker is given, only that symbol's lots (across all accounts) are reviewed.

Per lot: (l)ow 10%  (h)igh 50%  (s)kip/Enter  (q)uit. Each choice is committed
immediately, so quitting (q or Ctrl+C) keeps everything already set.

--keep-last-share is a non-interactive batch command: for each (account, ticker)
down to a single open lot of exactly one share, it clears that lot's target
(the last share you never want to sell). Honors the optional ticker.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

from rebrands import canonical_symbol

HERE = Path(__file__).resolve().parent
LOW = 10.0
HIGH = 50.0
EPSILON = 1e-9


def fmt(v, dollars=False):
    if v is None:
        return "none"
    return f"${v:,.2f}" if dollars else f"{v:g}"


def keep_last_share(conn, ticker):
    """Clear the target on any (account, ticker) that is down to a single open
    lot holding exactly one share - the last share you never want to sell."""
    where = "t.status = 'open'"
    params = []
    if ticker:
        where += " AND t.symbol = ?"
        params.append(ticker)
    rows = conn.execute(
        f"""
        SELECT t.id, a.name, t.symbol, t.remaining_quantity, t.target_min_profit_pct
        FROM tax_lots t JOIN accounts a ON a.id = t.account_id
        WHERE {where}
        ORDER BY a.name, t.symbol
        """,
        params,
    ).fetchall()

    groups = {}
    for r in rows:
        groups.setdefault((r[1], r[2]), []).append(r)  # key (account name, symbol)

    changed = 0
    for (acct, symbol), lots in sorted(groups.items()):
        if len(lots) == 1 and abs(lots[0][3] - 1.0) < EPSILON:
            lot_id, _, _, _, target = lots[0]
            if target is None:
                continue  # already none
            conn.execute("UPDATE tax_lots SET target_min_profit_pct = NULL WHERE id = ?",
                         (lot_id,))
            changed += 1
            print(f"  {acct} | {symbol}: last single share -> target=none (was {target:g}%)")
    conn.commit()
    print(f"\nCleared target on {changed} last-single-share lot(s).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker", nargs="?",
                    help="If given, only this ticker's lots (across all accounts)")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    ap.add_argument("--only-missing", action="store_true",
                    help="Only prompt lots that have no target yet")
    ap.add_argument("--keep-last-share", action="store_true",
                    help="Non-interactive: clear target where an account is down to a "
                         "single lot of exactly one share")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    ticker = canonical_symbol(args.ticker.upper()) if args.ticker else None

    if args.keep_last_share:
        try:
            keep_last_share(conn, ticker)
        finally:
            conn.close()
        return

    where = "t.status = 'open'"
    params = []
    if ticker:
        where += " AND t.symbol = ?"
        params.append(ticker)
    if args.only_missing:
        where += " AND t.target_min_profit_pct IS NULL"
    lots = conn.execute(
        f"""
        SELECT t.id, a.name, t.symbol, t.open_date, t.remaining_quantity,
               t.cost_basis, t.target_min_profit_pct
        FROM tax_lots t JOIN accounts a ON a.id = t.account_id
        WHERE {where}
        ORDER BY a.name, t.symbol, t.open_date, t.id
        """,
        params,
    ).fetchall()

    total = len(lots)
    if not total:
        print("No matching lots.")
        return
    print(f"{total} lot(s) to review. low={LOW:g}%  high={HIGH:g}%\n")

    counts = {"low": 0, "high": 0, "skip": 0}
    try:
        for i, (lot_id, acct, symbol, date, qty, basis, cur) in enumerate(lots, 1):
            cur_str = f"{cur:g}%" if cur is not None else "none"
            print(f"[{i}/{total}] {acct} | {symbol}  {date}  "
                  f"qty={fmt(qty)}  basis={fmt(basis, True)}  current={cur_str}")
            while True:
                choice = input("  (l)ow 10%  (h)igh 50%  (s)kip  (q)uit > ").strip().lower()
                if choice in ("l", "h", "s", "q", ""):
                    break
                print("  ? enter l, h, s, or q")
            if choice == "q":
                print("Quit.")
                break
            if choice in ("s", ""):
                counts["skip"] += 1
                continue
            value = LOW if choice == "l" else HIGH
            conn.execute(
                "UPDATE tax_lots SET target_min_profit_pct = ? WHERE id = ?",
                (value, lot_id),
            )
            conn.commit()
            counts["low" if choice == "l" else "high"] += 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        conn.close()

    print(f"\nDone. low={counts['low']}  high={counts['high']}  skipped={counts['skip']}")


if __name__ == "__main__":
    main()
