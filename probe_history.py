"""Probe how far back SnapTrade actually has transaction history.

Calls the /activities endpoint (arbitrary start_date..end_date, no 90-day cap)
and reports, per account, the EARLIEST activity date actually returned plus a
type breakdown. SnapTrade can't return more than the brokerage gave it, so this
tells you the real floor for your Fidelity connection. Read-only.

Usage:
    python probe_history.py <consumer-key-file> [--start 2020-11-01] [--ticker QS]
"""
import argparse
import datetime
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from snaptrade_client import SnapTrade

HERE = Path(__file__).resolve().parent
CLIENT_ID = "PERS-97BRLMMWM55XNVEEORUA"


def read_secret(path):
    secret = Path(path).read_text(encoding="utf-8-sig").strip()
    if not secret:
        sys.exit(f"Error: {path} is empty")
    return secret


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def act_date(a):
    for k in ("trade_date", "settlement_date", "date"):
        v = a.get(k)
        if v:
            return str(v)[:10]
    return None


def act_symbol(a):
    s = a.get("symbol")
    if isinstance(s, dict):
        return s.get("symbol") or s.get("raw_symbol")
    if isinstance(s, str):
        return s
    u = a.get("universal_symbol") or {}
    return u.get("symbol")


def fetch_all_activities(snaptrade, consumer_key, account_id, start, end, limit=1000):
    """Page through the account-scoped /activities endpoint and return all rows."""
    out = []
    offset = 0
    for _ in range(200):  # safety cap
        body = snaptrade.account_information.get_account_activities(
            account_id=account_id, user_id=CLIENT_ID, user_secret=consumer_key,
            start_date=start, end_date=end, offset=offset, limit=limit,
        ).body
        page = body.get("data") if isinstance(body, dict) else (list(body) if body else [])
        page = page or []
        out.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return out


def main():
    ap = argparse.ArgumentParser(description="Probe SnapTrade transaction-history depth.")
    ap.add_argument("key_file")
    ap.add_argument("--start", default="2020-11-01", help="Earliest date to request (YYYY-MM-DD)")
    ap.add_argument("--ticker", help="Also list activities for this ticker")
    ap.add_argument("--type", dest="atype",
                    help="Dump sample rows of this activity type (e.g. LOAN, INTEREST)")
    args = ap.parse_args()

    consumer_key = read_secret(args.key_file)
    snaptrade = SnapTrade(client_id=CLIENT_ID, consumer_key=consumer_key)

    accounts = snaptrade.account_information.list_user_accounts(
        user_id=CLIENT_ID, user_secret=consumer_key,
    ).body

    start = datetime.date.fromisoformat(args.start)
    today = datetime.date.today()
    print(f"Requesting activities {start} -> {today} per account...\n")

    schema_shown = False
    ticker_hits = []
    type_hits = []
    for acct in accounts:
        name = acct.get("name") or acct.get("institution_name") or "?"
        rows = fetch_all_activities(snaptrade, consumer_key, acct["id"], start, today)
        if rows and not schema_shown:
            print("=== raw fields of first activity (to learn the schema) ===")
            print(json.dumps(rows[0], indent=2, default=str)[:1500])
            print()
            schema_shown = True

        dates = sorted(d for d in (act_date(a) for a in rows) if d)
        types = Counter(a.get("type") for a in rows)
        span = f"{dates[0]} -> {dates[-1]}" if dates else "no dated activities"
        print(f"{name} [{acct['id'][:8]}]: {len(rows)} activities, {span}")
        print(f"  types: {dict(types)}")
        # net cash (amount) by type: income types net positive, collateral nets ~0
        net = defaultdict(float)
        for a in rows:
            net[a.get("type")] += (to_float(a.get("amount")) or 0.0)
        print("  net $ by type: " + "  ".join(f"{t}={v:+,.0f}" for t, v in sorted(net.items())))

        if args.ticker:
            t = args.ticker.upper()
            ticker_hits += [a for a in rows if (act_symbol(a) or "").upper() == t]
        if args.atype:
            type_hits += [a for a in rows if (a.get("type") or "").upper() == args.atype.upper()]

    if args.atype:
        at = args.atype.upper()
        print(f"\n=== {at}: {len(type_hits)} rows (sample) ===")
        for a in sorted(type_hits, key=lambda a: act_date(a) or "")[:25]:
            print(f"  {act_date(a)}  {(act_symbol(a) or '-'):<8} amount={a.get('amount')} "
                  f"units={a.get('units')} | {a.get('description')}")

    if args.ticker:
        print(f"\n=== {args.ticker.upper()}: {len(ticker_hits)} activities ===")
        for a in sorted(ticker_hits, key=lambda a: act_date(a) or ""):
            print(f"  {act_date(a)}  {str(a.get('type')):<10} units={a.get('units')} "
                  f"price={a.get('price')} amount={a.get('amount')}")


if __name__ == "__main__":
    main()
