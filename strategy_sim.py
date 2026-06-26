"""Day-by-day backtest of the volatility-harvesting strategy on a price series.

Pure simulator: feed it daily bars + params + a starting lot state, it replays the
strategy and returns trades + an equity curve + a metric set. Reused by:
  * analyze_ticker.py  - seed one synthetic base lot -> screen a candidate.
  * the portfolio backtest - seed real lots, sweep params to tune thresholds.

Every buy/sell/size decision goes through strategy_core (same code as the live
recommend_orders.py), and the daily 52wk-high / drawdown / 200d-MA snapshot goes
through build_metrics.compute_metrics (same code as the live stock_metrics) - so
the backtest and the live system can't drift.

Mechanics replicated: average-down buys (drawdown-scaled size + 200d-MA breaker),
re-entry, trailing-stop sells (low target sells 90% then the remainder bumps to
the high target; high target sells all; keep-last-share), ratcheting stops.

Bars must include ~252 trading days of lookback BEFORE the window start, so the
52-week high / 200-day MA are valid on day 1 of the window.
"""
import datetime
from dataclasses import dataclass, field

from build_metrics import bar_date, compute_metrics
from strategy_core import (
    BUY_DIP_PCT, DEFAULT_BUFFER_PCT, EPSILON, HIGH_TARGET_PCT, LOW_TARGET_PCT,
    RE_ENTRY_DIP_PCT, add_size_factor, evaluate_buy, evaluate_lot, sell_quantity,
)


@dataclass
class SimParams:
    base_shares: float = 100.0          # the "full lot" unit (normalized; results are ratios)
    buffer_pct: float = DEFAULT_BUFFER_PCT
    dip_pct: float = BUY_DIP_PCT
    reentry_dip_pct: float = RE_ENTRY_DIP_PCT
    low_target_pct: float = LOW_TARGET_PCT
    high_target_pct: float = HIGH_TARGET_PCT
    position_cap: float = None          # $ ceiling on position cost (None = drawdown-sizing only)


def _is_low(lot, params):
    return abs(lot["target_pct"] - params.low_target_pct) < 1e-9


def _new_lot(date, qty, price, target_pct):
    return {"open_date": date, "original_qty": qty, "remaining_qty": qty,
            "cost_per_share": price, "target_pct": target_pct, "stop": None}


@dataclass
class SimResult:
    ticker: str = ""
    start_date: str = ""
    end_date: str = ""
    years: float = 0.0
    buys: int = 0
    sells: int = 0
    total_invested: float = 0.0         # sum of all buy notional (seed + adds)
    max_capital: float = 0.0            # peak simultaneous position cost basis (capital at risk)
    realized_pnl: float = 0.0           # harvested gains from trailing-stop sells
    unrealized_pnl: float = 0.0         # final mark-to-market of the residual position
    total_pnl: float = 0.0
    return_on_capital: float = 0.0      # total_pnl / max_capital  (strategy return %)
    bh_return: float = 0.0              # buy-and-hold the seed lot over the same window
    edge_vs_hold: float = 0.0           # return_on_capital - bh_return
    max_drawdown: float = 0.0           # deepest peak-to-trough of net P&L, as % of max_capital
    risk_adjusted: float = 0.0          # return_on_capital / max_drawdown
    harvest_per_year: float = 0.0       # sells per year (oscillation frequency)
    atr_pct: float = 0.0                # end-of-window ATR% (volatility fingerprint)
    final_drawdown: float = 0.0         # end-of-window drawdown from 52wk high
    events: list = field(default_factory=list)


