"""Reconcile the holdings DB against live SnapTrade positions.

Pulls current positions from SnapTrade for each account and compares the
per-symbol share counts to the open tax lots recorded in the database.

Read-only: it reports discrepancies, it does not write to the DB. Fix any
mismatches by re-pasting the Fidelity lot view through reconcile_lots.py.

Usage:
    python reconcile_snaptrade.py <consumer-key-file> [--db holdings.db] [--account X]
"""
import argparse
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


def main():
    ap = argparse.ArgumentParser(description="Reconcile holdings DB vs SnapTrade positions.")
    ap.add_argument("key_file", help="File containing the SnapTrade consumer key")
    ap.add_argument("--db", default=str(HERE / "holdings.db"))
    ap.add_argument("--account", help="Filter by account id or last-4 digits")
    args = ap.parse_args()

    consumer_key = read_secret(args.key_file)
    snaptrade = SnapTrade(client_id=CLIENT_ID, consumer_key=consumer_key)

    conn = sqlite3.connect(args.db)

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
    try:
        for acct in accounts:
            name = acct.get("name") or acct.get("institution_name") or "(unnamed)"
            counts = reconcile_account(conn, snaptrade, consumer_key, acct["id"], name)
            for k in totals:
                totals[k] += counts[k]
    finally:
        conn.close()

    print(f"\nTOTAL  OK={totals['OK']}  MISMATCH={totals['MISMATCH']}  "
          f"MISSING_IN_DB={totals['MISSING_IN_DB']}  "
          f"MISSING_IN_SNAPTRADE={totals['MISSING_IN_SNAPTRADE']}  "
          f"IGNORED={totals['IGNORED']}")
    if totals["MISMATCH"] or totals["MISSING_IN_SNAPTRADE"]:
        print("Review MISMATCH / MISSING_IN_SNAPTRADE rows; re-seed via reconcile_lots.py.")


if __name__ == "__main__":
    main()
