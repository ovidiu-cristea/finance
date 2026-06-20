"""Reconcile (or re-seed) one security's tax lots in one account from a
copy/pasted Fidelity lot view.

Paste the Fidelity "Cost basis / lots" view for a single position into a text
file, e.g.:

    Individual - TODZ19873349
    Option summary
    Manage dividends
    UNITY SOFTWARE INC COM
    Acquired<TAB>Term<TAB>$ Total gain/loss<TAB>...<TAB>Cost basis total
    Jun-10-2026<TAB>Short<TAB>-$39.00<TAB>-2.86%<TAB>$1,323.50<TAB>50<TAB>$27.25<TAB>$1,362.50
    ...

The ticker is NOT in the paste, so pass it explicitly:

    python reconcile_lots.py U unity.txt            # dry run: show the diff
    python reconcile_lots.py U unity.txt --apply     # write the lots

The account is resolved from the header line by its last 4 digits; override
with --account <id-or-number> if parsing fails.
"""
import argparse
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIDELITY_DATE = "%b-%d-%Y"

# Known Fidelity accounts (SnapTrade id -> metadata). account_type is manual.
# These stubs are upserted so the tax_lots FK resolves; the sync refreshes them.
ACCOUNTS = {
    "935ff59e-ca43-4da2-bf09-51085193ff72": dict(
        name="Individual - TOD", number="*****3349", account_type="taxable"),
    "d7695b5c-5710-4d70-a473-b59e4d9b5dda": dict(
        name="ROTH IRA", number="*****9270", account_type="roth-IRA"),
    "3cf06dd4-f93f-4821-9092-cc965d71de31": dict(
        name="Traditional IRA", number="*****4711", account_type="traditional-IRA"),
    "ebb264b0-098b-4b6b-be37-6dc7fa79aae3": dict(
        name="Health Savings Account", number="*****5187", account_type="HSA"),
    "ccace6c4-093f-40d4-b90a-e6e90730bb0f": dict(
        name="Rollover IRA", number="*****4749", account_type="rollover-IRA"),
}


def parse_amount(text):
    """'$1,362.50' / '-$39.00' / '-2.86%' -> float."""
    return float(text.replace("$", "").replace(",", "").replace("%", "").strip())


def split_row(line):
    """Split a pasted row on tabs, falling back to runs of 2+ spaces."""
    return re.split(r"\t+", line) if "\t" in line else re.split(r"\s{2,}", line)


def parse_file(text):
    """Return dict(account_name, account_number, security_desc, lots)."""
    lines = [ln for ln in (l.rstrip() for l in text.splitlines()) if ln.strip()]
    if not lines:
        sys.exit("Error: file is empty")

    header = lines[0].strip()
    m = re.search(r"([A-Za-z]?\d{4,})\s*$", header)
    account_number = m.group(1) if m else None
    account_name = (header[:m.start()] if m else header).strip()

    # The column-header row anchors the table; the security name is just above it.
    col_idx = next(
        (i for i, ln in enumerate(lines)
         if "Acquired" in ln and "Cost basis" in ln),
        None,
    )
    if col_idx is None or col_idx == 0:
        sys.exit("Error: could not find the lot table header (Acquired ... Cost basis total)")
    security_desc = lines[col_idx - 1].strip()

    lots = []
    for ln in lines[col_idx + 1:]:
        cols = [c.strip() for c in split_row(ln)]
        try:
            open_date = datetime.strptime(cols[0], FIDELITY_DATE).strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            continue  # skip non-lot lines (footers, blanks, etc.)
        if len(cols) < 8:
            sys.exit(f"Error: lot row has {len(cols)} columns, expected 8:\n  {ln!r}")
        qty = parse_amount(cols[5])
        avg_cost = parse_amount(cols[6])
        cost_basis = parse_amount(cols[7])
        lots.append((open_date, qty, avg_cost, cost_basis))

    if not lots:
        sys.exit("Error: no lot rows parsed")
    return dict(account_name=account_name, account_number=account_number,
                security_desc=security_desc, lots=lots)


def resolve_account(override, account_number):
    """Map to a known SnapTrade account id by id, number, or last 4 digits."""
    if override:
        if override in ACCOUNTS:
            return override
        digits = re.sub(r"\D", "", override)[-4:]
    elif account_number:
        digits = re.sub(r"\D", "", account_number)[-4:]
    else:
        sys.exit("Error: could not parse an account number; pass --account")

    matches = [aid for aid, m in ACCOUNTS.items()
               if re.sub(r"\D", "", m["number"])[-4:] == digits]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"Error: no known account ends in {digits}; pass --account")
    sys.exit(f"Error: account suffix {digits} is ambiguous; pass --account <id>")


