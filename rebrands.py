"""Internal registry of ticker rebrands / symbol changes.

Brokerages and SnapTrade sometimes lag a ticker change, reporting a position
under the OLD symbol while Fidelity's current lot view (and our DB) use the NEW
one. List each rebrand here so reconciliation and order ingest treat the old and
new tickers as the same security.

Keep entries newest-first. `effective` is the date the new ticker began trading.
"""

# old -> new ticker, with the effective date and a human note.
REBRANDS = [
    {"old": "SAVA", "new": "FLNA", "effective": "2026-03-11",
     "note": "Cassava Sciences Inc. -> Filana Therapeutics, Inc."},
]

_OLD_TO_NEW = {r["old"].upper(): r["new"].upper() for r in REBRANDS}


def canonical_symbol(symbol):
    """Map a possibly-outdated ticker to its current (canonical) symbol.

    Returns the input unchanged if it is not a known former ticker. Applying it
    to an already-current symbol is a no-op, so it is safe to normalize both the
    SnapTrade side and the DB side through this function.
    """
    if not symbol:
        return symbol
    return _OLD_TO_NEW.get(symbol.upper(), symbol)
