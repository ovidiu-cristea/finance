"""Reconcile the holdings DB against live SnapTrade positions, and record
securities-lending income.

Pulls current positions from SnapTrade for each account and compares the
per-symbol share counts to the open tax lots recorded in the database (read-only
verification - fix mismatches via reconcile_lots.py).

It also pulls the fully-paid securities-lending interest ("INTEREST FULLY PAID")
and records it into realized_events as `lending_interest` income, idempotently
deduped on SnapTrade's external_reference_id. Use --no-lending to skip.

Usage:
    python reconcile_snaptrade.py <consumer-key-file> [--db holdings.db] [--account X]
                                  [--no-lending] [--lending-since YYYY-MM-DD]
"""
import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

from snaptrade_client import SnapTrade

from rebrands import canonical_symbol

HERE = Path(__file__).resolve().parent
CLIENT_ID = "PERS-97BRLMMWM55XNVEEORUA"

# Fidelity core/sweep money-market symbols that represent cash, not equity.
# These are expected to be present in SnapTrade but absent from the DB.
CASH_SYMBOLS = {"SPAXX", "FDRXX", "FZFXX", "FDIC"}
EPSILON = 1e-6


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_secret(path):
    secret = Path(path).read_text(encoding="utf-8-sig").strip()
    if not secret:
        sys.exit(f"Error: {path} is empty")
    return secret


def position_symbol(pos):
    """Human-readable ticker for a SnapTrade position (v2 or legacy shape)."""
    instrument = pos.get("instrument") or {}
    if instrument:
        return instrument.get("symbol") or instrument.get("raw_symbol") or "?"
    sym = pos.get("symbol") or {}
    inner = sym.get("symbol") if isinstance(sym, dict) else None
    if isinstance(inner, dict):
        return inner.get("symbol") or inner.get("raw_symbol") or "?"
    return inner or "?"


def snaptrade_positions(snaptrade, consumer_key, account_id):
    """Return {symbol: {units, price, value}} for one account."""
    body = snaptrade.account_information.get_all_account_positions(
        user_id=CLIENT_ID, user_secret=consumer_key, account_id=account_id,
    ).body
    out = {}
    for pos in body.get("results") or []:
        symbol = canonical_symbol(position_symbol(pos))
        units = to_float(pos.get("units")) or 0.0
        price = to_float(pos.get("price"))
        kind = (pos.get("instrument") or {}).get("kind")
        entry = out.setdefault(symbol, {"units": 0.0, "price": price, "value": 0.0, "kind": kind})
        entry["units"] += units
        if price is not None:
            entry["value"] += units * price
    return out, body.get("data_freshness")


def db_positions(conn, account_id):
    """Return {symbol: shares} from open tax lots for one account."""
    rows = conn.execute(
        """
        SELECT symbol, COALESCE(SUM(remaining_quantity), 0) AS qty
        FROM tax_lots
        WHERE account_id = ? AND status = 'open'
        GROUP BY symbol
        """,
        (account_id,),
    ).fetchall()
    out = {}
    for sym, qty in rows:
        if abs(qty) > EPSILON:
            c = canonical_symbol(sym)
            out[c] = out.get(c, 0.0) + qty
    return out


def classify(db_qty, st_units):
    if db_qty is None:
        return "MISSING_IN_DB"
    if st_units is None:
        return "MISSING_IN_SNAPTRADE"
    if abs(st_units - db_qty) <= EPSILON:
        return "OK"
    return "MISMATCH"


def reconcile_account(conn, snaptrade, consumer_key, account_id, account_name):
    st_pos, freshness = snaptrade_positions(snaptrade, consumer_key, account_id)
    db_pos = db_positions(conn, account_id)

    symbols = sorted(set(st_pos) | set(db_pos))
    fresh = f"  (data: {freshness})" if freshness else ""
    print(f"\n{account_name}  [{account_id[:8]}]{fresh}")
    print(f"  {'symbol':<10} {'db sh':>12} {'snaptrade':>12} {'diff':>12} {'value':>12}  status")

    counts = {"OK": 0, "MISMATCH": 0, "MISSING_IN_DB": 0, "MISSING_IN_SNAPTRADE": 0, "IGNORED": 0}
    for symbol in symbols:
        db_qty = db_pos.get(symbol)
        st = st_pos.get(symbol)
        st_units = st["units"] if st else None
        status = classify(db_qty, st_units)

        # Benign SnapTrade-only lines that are not tax lots: cash sweeps and
        # Fidelity bookkeeping placeholders (e.g. securities-lending collateral,
        # which come through as kind="other").
        label = status
        if status == "MISSING_IN_DB" and symbol in CASH_SYMBOLS:
            label, status = "CASH", "IGNORED"
        elif status == "MISSING_IN_DB" and st and st.get("kind") == "other":
            label, status = "LENDING", "IGNORED"
        counts[status] += 1

        diff = (st_units - db_qty) if (db_qty is not None and st_units is not None) else None
        print(
            f"  {symbol:<10} "
            f"{(db_qty if db_qty is not None else 0):>12g} "
            f"{(st_units if st_units is not None else 0):>12g} "
            f"{(diff if diff is not None else 0):>12g} "
            f"{(st['value'] if st else 0):>12,.2f}  {label}"
        )
    return counts


