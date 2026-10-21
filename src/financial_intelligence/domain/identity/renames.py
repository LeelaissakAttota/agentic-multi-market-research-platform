"""Known ticker changes after renames / demergers (old -> current)."""

from __future__ import annotations

#: Old NSE/BSE tickers that changed after renames/demergers (old -> current).
FORMER_TICKERS: dict[str, str] = {
    "TATAMOTORS": "TMPV",
    "ZOMATO": "ETERNAL",
    "MINDTREE": "LTIM",
    "LTI": "LTIM",
    "ADANITRANS": "ADANIENSOL",
    "MCDOWELL-N": "UNITDSPR",
    "IBULHSGFIN": "SAMMAANCAP",
}


def current_ticker_for(ticker: str) -> str | None:
    """Current ticker for a well-known former ticker, else None."""

    return FORMER_TICKERS.get(ticker.strip().upper())
