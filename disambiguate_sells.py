"""Disambiguate SELL orders for one ticker using a pasted Fidelity lot view.

SnapTrade can't tell us WHICH tax lots a sale disposed. This script figures it
out by comparing the DB's open lots (pre-sale, what we think we hold) against
Fidelity's CURRENT post-sale lot view (pasted into a file, same format as
reconcile_lots.py). Lots that shrank or vanished in Fidelity are the disposed
shares; we attribute the ticker's executed SELL orders to them, relieve the
lots, and write realized_events.

Lots are matched between the DB and the paste by acquisition DATE (which never
changes); per-share cost only disambiguates multiple lots sharing a date, and is
matched with a small tolerance because Fidelity re-rounds the cost basis of a
lot's remaining shares after a partial sale (so per-share can drift a cent).

Usage:
    python disambiguate_sells.py <consumer-key-file> <TICKER> <paste-file> [--db X] [--account X] [--days N] [--apply]

Account comes from the paste header (last-4 digits). Dry-run by default.
"""
import argparse
import datetime
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from snaptrade_client import SnapTrade

from rebrands import canonical_symbol
from reconcile_lots import parse_file, resolve_account
from ingest_orders import (CLIENT_ID, read_secret, to_float, order_symbol,
                           is_sell, fetch_executed_orders, ensure_security,
                           record_order, open_lots)

HERE = Path(__file__).resolve().parent
EPSILON = 1e-6
PS_TOL = 0.05  # per-share match tolerance (absorbs Fidelity's cent re-rounding)

# After a partial sale (the 90%-of-a-low-target-lot strategy), the kept remainder
# is switched from the low target to the high target.
LOW_TARGET_PCT = 10.0
HIGH_TARGET_PCT = 50.0


def lot_per_share(lot):
    return (lot[4] / lot[2]) if lot[2] else 0.0  # cost_basis / original_quantity


def reconcile_disposed(db_lots, fidelity_lots, tol=PS_TOL):
    """Match each Fidelity remaining lot to its DB lot by acquisition date (using
    per-share cost only to tell apart multiple same-date lots, with `tol` to
    absorb the cent drift from Fidelity re-rounding a partly-sold lot).

    Returns (disposed, extra): disposed = {lot_id: shares disposed}; extra =
    [(date, per_share, qty)] for Fidelity lots with no DB match (un-ingested buys)."""
    db_by_date = defaultdict(list)
    for lot in db_lots:                       # lot = (id, date, orig, remaining, basis)
        db_by_date[lot[1]].append([lot, 0.0])  # [lot, matched_fidelity_qty]
    fid_count = Counter(fl[0] for fl in fidelity_lots)

    extra = []
    for open_date, qty, _avg_cost, cost_basis in fidelity_lots:
        fps = (cost_basis / qty) if qty else 0.0
        cands = db_by_date.get(open_date, [])
        match = None
        if len(cands) == 1 and fid_count[open_date] == 1:
            match = cands[0]                  # one lot per side on the date: unambiguous
        elif cands:
            best, best_diff = None, None
            for entry in cands:
                diff = abs(lot_per_share(entry[0]) - fps)
                if best is None or diff < best_diff:
                    best, best_diff = entry, diff
            if best_diff is not None and best_diff <= tol:
                match = best
        if match is not None:
            match[1] += qty
        else:
            extra.append((open_date, round(fps, 2), qty))

    disposed = {}
    for entries in db_by_date.values():
        for lot, matched in entries:
            gone = lot[3] - matched
            if gone > EPSILON:
                disposed[lot[0]] = gone
    return disposed, extra