def lot_key(open_date, qty, cost_basis):
    return (open_date, round(qty, 6), round(cost_basis, 2))


def reconcile(conn, account_id, symbol, parsed, apply):
    acct = ACCOUNTS[account_id]
    conn.execute(
        """
        INSERT INTO accounts (id, institution, name, number, status, account_type)
        VALUES (?, 'Fidelity', ?, ?, 'open', ?)
        ON CONFLICT(id) DO UPDATE SET
            account_type = COALESCE(accounts.account_type, excluded.account_type)
        """,
        (account_id, acct["name"], acct["number"], acct["account_type"]),
    )
    conn.execute(
        """
        INSERT INTO securities (symbol, description, kind)
        VALUES (?, ?, 'equity')
        ON CONFLICT(symbol) DO UPDATE SET
            description = COALESCE(securities.description, excluded.description)
        """,
        (symbol, parsed["security_desc"]),
    )

    existing = conn.execute(
        """
        SELECT open_date, remaining_quantity, cost_basis, source, buy_order_id,
               target_min_profit_pct
        FROM tax_lots
        WHERE account_id = ? AND symbol = ? AND status = 'open'
        """,
        (account_id, symbol),
    ).fetchall()

    file_lots = parsed["lots"]
    file_keys = Counter(lot_key(d, q, b) for d, q, _, b in file_lots)
    db_keys = Counter(lot_key(r[0], r[1], r[2]) for r in existing)
    added = file_keys - db_keys
    removed = db_keys - file_keys
    sync_removed = sum(1 for r in existing if r[3] == "sync" or r[4])

    print(f"Account : {account_id}  ({acct['name']})")
    print(f"Symbol  : {symbol}  ({parsed['security_desc']})")
    print(f"Current : {len(existing)} open lots, "
          f"{sum(r[1] for r in existing):g} sh, "
          f"${sum(r[2] for r in existing):,.2f} basis")
    print(f"Fidelity: {len(file_lots)} lots, "
          f"{sum(q for _, q, _, _ in file_lots):g} sh, "
          f"${sum(b for _, _, _, b in file_lots):,.2f} basis")

    if not added and not removed:
        print("\nIn sync - no changes.")
        return
    print("\nDifferences (Fidelity vs DB):")
    for key, n in sorted(removed.items()):
        for _ in range(n):
            print(f"  - remove  {key[0]}  {key[1]:g} sh  ${key[2]:,.2f}")
    for key, n in sorted(added.items()):
        for _ in range(n):
            print(f"  + add     {key[0]}  {key[1]:g} sh  ${key[2]:,.2f}")
    if sync_removed:
        print(f"\n  ! {sync_removed} open lot(s) had sync origin / a buy order id "
              f"and will be replaced by seed lots.")

    if not apply:
        print("\nDry run - re-run with --apply to write these lots.")
        return

    # Preserve manual per-lot targets across the replace: carry over the
    # target_min_profit_pct of any old lot whose key matches a new lot.
    old_targets = {}
    for r in existing:
        old_targets.setdefault(lot_key(r[0], r[1], r[2]), []).append(r[5])

    conn.execute(
        "DELETE FROM tax_lots WHERE account_id = ? AND symbol = ? AND status = 'open'",
        (account_id, symbol),
    )
    carried = 0
    for d, q, avg, b in file_lots:
        targets = old_targets.get(lot_key(d, q, b))
        target = targets.pop(0) if targets else None
        if target is not None:
            carried += 1
        conn.execute(
            """
            INSERT INTO tax_lots
                (account_id, symbol, open_date, original_quantity,
                 remaining_quantity, price_per_share, cost_basis, source, status,
                 target_min_profit_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'seed', 'open', ?)
            """,
            (account_id, symbol, d, q, q, avg, b, target),
        )
    conn.commit()
    note = f" ({carried} target% carried over)" if carried else ""
    print(f"\nApplied: {len(file_lots)} seed lots written for {symbol}.{note}")


def main():
    ap = argparse.ArgumentParser(description="Reconcile Fidelity tax lots into the holdings DB.")
    ap.add_argument("symbol", help="Ticker for the position (not in the paste), e.g. U")
    ap.add_argument("file", help="Text file containing the pasted Fidelity lot view")
    ap.add_argument("--db", default=str(HERE / "holdings.db"), help="Path to the SQLite DB")
    ap.add_argument("--account", help="Account id or number, if the header can't be parsed")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = ap.parse_args()

    parsed = parse_file(Path(args.file).read_text(encoding="utf-8-sig"))
    account_id = resolve_account(args.account, parsed["account_number"])

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        reconcile(conn, account_id, args.symbol, parsed, args.apply)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
