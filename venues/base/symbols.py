"""Canonical symbol mapping.

``BTCUSDT``, ``BTC-USD`` and ``XBT/USD`` all denote the same economic
instrument.  Everything past the venue adapter speaks the canonical form.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Canonical form is ``BASE-QUOTE`` with USD-pegged quotes normalised to USD.
CANONICAL_QUOTES = ("USD", "USDT", "USDC", "EUR")

_BASE_ALIASES = {
    "XBT": "BTC",
    "WBTC": "BTC",
    "WETH": "ETH",
}

#: Quotes treated as economically equivalent for cross-venue comparison. This
#: is an explicit modelling decision, not an accident: a USDT market is
#: comparable to a USD market for dislocation purposes, and the residual
#: stablecoin basis is a cost the transaction-cost model must eventually carry.
_QUOTE_ALIASES = {
    "USDT": "USD",
    "USDC": "USD",
    "BUSD": "USD",
}


@dataclass(frozen=True)
class Instrument:
    base: str
    quote: str

    @property
    def canonical(self) -> str:
        return f"{self.base}-{self.quote}"


class UnknownSymbol(ValueError):
    pass


def normalize(symbol: str) -> str:
    """Map any venue symbol format to the canonical ``BASE-QUOTE`` form."""
    return parse(symbol).canonical


def parse(symbol: str) -> Instrument:
    raw = symbol.strip().upper().replace("_", "-").replace("/", "-")
    if "-" in raw:
        base, _, quote = raw.partition("-")
    else:
        for quote_candidate in sorted(
            set(CANONICAL_QUOTES) | set(_QUOTE_ALIASES), key=len, reverse=True
        ):
            if raw.endswith(quote_candidate) and len(raw) > len(quote_candidate):
                base, quote = raw[: -len(quote_candidate)], quote_candidate
                break
        else:
            raise UnknownSymbol(f"cannot parse symbol: {symbol!r}")
    base = _BASE_ALIASES.get(base, base)
    quote = _QUOTE_ALIASES.get(quote, quote)
    if not base or not quote:
        raise UnknownSymbol(f"cannot parse symbol: {symbol!r}")
    return Instrument(base=base, quote=quote)


def denormalize(canonical: str, style: str) -> str:
    """Render a canonical symbol in a venue's own format."""
    instrument = parse(canonical)
    base, quote = instrument.base, instrument.quote
    if style == "concat_usdt":
        return f"{base}{'USDT' if quote == 'USD' else quote}"
    if style == "dash":
        return f"{base}-{quote}"
    if style == "slash":
        return f"{base}/{quote}"
    raise ValueError(f"unknown symbol style: {style}")