def main():
    ap = argparse.ArgumentParser(description="Disambiguate sells for one ticker via a Fidelity paste.")
    ap.add_argument("key_file", help="SnapTrade consumer-key file")
    ap.add_argument("ticker", help="Ticker to process, e.g. QS")
    ap.add_argument("file", help="Text file with the pasted Fidelity lot view for this ticker")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    ap.add_argument("--account", help="Account id or number, if the paste header can't be parsed")
    ap.add_argument("--days", type=int, default=30, help="Look-back window for sells (max 90)")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = ap.parse_args()

    ticker = canonical_symbol(args.ticker.upper())
    parsed = parse_file(Path(args.file).read_text(encoding="utf-8-sig"))
    account_id = resolve_account(args.account, parsed["account_number"])

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON;")

    db_lots = open_lots(conn, account_id, ticker)  # (id, date, orig, remaining, cost_basis)
    lots_by_id = {l[0]: l for l in db_lots}

    disposed, extra = reconcile_disposed(db_lots, parsed["lots"])
    total_disposed = sum(disposed.values())

    # The ticker's executed SELL orders.
    consumer_key = read_secret(args.key_file)
    snaptrade = SnapTrade(client_id=CLIENT_ID, consumer_key=consumer_key)
    orders = fetch_executed_orders(snaptrade, consumer_key, account_id, args.days)
    sells = [o for o in orders if is_sell(o.get("action")) and order_symbol(o) == ticker]
    total_sold = sum(to_float(o.get("filled_quantity")) or 0 for o in sells)
    proceeds_total = sum((to_float(o.get("filled_quantity")) or 0) *
                         (to_float(o.get("execution_price")) or 0) for o in sells)
    avg_price = proceeds_total / total_sold if total_sold else 0.0

    print(f"Account : {account_id}")
    print(f"Ticker  : {ticker}  ({parsed['security_desc']})")
    print(f"DB open : {sum(l[3] for l in db_lots):g} sh in {len(db_lots)} lots")
    print(f"Fidelity: {sum(fl[1] for fl in parsed['lots']):g} sh")
    print(f"Sells   : {len(sells)} order(s), {total_sold:g} sh, "
          f"${proceeds_total:,.2f} proceeds (avg ${avg_price:,.4f})")

    print("\nDisposed lots (DB minus Fidelity):")
    if not disposed:
        print("  (none - DB already matches Fidelity)")
    for lot_id in sorted(disposed, key=lambda i: (lots_by_id[i][1], i)):
        lot = lots_by_id[lot_id]
        print(f"  {lot[1]}  @${lot_per_share(lot):.2f}/sh  dispose {disposed[lot_id]:g} of "
              f"{lot[3]:g} sh  (lot id {lot_id})")

    # Fidelity lots with no DB match are likely un-ingested buys; surface them.
    if extra:
        print("\nNote: Fidelity has shares the DB lacks (un-ingested buys?):")
        for open_date, ps, qty in sorted(extra):
            print(f"  {open_date}  @${ps:.2f}/sh  +{qty:g} sh")

    if abs(total_disposed - total_sold) > 0.01:
        print(f"\nMISMATCH: disposed {total_disposed:g} != sold {total_sold:g}. "
              "Not applying - check the paste, the look-back window, or un-ingested buys/sells.")
        conn.close()
        return
    if total_disposed <= EPSILON:
        print("\nNothing to disambiguate.")
        conn.close()
        return

    if not args.apply:
        print("\nDry run - re-run with --apply to relieve lots and record realized gains.")
        conn.close()
        return

    try:
        if input("\napply this disambiguation? [y/N] ").strip().lower() != "y":
            print("SKIPPED")
            conn.close()
            return
    except EOFError:
        print("SKIPPED")
        conn.close()
        return

    apply_disambiguation(conn, account_id, ticker, parsed, lots_by_id, disposed, sells, avg_price)
    conn.close()


def apply_disambiguation(conn, account_id, ticker, parsed, lots_by_id, disposed, sells, avg_price):
    cur = conn.execute(
        "INSERT INTO sync_runs (run_at, source) VALUES (?, 'sell-disambiguation')",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),),
    )
    run_id = cur.lastrowid
    ensure_security(conn, ticker, parsed.get("security_desc"))

    existing = {r[0] for r in conn.execute("SELECT brokerage_order_id FROM executed_orders")}
    for o in sells:
        record_order(conn, o, account_id, run_id, applied=1, needs_review=0,
                     note="sell - disambiguated via Fidelity lot paste",
                     already=o.get("brokerage_order_id") in existing)

    # One order id to link realized events to (only unambiguous when there's one sell).
    link_oid = sells[0].get("brokerage_order_id") if len(sells) == 1 else None
    multi_note = None if link_oid else "multiple sells: " + ",".join(
        o.get("brokerage_order_id") for o in sells)

    event_date = max((str(o.get("time_executed") or "")[:10] for o in sells), default="")
    total_realized = 0.0
    relieved = 0
    bumped = 0
    for lot_id, take in disposed.items():
        _id, _date, orig, rem, basis = lots_by_id[lot_id]
        per_share_cost = (basis / orig) if orig else 0.0
        cost = per_share_cost * take
        proceeds = take * avg_price
        realized = proceeds - cost
        total_realized += realized
        relieved += 1
        conn.execute(
            """
            INSERT INTO realized_events
                (account_id, symbol, event_date, event_type, quantity,
                 lot_id, sell_order_id, cost_basis, proceeds, amount, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, ticker, event_date,
             "realized_gain" if realized >= 0 else "realized_loss",
             take, lot_id, link_oid, cost, proceeds, realized, multi_note),
        )
        new_rem = rem - take
        conn.execute(
            "UPDATE tax_lots SET remaining_quantity = ?, status = ? WHERE id = ?",
            (new_rem, "closed" if new_rem <= EPSILON else "open", lot_id),
        )
        # Partial sale: bump the kept remainder of a low-target lot to the high target.
        if new_rem > EPSILON:
            bumped += conn.execute(
                "UPDATE tax_lots SET target_min_profit_pct = ? "
                "WHERE id = ? AND ABS(target_min_profit_pct - ?) < ?",
                (HIGH_TARGET_PCT, lot_id, LOW_TARGET_PCT, EPSILON),
            ).rowcount
    conn.commit()
    extra = f", bumped {bumped} remainder(s) to {HIGH_TARGET_PCT:g}% target" if bumped else ""
    print(f"\nApplied: relieved {relieved} lot(s), realized ${total_realized:,.2f}{extra}.")


if __name__ == "__main__":
    main()
