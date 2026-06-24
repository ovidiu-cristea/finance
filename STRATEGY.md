# Trading strategy

**Status:** sell side implemented; buy-side guardrails designed, not yet built.

> All percentages below are **placeholders** to be refined empirically (see
> *Open items*). They may also become **per-stock parameters** after more
> detailed analysis (e.g. volatility-normalized bands, per-name caps) — likely a
> per-stock config table in the DB.

## Philosophy

**Volatility harvesting / mean-reversion accumulation.** Scale *in* on weakness
(average down), scale *out* on strength (trailing-stop profit-taking on the
lowest-cost lots). Profit comes from *oscillation*, not from the stock going up.

The core risk: averaging into a stock whose volatility is a **structural decline
(permanent impairment)** rather than **mean-reverting chop**. The two look
identical day to day. So the buy side needs guardrails that bound the position
when a "dip" turns out to be a "regime change."

## Two gates for what to buy

- **Durability gate (the stock):** *can this business survive a drawdown at all?*
  Slow/fundamental. For now a **manual whitelist** (to be built later) — keeps
  melting-ice-cube names (pre-revenue, cash-burning, single-product, distressed)
  out of the averaging-down program. This is what price action can't see in time.
- **Regime gate (the moment):** *even for a durable name, is now a buyable dip or
  a falling knife?* Fast/technical — the contrarian guardrails below.

A name must clear **both** to be averaged into.

## Buy side (designed, not implemented)

**Base trigger (existing behaviour):** average down — buy when a position is
~10%+ below the cheapest "full lot" previously owned.
*Note:* anchoring to the cheapest lot ratchets the threshold down forever, which
is the mechanical reason a name can accumulate all the way down (e.g. QS, $86 →
$7). The guardrails below bound the consequences rather than changing the trigger.

**Contrarian guardrails (chosen 2026-06-20).** We deliberately are **not** using a
hard "don't buy below the 200-day MA" filter (too strict for a contrarian), so
**position sizing becomes the primary defence**:

1. **Drawdown-conditioned add size — fast-crash protection.** Add size is a
   function of drawdown from the **52-week high**:
   - 0 to ~ −20% (normal oscillation): **100%** (full lot size)
   - ~ −20% to ~ −50%: **decay linearly 100% → 0%**
   - below ~ −50%: **0** (stop)

   Responds to *price level*, so a violent collapse throttles adds within days.
   "Normal range" is ideally measured in the stock's **own volatility** (ATR /
   typical pullback), not a fixed %, but a fixed band is the v1.

2. **Prolonged-below-200-day-MA breaker — slow-grind protection.** Stop adding if
   the stock has been below a *falling* 200-day MA for more than ~6 months.
   Catches the multi-year structural decline that the 52-week-high drawdown metric
   is too forgiving of (the 52-wk high resets lower each year). This is the signal
   that would have stopped the QS accumulation.

3. **Hard max-position cap per name — backstop.** Explicit ceiling (X% of account
   or a $ cap). Decay makes it rarely bind, but it guarantees no single falling
   knife can dominate the account.

**Why this shape:** drawdown-sizing (fast crash) and the MA breaker (slow grind)
cover **different failure modes** — complementary, not redundant. Calibration must
avoid a *gap*: sizing should be near-zero *before* the (slow, time-based) MA
breaker fires, or a fast crash lets you add big size in the unprotected window.

**Reference high:** 52-week high drives the sizing throttle (responsive to the
recent regime); the MA breaker handles long structural declines (since the 52-wk
high forgives them).

## Sell side (implemented)

- **Per-lot target** (`tax_lots.target_min_profit_pct`): low **10%** / high
  **50%** conviction tiers.
- **Trailing stop** (`recommend_orders.py`, run end-of-day on Massive VWAP):
  `target_price = per_share_cost × (1 + target/100)`;
  `trailing_stop = price × (1 − 3.5%)`; recommend a SELL STOP at `trailing_stop`
  once it clears `target_price`; ratchet up daily, never lower.
- **Order size:** low-target lots sell **90%** (floor, keep ≥1 share); high-target
  lots sell all. After a 90% partial sale, the kept remainder is bumped from the
  low to the high target (`disambiguate_sells.py`).
- **Keep-last-share:** single-share lots have their target cleared (never sell the
  last share).

## Downside exit (designed 2026-06-21, not built)

**NOT a blanket loss-cut.** Beaten-down-but-viable names are **held, not cut** —
because (a) they still mean-revert/spike and the trim engine harvests those (QS
$7→$18 in 2025), (b) the **fully-paid lending program pays the most on exactly
these high-short-interest losers**, so the bag *yields income*, and (c)
opportunity cost is low (ample cash; and in IRAs there's no tax-harvest benefit
anyway). A blanket loss-cut would lock the loss, forfeit the spike, *and* forgo
the lending income.

**Exit ONLY on terminal risk** — a name actually heading to zero (bankruptcy,
delisting, dilution-to-oblivion). When a name dies, *both* pillars vanish at once:
it can't spike, and the borrow (lending income) disappears — you can't "wait out"
a zero. So the trigger is **solvency/viability, NOT price or the 200-day MA** —
i.e. the **durability whitelist inverted**: a name exits only when it drops off
the whitelist for *terminal* reasons, not for merely being down. Surgical and
rare; does **not** touch the held cohort (QS/LCID/RIVN/ENPH/DDD). Candidate so
far: **FLNA** (post-failure Cassava/Filana, −89%).

**Trap to respect:** highest lending yield ≈ highest distress — the income is
*risk compensation, not free money*. The terminal-risk filter is what stops you
getting paid ~15%/yr to ride a name to −100%.

> Lending income is an **omitted return component** in every P&L computed so far
> (QS etc.) — it should be quantified and netted in; it may reposition the whole
> picture, especially on the high-fee losers.

## Open items / not yet designed

- **Downside exit:** designed (above) — terminal-risk filter via the durability
  whitelist; not yet built. The rest of the system stays one-sided by design.
- **Durability whitelist (Gate 1):** manual for now; possibly automated
  fundamentals later (profitability, cash runway, balance sheet, Altman Z,
  size/liquidity).
- **Parameter refinement via backtest** against own history (QS, U, winners):
  would the −20 / −50 / 6-month / decay-slope bands have stopped the QS
  accumulation early without gutting the gains on names that worked? Set the
  numbers empirically, not by reasoning.
- **Per-stock parameters:** the percentages above may become per-stock after
  analysis → likely a per-stock config table.
- **Volatility-adjusted trailing (ATR)** and possibly **close-vs-VWAP** for the
  sell side (deferred; flat 3.5% + VWAP chosen for now).
