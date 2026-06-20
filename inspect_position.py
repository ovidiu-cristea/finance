"""Dump the full SnapTrade position/instrument record(s) matching a symbol.

Useful for identifying odd holdings (delisted tickers, internal placeholder
ids, residual lines) where the displayed "symbol" is uninformative. Prints
Fidelity's own description / raw_symbol / kind as SnapTrade reports them.

Usage:
    python inspect_position.py <consumer-key-file> <symbol-substring> [--account X]
    python inspect_position.py ConsumerKey.txt L0C990030 --account 9270
"""
import argparse
import json
import sys
from pathlib import Path

from snaptrade_client import SnapTrade

HERE = Path(__file__).resolve().parent
CLIENT_ID = "PERS-97BRLMMWM55XNVEEORUA"


def read_secret(path):
    secret = Path(path).read_text(encoding="utf-8-sig").strip()
    if not secret:
        sys.exit(f"Error: {path} is empty")
    return secret


def position_symbol(pos):
    instrument = pos.get("instrument") or {}
    if instrument:
        return instrument.get("symbol") or instrument.get("raw_symbol") or "?"
    sym = pos.get("symbol") or {}
    inner = sym.get("symbol") if isinstance(sym, dict) else None
    return (inner.get("symbol") if isinstance(inner, dict) else inner) or "?"


def main():
    ap = argparse.ArgumentParser(description="Inspect SnapTrade positions by symbol.")
    ap.add_argument("key_file")
    ap.add_argument("needle", help="Symbol or substring to match (case-insensitive)")
    ap.add_argument("--account", help="Filter by account id or last-4 digits")
    args = ap.parse_args()

    consumer_key = read_secret(args.key_file)
    snaptrade = SnapTrade(client_id=CLIENT_ID, consumer_key=consumer_key)
    needle = args.needle.upper()

    accounts = snaptrade.account_information.list_user_accounts(
        user_id=CLIENT_ID, user_secret=consumer_key,
    ).body
    if args.account:
        accounts = [a for a in accounts
                    if a["id"] == args.account
                    or str(a.get("number", "")).endswith(args.account)]

    found = 0
    for acct in accounts:
        body = snaptrade.account_information.get_all_account_positions(
            user_id=CLIENT_ID, user_secret=consumer_key, account_id=acct["id"],
        ).body
        for pos in body.get("results") or []:
            if needle in position_symbol(pos).upper():
                found += 1
                name = acct.get("name") or acct.get("institution_name") or "(unnamed)"
                print(f"\n=== {name}  [{acct['id'][:8]}] ===")
                print(json.dumps(pos, indent=2, default=str))

    if not found:
        print(f"No positions matched {args.needle!r}.")


if __name__ == "__main__":
    main()