def fetch_lending_interest(snaptrade, consumer_key, account_id, start, end, limit=1000):
    """Return the fully-paid securities-lending interest activities for one account."""
    out = []
    offset = 0
    for _ in range(50):  # safety cap
        body = snaptrade.account_information.get_account_activities(
            account_id=account_id, user_id=CLIENT_ID, user_secret=consumer_key,
            start_date=start, end_date=end, offset=offset, limit=limit, type="INTEREST",
        ).body
        page = body.get("data") if isinstance(body, dict) else (list(body) if body else [])
        page = page or []
        out.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return [a for a in out if "FULLY PAID" in (a.get("description") or "").upper()]


def record_lending_income(conn, snaptrade, consumer_key, account_id, name, start, end, existing_refs):
    """Insert new lending-interest events into realized_events; return (new, total$)."""
    acts = fetch_lending_interest(snaptrade, consumer_key, account_id, start, end)
    new = 0
    new_amt = 0.0
    period_total = 0.0
    for a in acts:
        amt = to_float(a.get("amount")) or 0.0
        period_total += amt
        ref = a.get("external_reference_id")
        if ref and ref in existing_refs:
            continue
        event_date = str(a.get("trade_date") or a.get("settlement_date") or "")[:10] or None
        conn.execute(
            """
            INSERT INTO realized_events
                (account_id, symbol, event_date, event_type, amount, notes, external_ref)
            VALUES (?, NULL, ?, 'lending_interest', ?, ?, ?)
            """,
            (account_id, event_date, amt, a.get("description"), ref),
        )
        if ref:
            existing_refs.add(ref)
        new += 1
        new_amt += amt
    if acts:
        print(f"  {name:24} {len(acts):>3} events  ${period_total:>10,.2f} total   "
              f"({new} new, ${new_amt:,.2f})")
    return new, new_amt


def main():
    ap = argparse.ArgumentParser(description="Reconcile holdings DB vs SnapTrade positions.")
    ap.add_argument("key_file", help="File containing the SnapTrade consumer key")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    ap.add_argument("--account", help="Filter by account id or last-4 digits")
    ap.add_argument("--no-lending", action="store_true",
                    help="Skip recording securities-lending interest income")
    ap.add_argument("--lending-since", default="2024-01-01",
                    help="Earliest date to pull lending interest from (YYYY-MM-DD)")
    args = ap.parse_args()

    consumer_key = read_secret(args.key_file)
    snaptrade = SnapTrade(client_id=CLIENT_ID, consumer_key=consumer_key)

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON;")

    accounts = snaptrade.account_information.list_user_accounts(
        user_id=CLIENT_ID, user_secret=consumer_key,
    ).body
    if args.account:
        accounts = [a for a in accounts
                    if a["id"] == args.account
                    or str(a.get("number", "")).endswith(args.account)]
        if not accounts:
            sys.exit(f"No SnapTrade account matched {args.account!r}")

    totals = {"OK": 0, "MISMATCH": 0, "MISSING_IN_DB": 0, "MISSING_IN_SNAPTRADE": 0, "IGNORED": 0}
    lend_new, lend_amt = 0, 0.0
    try:
        for acct in accounts:
            name = acct.get("name") or acct.get("institution_name") or "(unnamed)"
            counts = reconcile_account(conn, snaptrade, consumer_key, acct["id"], name)
            for k in totals:
                totals[k] += counts[k]

        if not args.no_lending:
            start = datetime.date.fromisoformat(args.lending_since)
            today = datetime.date.today()
            existing_refs = {r[0] for r in conn.execute(
                "SELECT external_ref FROM realized_events WHERE external_ref IS NOT NULL")}
            print("\n=== securities-lending income (INTEREST FULLY PAID) ===")
            for acct in accounts:
                name = acct.get("name") or acct.get("institution_name") or "(unnamed)"
                n, amt = record_lending_income(conn, snaptrade, consumer_key, acct["id"],
                                               name, start, today, existing_refs)
                lend_new += n
                lend_amt += amt
            conn.commit()
    finally:
        conn.close()

    print(f"\nTOTAL  OK={totals['OK']}  MISMATCH={totals['MISMATCH']}  "
          f"MISSING_IN_DB={totals['MISSING_IN_DB']}  "
          f"MISSING_IN_SNAPTRADE={totals['MISSING_IN_SNAPTRADE']}  "
          f"IGNORED={totals['IGNORED']}")
    if not args.no_lending:
        print(f"Lending income: recorded {lend_new} new event(s), ${lend_amt:,.2f}.")
    if totals["MISMATCH"] or totals["MISSING_IN_SNAPTRADE"]:
        print("Review MISMATCH / MISSING_IN_SNAPTRADE rows; re-seed via reconcile_lots.py.")


if __name__ == "__main__":
    main()
