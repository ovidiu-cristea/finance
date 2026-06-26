"""Shared scoring core for the durability whitelist.

Vendor-agnostic: the Massive fetcher (build_durability.py) and the yfinance
fetcher (build_durability_yf.py) both import from here, so a name is scored and
vetoed identically regardless of where its financials came from. No HTTP/vendor
code lives in this module.

Everything tunable lives in CONFIG. Weights need not sum to 100 (score is
normalized). The philosophy is deliberately contrarian: veto only genuine
terminal risk (can't survive), never mere unprofitability.
"""
import datetime
import math
import sqlite3

# ---------------------------------------------------------------------------
# Tunable configuration. Adjust freely; the classification logic reads only
# these values. Weights need not sum to 100 (the score is normalized).
# ---------------------------------------------------------------------------
CONFIG = {
    # ---- hard vetoes (any -> TERMINAL / exit) -----------------------------
    "min_market_cap": 300e6,        # below this = micro-cap fragility veto
    "min_runway_quarters": 4.0,     # below this (only when burning cash) = runway veto
    "veto_negative_equity": True,   # equity < 0 AND burning cash = insolvency veto
                                    # (profitable buyback-driven negative equity, e.g.
                                    #  TDG/AAL/W, is NOT vetoed - it self-funds)

    # ---- quality-score weights (relative; normalized to 100) --------------
    "w_balance": 30.0,              # balance-sheet strength
    "w_burn": 30.0,                 # profitability / burn-trend
    "w_revenue": 20.0,              # revenue trend
    "w_scale": 20.0,                # market-cap scale
    "eligible_min_score": 60.0,     # >= this -> ELIGIBLE, else HOLD_ONLY

    # ---- component knobs --------------------------------------------------
    "runway_good_quarters": 12.0,   # runway >= this -> full runway sub-score
    "solvency_good_ratio": 2.0,     # equity/liabilities >= this -> full sub-score
    "wc_good_ratio": 2.0,           # current ratio >= this -> full sub-score
    "margin_good_pct": 10.0,        # operating margin >= this -> full burn score
    "margin_floor_pct": -50.0,      # operating margin <= this -> zero burn score
    "rev_cagr_good_pct": 20.0,      # revenue CAGR >= this -> full revenue score
    "scale_full_cap": 10e9,         # market cap >= this -> full scale score
    "scale_floor_cap": 300e6,       # market cap at the floor -> zero scale score
}


def clamp01(x):
    return max(0.0, min(1.0, x))


def held_tickers(db_path):
    rows = sqlite3.connect(db_path).execute(
        "SELECT DISTINCT symbol FROM tax_lots WHERE status = 'open'").fetchall()
    return sorted(r[0] for r in rows if r[0])


def load_flags(conn):
    """{symbol: manual_flag} for names with a user-set going-concern/terminal flag."""
    return dict(conn.execute(
        "SELECT symbol, manual_flag FROM durability "
        "WHERE manual_flag IS NOT NULL AND manual_flag != ''"))


def line(stmt, key):
    """Pull a numeric line item value from a statement dict, or None."""
    item = (stmt or {}).get(key)
    if isinstance(item, dict):
        return item.get("value")
    return None


def extract(report):
    """Pull the metrics we care about from one financials report (canonical shape)."""
    fin = report.get("financials") or {}
    bs, inc, cf = fin.get("balance_sheet"), fin.get("income_statement"), fin.get("cash_flow_statement")
    return dict(
        total_assets=line(bs, "assets"),
        total_liabilities=line(bs, "liabilities"),
        equity=line(bs, "equity"),
        current_assets=line(bs, "current_assets"),
        current_liabilities=line(bs, "current_liabilities"),
        revenues=line(inc, "revenues"),
        operating_income=line(inc, "operating_income_loss"),
        net_income=line(inc, "net_income_loss"),
        ocf=line(cf, "net_cash_flow_from_operating_activities"),
        fiscal_year=report.get("fiscal_year"),
        as_of=report.get("end_date"),
    )


