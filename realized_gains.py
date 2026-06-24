"""Display realized gains recorded from sell orders.

Sums the realized_events written by the order pipeline (ingest_orders /
disambiguate_sells) per account and per ticker, with account subtotals and a
grand total. Manual dividends are excluded. With --include-lending, fully-paid
securities-lending income (recorded by reconcile_snaptrade) is added as a
"(lending)" line per account. Read-only.

Usage:
    python realized_gains.py [--db X] [--account X] [--ticker X] [--year YYYY] [--include-lending]
"""
import argparse
import sqlite3
from pathlib import Path

from rebrands import canonical_symbol

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description="Show realized gains from sell orders.")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    ap.add_argument("--account", help="Filter by account id or last-4 digits")
    ap.add_argument("--ticker", help="Filter to one ticker (rebrand-aware)")
    ap.add_argument("--year", help="Filter to a calendar year, e.g. 2026")
    ap.add_argument("--include-lending", action="store_true",
                    help="Also include securities-lending income (lending_interest events)")
    args = ap.parse_args()

    event_types = ["realized_gain", "realized_loss"]
    if args.include_lending:
        event_types.append("lending_interest")
    where = [f"r.event_type IN ({','.join('?' for _ in event_types)})"]
    params = list(event_types)
    if args.account:
        where.append("(a.id = ? OR a.number LIKE ?)")
        params += [args.account, f"%{args.account}"]
    if args.ticker:
        where.append("r.symbol = ?")
        params.append(canonical_symbol(args.ticker.upper()))
    if args.year:
        where.append("substr(r.event_date, 1, 4) = ?")
        params.append(args.year)

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        f"""
        SELECT a.name, r.symbol,
               COALESCE(SUM(r.quantity), 0)   AS shares,
               COALESCE(SUM(r.cost_basis), 0) AS cost,
               COALESCE(SUM(r.proceeds), 0)   AS proceeds,
               COALESCE(SUM(r.amount), 0)     AS gain,
               COUNT(*)                       AS events
        FROM realized_events r
        JOIN accounts a ON a.id = r.account_id
        WHERE {' AND '.join(where)}
        GROUP BY a.name, r.symbol
        ORDER BY a.name, r.symbol
        """,
        params,
    ).fetchall()
    conn.close()

    if not rows:
        print("No realized gains found.")
        return

    def pct(gain, cost):
        return f"{gain / cost * 100:+.2f}%" if cost else "-"

    def header():
        print(f"  {'ticker':<8} {'shares':>9} {'cost basis':>14} "
              f"{'proceeds':>14} {'gain':>14} {'gain%':>9}")

    def line(label, shares, cost, proceeds, gain):
        print(f"  {label:<8} {shares:>9g} {cost:>14,.2f} "
              f"{proceeds:>14,.2f} {gain:>+14,.2f} {pct(gain, cost):>9}")

    g_cost = g_proceeds = g_gain = 0.0
    acct = None
    a_shares = a_cost = a_proceeds = a_gain = 0.0

    def flush_account():
        if acct is not None:
            print("  " + "-" * 62)
            line("subtotal", a_shares, a_cost, a_proceeds, a_gain)

    for name, symbol, shares, cost, proceeds, gain, _events in rows:
        if name != acct:
            flush_account()
            acct = name
            a_shares = a_cost = a_proceeds = a_gain = 0.0
            print(f"\n{name}")
            header()
        line(symbol if symbol is not None else "(lending)", shares, cost, proceeds, gain)
        a_shares += shares; a_cost += cost; a_proceeds += proceeds; a_gain += gain
        g_cost += cost; g_proceeds += proceeds; g_gain += gain
    flush_account()

    print(f"\nTOTAL  cost ${g_cost:,.2f}  proceeds ${g_proceeds:,.2f}  "
          f"realized ${g_gain:+,.2f}  ({pct(g_gain, g_cost)})")


if __name__ == "__main__":
    main()