def simulate(bars, window_start_idx, params=None, initial_lots=None, ticker=""):
    """Replay the strategy over bars[window_start_idx:]. Returns a SimResult."""
    params = params or SimParams()
    bars = [b for b in bars if b.get("c") is not None]
    n = len(bars)
    if window_start_idx >= n:
        return SimResult(ticker=ticker)
    buf = params.buffer_pct

    start_price = bars[window_start_idx]["c"]
    if initial_lots is None:
        lots = [_new_lot(bar_date(bars[window_start_idx]), params.base_shares,
                         start_price, params.low_target_pct)]
        total_invested = params.base_shares * start_price
    else:
        lots = [dict(l, stop=None) for l in initial_lots]
        total_invested = sum(l["remaining_qty"] * l["cost_per_share"] for l in lots)

    realized = 0.0
    max_capital = sum(l["remaining_qty"] * l["cost_per_share"] for l in lots)
    peak_equity, max_dd = 0.0, 0.0
    buys = sells = 0
    events = []

    for i in range(window_start_idx, n):
        bar = bars[i]
        price, low, opn, date = bar["c"], bar["l"], bar["o"], bar_date(bar)
        snap = compute_metrics(bars[:i + 1]) or {}

        # ---- SELL pass: trigger placed stops, then (re-)arm ----
        for lot in lots:
            if lot["remaining_qty"] <= 0:
                continue
            if lot["stop"] is not None and low <= lot["stop"]:
                fill = opn if opn < lot["stop"] else lot["stop"]   # gap-down fills at the open
                qty = sell_quantity(lot["remaining_qty"], lot["target_pct"])
                if qty <= 0:                                       # keep-last-share: nothing to sell
                    continue
                realized += qty * (fill - lot["cost_per_share"])
                lot["remaining_qty"] -= qty
                sells += 1
                events.append((date, "SELL", qty, round(fill, 4), lot["target_pct"]))
                if lot["remaining_qty"] > 0 and _is_low(lot, params):
                    lot["target_pct"] = params.high_target_pct     # bump the kept remainder
                lot["stop"] = None
                continue
            status, _, _, stop = evaluate_lot(lot["cost_per_share"], lot["target_pct"], price, buf)
            if status == "armed":
                lot["stop"] = max(lot["stop"] or 0.0, stop)        # ratchet up only

        # ---- BUY pass: average down / re-enter ----
        open_low = [l for l in lots if l["remaining_qty"] > 0 and _is_low(l, params)]
        cheapest = min((l["cost_per_share"] for l in open_low), default=None)
        if cheapest is None:                                       # no full lot -> re-entry
            dd = snap.get("drawdown_pct")
            armed = dd is not None and dd <= -params.reentry_dip_pct
        else:
            armed, _ = evaluate_buy(cheapest, price, params.dip_pct)
        if armed:
            factor, _ = add_size_factor(snap.get("drawdown_pct"),
                                        snap.get("below_ma_days"), snap.get("ma_200_slope"))
            qty = round(params.base_shares * factor)
            pos_cost = sum(l["remaining_qty"] * l["cost_per_share"] for l in lots)
            capped = params.position_cap and pos_cost + qty * price > params.position_cap
            if factor > EPSILON and qty >= 1 and not capped:
                lots.append(_new_lot(date, qty, price, params.low_target_pct))
                total_invested += qty * price
                buys += 1
                events.append((date, "BUY", qty, round(price, 4), params.low_target_pct))

        # ---- mark-to-market: capital at risk + equity drawdown ----
        pos_cost = sum(l["remaining_qty"] * l["cost_per_share"] for l in lots)
        pos_value = sum(l["remaining_qty"] * price for l in lots)
        equity = realized + (pos_value - pos_cost)                 # net P&L, marked daily
        max_capital = max(max_capital, pos_cost)
        peak_equity = max(peak_equity, equity)
        max_dd = max(max_dd, peak_equity - equity)

    # ---- final metrics ----
    end_bar = bars[-1]
    end_price, end_date = end_bar["c"], bar_date(end_bar)
    snap = compute_metrics(bars) or {}
    pos_cost = sum(l["remaining_qty"] * l["cost_per_share"] for l in lots)
    pos_value = sum(l["remaining_qty"] * end_price for l in lots)
    unrealized = pos_value - pos_cost
    total_pnl = realized + unrealized
    days = (datetime.date.fromisoformat(end_date) -
            datetime.date.fromisoformat(bar_date(bars[window_start_idx]))).days
    years = days / 365.25 if days else 0.0

    ret = total_pnl / max_capital if max_capital else 0.0
    bh = end_price / start_price - 1 if start_price else 0.0
    dd_pct = max_dd / max_capital if max_capital else 0.0
    return SimResult(
        ticker=ticker, start_date=bar_date(bars[window_start_idx]), end_date=end_date,
        years=round(years, 2), buys=buys, sells=sells,
        total_invested=round(total_invested, 2), max_capital=round(max_capital, 2),
        realized_pnl=round(realized, 2), unrealized_pnl=round(unrealized, 2),
        total_pnl=round(total_pnl, 2), return_on_capital=round(ret, 4),
        bh_return=round(bh, 4), edge_vs_hold=round(ret - bh, 4),
        max_drawdown=round(dd_pct, 4),
        risk_adjusted=round(ret / dd_pct, 3) if dd_pct > EPSILON else None,
        harvest_per_year=round(sells / years, 2) if years else 0.0,
        atr_pct=round(snap.get("atr_pct") or 0.0, 2),
        final_drawdown=round(snap.get("drawdown_pct") or 0.0, 2),
        events=events,
    )


def window_start_index(bars, years):
    """Index of the first bar within `years` of the last bar (the rest is lookback)."""
    if not bars:
        return 0
    cutoff = datetime.date.fromisoformat(bar_date(bars[-1])) - datetime.timedelta(days=round(years * 365.25))
    for i, b in enumerate(bars):
        if datetime.date.fromisoformat(bar_date(b)) >= cutoff:
            return i
    return len(bars) - 1
