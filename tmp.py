import sys
from pathlib import Path

from snaptrade_client import SnapTrade

client_id = "PERS-97BRLMMWM55XNVEEORUA"

def to_float(value):
    """Coerce SDK numeric values (often decimal strings) to float; None on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_secret_file(path):
    secret = Path(path).read_text(encoding="utf-8-sig").strip()
    if not secret:
        sys.exit(f"Error: {path} is empty")
    return secret


def list_user_accounts(snaptrade, consumer_key):
    """Return all brokerage accounts connected to this SnapTrade user."""
    return snaptrade.account_information.list_user_accounts(
        user_id=client_id,
        user_secret=consumer_key,
    ).body


def list_orders(snaptrade, consumer_key, account_id, state="all", days=10):
    """Return orders for one account; state is 'all', 'open' or 'executed'."""
    return snaptrade.account_information.get_user_account_orders(
        user_id=client_id,
        user_secret=consumer_key,
        account_id=account_id,
        state=state,
        days=days,
    ).body

def list_executed_orders(snaptrade, consumer_key, account_id, days=10):
    """Return executed (filled) orders for one account over the trailing `days`."""
    return snaptrade.account_information.get_user_account_orders(
        user_id=client_id,
        user_secret=consumer_key,
        account_id=account_id,
        state="executed",
        days=days,
    ).body


def list_positions(snaptrade, consumer_key, account_id):
    """Return all positions (with tax lots when the brokerage provides them)."""
    return snaptrade.account_information.get_all_account_positions(
        user_id=client_id,
        user_secret=consumer_key,
        account_id=account_id,
    ).body


def position_symbol(position):
    """Human-readable ticker for a position."""
    instrument = position.get("instrument") or {}
    if instrument:
        return instrument.get("symbol") or "?"
    # fallback for the legacy positions shape
    sym = position.get("symbol") or {}
    inner = sym.get("symbol") or {}
    if isinstance(inner, dict):
        return inner.get("symbol") or inner.get("raw_symbol") or "?"
    return inner or "?"


def order_symbol(order):
    """Best-effort human-readable symbol for an order."""
    uni = order.get("universal_symbol") or {}
    opt = order.get("option_symbol") or {}
    return uni.get("symbol") or opt.get("ticker") or order.get("symbol") or "?"


if len(sys.argv) < 2:
    sys.exit(f"Usage: {sys.argv[0]} <consumer-key-file>")

consumer_key = read_secret_file(sys.argv[1])


snaptrade = SnapTrade(
    client_id=client_id,
    consumer_key=consumer_key,
)

accounts = list_user_accounts(snaptrade, consumer_key)
for acct in accounts:
    balance = acct.get("balance") or {}
    total = balance.get("total") or {}
    print(
        f"{acct['id']}  {acct.get('institution_name', '?'):20}  "
        f"{acct.get('name') or '(unnamed)':30}  #{acct.get('number', '?')}  "
        f"{total.get('amount', '?')} {total.get('currency', '')}  "
        f"status={acct.get('status', '?')}"
    )

    response = list_positions(snaptrade, consumer_key, acct["id"])
    positions = response.get("results") or []
    for pos in positions:
        kind = (pos.get("instrument") or {}).get("kind", "")
        units = to_float(pos.get("units"))
        cost_per_share = to_float(pos.get("cost_basis"))  # avg purchase price per share
        total_cost = round(cost_per_share * units, 2) if cost_per_share and units else None
        print(
            f"    {position_symbol(pos):10} {kind:14} "
            f"units={units} price={pos.get('price')} "
            f"$/share={cost_per_share} total_cost={total_cost}"
        )
        for lot in pos.get("tax_lots") or []:
            print(
                f"        lot: bought={lot.get('original_purchase_date')} "
                f"qty={lot.get('quantity')} $/share={lot.get('purchased_price')} "
                f"cost_basis={lot.get('cost_basis')} value={lot.get('current_value')}"
            )

    orders = list_executed_orders(snaptrade, consumer_key, acct["id"], days=10)
    print(f"    executed orders (last 10 days): {len(orders)}")
    for order in orders:
        filled = to_float(order.get("filled_quantity")) or to_float(order.get("total_quantity"))
        print(
            f"      {order.get('time_executed') or order.get('time_placed') or '?':25} "
            f"{order.get('action', '?'):4} {order_symbol(order):10} "
            f"qty={filled} @ {order.get('execution_price')} "
            f"type={order.get('order_type', '?')} status={order.get('status', '?')}"
        )