def compute(details, reports, cfg, manual_flag):
    """Returns a dict of metrics + component scores + class. reports are desc by date."""
    m = extract(reports[0])
    prior = extract(reports[1]) if len(reports) > 1 else None
    m["market_cap"] = details.get("market_cap")

    # ---- runway (only meaningful when burning cash) -----------------------
    ocf, ca = m["ocf"], m["current_assets"]
    runway_q = None
    if ocf is not None and ocf < 0 and ca:
        runway_q = ca / (-ocf / 4.0)   # quarters of liquidity at current burn
    m["runway_quarters"] = runway_q

    # ---- revenue CAGR over the available annual reports -------------------
    revs = [extract(r)["revenues"] for r in reversed(reports)]   # oldest -> newest
    revs = [v for v in revs if v is not None]
    rev_cagr = None
    if len(revs) >= 2 and revs[0] and revs[0] > 0 and revs[-1] > 0:
        rev_cagr = (revs[-1] / revs[0]) ** (1.0 / (len(revs) - 1)) - 1.0
    m["rev_cagr"] = rev_cagr

    # ---- burn trend (YoY change in OCF; positive = burning less) ----------
    burn_trend = None
    if prior and ocf is not None and prior["ocf"] is not None:
        burn_trend = ocf - prior["ocf"]
    m["burn_trend"] = burn_trend

    # ---- hard vetoes ------------------------------------------------------
    vetoes = []
    if manual_flag:
        vetoes.append("manual")
    if m["market_cap"] is not None and m["market_cap"] < cfg["min_market_cap"]:
        vetoes.append("microcap")
    if (cfg["veto_negative_equity"] and m["equity"] is not None and m["equity"] < 0
            and ocf is not None and ocf < 0):
        vetoes.append("negative_equity")
    if runway_q is not None and runway_q < cfg["min_runway_quarters"]:
        vetoes.append("runway")
    m["vetoes"] = ",".join(vetoes)

    # ---- quality-score components (each 0..1) -----------------------------
    # Balance-sheet strength: runway + solvency + working capital.
    if ocf is not None and ocf >= 0:
        runway01 = 1.0                                   # generating cash -> no runway risk
    elif runway_q is not None:
        runway01 = clamp01(runway_q / cfg["runway_good_quarters"])
    else:
        runway01 = 0.5                                   # unknown burn/liquidity
    eq, tl = m["equity"], m["total_liabilities"]
    if eq is not None and eq < 0:
        solv01 = 0.0
    elif eq is not None and tl:
        solv01 = clamp01((eq / tl) / cfg["solvency_good_ratio"])
    else:
        solv01 = 0.5
    cl = m["current_liabilities"]
    if ca is not None and cl:
        wc01 = clamp01(((ca / cl) - 1.0) / (cfg["wc_good_ratio"] - 1.0))
    else:
        wc01 = 0.5
    balance01 = (runway01 + solv01 + wc01) / 3.0

    # Profitability / burn.
    rev, oi = m["revenues"], m["operating_income"]
    if rev and rev > 0 and oi is not None:
        margin_pct = oi / rev * 100.0
        burn01 = clamp01((margin_pct - cfg["margin_floor_pct"]) /
                         (cfg["margin_good_pct"] - cfg["margin_floor_pct"]))
    elif prior and prior["ocf"] and prior["ocf"] < 0 and ocf is not None:
        improvement = (ocf - prior["ocf"]) / abs(prior["ocf"])   # >0 = burning less
        burn01 = clamp01(0.5 + improvement)
    else:
        burn01 = 0.5

    # Revenue trend.
    rev01 = clamp01(rev_cagr / (cfg["rev_cagr_good_pct"] / 100.0)) if rev_cagr is not None else 0.0

    # Scale (log interpolation between floor and full market cap).
    mc = m["market_cap"]
    if mc and mc > 0:
        lo, hi = math.log(cfg["scale_floor_cap"]), math.log(cfg["scale_full_cap"])
        scale01 = clamp01((math.log(mc) - lo) / (hi - lo))
    else:
        scale01 = 0.0

    m["score_balance"] = balance01 * cfg["w_balance"]
    m["score_burn"] = burn01 * cfg["w_burn"]
    m["score_revenue"] = rev01 * cfg["w_revenue"]
    m["score_scale"] = scale01 * cfg["w_scale"]
    wsum = cfg["w_balance"] + cfg["w_burn"] + cfg["w_revenue"] + cfg["w_scale"]
    m["score"] = 100.0 * (balance01 * cfg["w_balance"] + burn01 * cfg["w_burn"] +
                          rev01 * cfg["w_revenue"] + scale01 * cfg["w_scale"]) / wsum

    if vetoes:
        m["class"] = "TERMINAL"
    elif m["score"] >= cfg["eligible_min_score"]:
        m["class"] = "ELIGIBLE"
    else:
        m["class"] = "HOLD_ONLY"
    return m


def upsert(conn, symbol, m):
    cols = ("class", "score", "score_balance", "score_burn", "score_revenue",
            "score_scale", "market_cap", "total_assets", "total_liabilities",
            "equity", "current_assets", "current_liabilities", "revenues",
            "operating_income", "net_income", "ocf", "runway_quarters",
            "rev_cagr", "burn_trend", "vetoes", "fiscal_year", "as_of")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in cols)
    conn.execute(
        f"""INSERT INTO durability (symbol, {", ".join(cols)}, computed_at)
            VALUES ({", ".join("?" * (len(cols) + 2))})
            ON CONFLICT(symbol) DO UPDATE SET {set_clause}, computed_at=excluded.computed_at""",
        (symbol, *(m.get(c) for c in cols), now),
    )


def fmt_row(symbol, m, extra=""):
    """Shared one-line summary used by both fetchers' output tables."""
    mc = f"{m['market_cap']/1e9:.2f}B" if m.get("market_cap") else "?"
    rq = f"{m['runway_quarters']:.1f}" if m.get("runway_quarters") is not None else "-"
    rc = f"{m['rev_cagr']*100:+.0f}%" if m.get("rev_cagr") is not None else "-"
    return (f"  {symbol:<8} {m['class']:<10} {m['score']:>6.1f} {mc:>9} "
            f"{rq:>8} {rc:>8} {extra}{m['vetoes']}")


HEADER = (f"  {'ticker':<8} {'class':<10} {'score':>6} {'mktcap':>9} "
          f"{'runwayQ':>8} {'revCAGR':>8}")
