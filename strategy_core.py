"""Pure strategy math: buy/sell triggers and position sizing.

Shared single source of truth so the live recommender (recommend_orders.py) and
the backtest simulator (strategy_sim.py) decide "buy here / sell here / size this"
with the exact same code - they cannot drift. No I/O, no DB, no prices fetching;
just functions of numbers + the strategy constants below.

All percentages are PLACEHOLDERS to be tuned via backtest (see STRATEGY.md).
"""
import math

DEFAULT_BUFFER_PCT = 3.5         # trailing-stop distance below price (= arming margin)

# Per-lot sell targets and partial-sale fraction.
LOW_TARGET_PCT = 10.0            # low-conviction (full) lot profit target
HIGH_TARGET_PCT = 50.0           # high-conviction lot profit target
LOW_SELL_FRACTION = 0.90         # low-target lots sell this fraction (floor, keep >=1 sh)

# Buy triggers.
BUY_DIP_PCT = 10.0               # average down when price is this % below the cheapest full lot
RE_ENTRY_DIP_PCT = 25.0          # re-enter (no full lot left) when this % below the 52-week high
EPSILON = 1e-9

# Buy-side guardrails (drawdown-scaled sizing + 200d-MA breaker).
DD_FULL_PCT = 20.0               # full add size up to this drawdown from the 52-week high
DD_STOP_PCT = 50.0               # stop adding below this drawdown (size -> 0)
MA_BREAKER_DAYS = 126            # ~6 months: stop adding if below a *falling* 200d MA this long


def add_size_factor(drawdown_pct, below_ma_days, ma_slope):
    """Buy-size multiplier in [0, 1] from the drawdown-scaling + MA breaker.

    Returns (factor, reason). drawdown_pct is negative below the 52-week high.
      - below a falling 200-day MA for >= MA_BREAKER_DAYS  -> 0 (breaker)
      - drawdown deeper than DD_STOP_PCT                   -> 0 (breaker)
      - drawdown <= DD_FULL_PCT                            -> 1 (full size)
      - in between                                         -> linear taper 1 -> 0
    """
    if (below_ma_days is not None and ma_slope is not None
            and below_ma_days >= MA_BREAKER_DAYS and ma_slope < 0):
        return 0.0, f"breaker: below falling 200d MA {below_ma_days}d"
    dd = max(0.0, -(drawdown_pct or 0.0))
    if dd >= DD_STOP_PCT:
        return 0.0, f"breaker: drawdown -{dd:.0f}%"
    if dd <= DD_FULL_PCT:
        return 1.0, f"full size (drawdown -{dd:.0f}%)"
    factor = (DD_STOP_PCT - dd) / (DD_STOP_PCT - DD_FULL_PCT)
    return factor, f"tapered {factor:.0%} (drawdown -{dd:.0f}%)"


def sell_quantity(remaining, target_pct):
    """Shares to sell for a lot: 90% (rounded down to a whole share) at the low
    target, otherwise the full remaining quantity. Rounding down means a small
    low-target lot always keeps at least one share."""
    if abs(target_pct - LOW_TARGET_PCT) < 1e-9:
        return math.floor(remaining * LOW_SELL_FRACTION)
    return remaining


def evaluate_lot(per_share, target_pct, price, buffer_pct):
    """Return (status, target_price, arm_price, stop).

    status is one of armed / watch / below / noprice. `arm_price` is the price at
    which a buffer% trailing stop first reaches the target. `stop` is set only
    when armed (= current price minus buffer%, which by construction >= target).
    """
    target_price = per_share * (1 + target_pct / 100)
    factor = 1 - buffer_pct / 100
    arm_price = target_price / factor if factor > 0 else None
    if price is None:
        return "noprice", target_price, arm_price, None
    stop = price * factor
    if stop >= target_price:
        return "armed", target_price, arm_price, stop
    if price >= target_price:
        return "watch", target_price, arm_price, None
    return "below", target_price, arm_price, None


def evaluate_buy(cheapest_cost, price, dip_pct):
    """Average-down trigger. Returns (armed, threshold) where threshold is the
    price at/below which to buy (cheapest open lot minus dip_pct%)."""
    threshold = cheapest_cost * (1 - dip_pct / 100)
    armed = price is not None and price <= threshold + EPSILON
    return armed, threshold
