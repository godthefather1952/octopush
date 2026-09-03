"""Canonical symbol mapping.

``BTCUSDT``, ``BTC/USDT`` and ``btc-usdt`` are three spellings of one
instrument, and everything past the venue adapter speaks the canonical
``BASE-QUOTE`` form.

What this module deliberately does **not** do is treat two different quote
assets as one instrument. ``BTC-USD`` and ``BTC-USDT`` settle in different
assets: one in dollars, one in a token whose dollar value is a market price,
not a constant. They trade at different prices for reasons that have nothing
to do with bitcoin.

Earlier versions aliased USDT, USDC and BUSD to USD, which made a Binance
BTC/USDT quote directly comparable to a Coinbase BTC/USD quote. The residual
basis then appeared as a cross-venue bitcoin dislocation — a signal the
strategy would size into, taking on an unmodelled and unhedged stablecoin
position while believing it was market-neutral in bitcoin (TIDAL-C3). The
basis routinely exceeds the 4 bps detection threshold, so this was not a
corner case; it was the behaviour.

The rule now: same base and same quote may be compared; same base and a
different quote may not. Comparing across quotes is a real strategy that
needs a reference feed, a conversion leg and a depeg model. It is not
something a symbol parser gets to imply.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Quote assets recognised in concatenated venue symbols. Each is its own
#: settlement asset; none is an alias for another. Ordering here is
#: irrelevant — :func:`parse` matches longest-first so ``BTCUSDT`` cannot be
#: read as ``BTC`` + ``USD`` with a stray ``T``.
CANONICAL_QUOTES = ("USD", "USDT", "USDC", "BUSD", "EUR")

#: Venue spellings of a base asset. ``XBT`` is bitcoin under an older ticker
#: convention — a naming difference, not an economic one, and correct to alias.
#:
#: ``WBTC`` and ``WETH`` are a different matter and are left here unchanged
#: only because they are out of scope for TIDAL-C3, which is about quote
#: assets. They carry the *same class* of error on the base side: WBTC is a
#: custodial claim on bitcoin, not bitcoin, and it trades at its own price.
#: Nothing in the default configuration subscribes to them, so the defect is
#: latent rather than live. It should be resolved the same way this one was —
#: by making them distinct instruments — in a later batch.
_BASE_ALIASES = {
    "XBT": "BTC",
    "WBTC": "BTC",
    "WETH": "ETH",
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
    """Split a venue symbol into base and quote, preserving both.

    Concatenated forms are matched longest-quote-first, so ``BTCUSDT`` reads
    as ``BTC`` + ``USDT`` rather than ``BTCU`` + ``SDT`` or ``BTC`` + ``USD``
    with a stray character.
    """
    raw = symbol.strip().upper().replace("_", "-").replace("/", "-")
    if "-" in raw:
        base, _, quote = raw.partition("-")
    else:
        for quote_candidate in sorted(CANONICAL_QUOTES, key=len, reverse=True):
            if raw.endswith(quote_candidate) and len(raw) > len(quote_candidate):
                base, quote = raw[: -len(quote_candidate)], quote_candidate
                break
        else:
            raise UnknownSymbol(f"cannot parse symbol: {symbol!r}")
    base = _BASE_ALIASES.get(base, base)
    if not base or not quote:
        raise UnknownSymbol(f"cannot parse symbol: {symbol!r}")
    return Instrument(base=base, quote=quote)


def denormalize(canonical: str, style: str) -> str:
    """Render a canonical symbol in a venue's own spelling.

    This changes *punctuation only*. It may drop a separator or swap one for
    another; it may never change the base or the quote, because doing so
    changes which instrument is being named.

    There used to be a ``concat_usdt`` style that rendered ``BTC-USD`` as
    ``BTCUSDT`` — silently substituting a different settlement asset so that a
    USD-quoted canonical symbol would find a listing on a USDT venue. Whether a
    venue lists a given instrument is a venue-configuration question, answered
    by ``VenueConfig.symbols``. It is not a formatting question, and a
    formatter that answers it will always answer it by lying.
    """
    instrument = parse(canonical)
    base, quote = instrument.base, instrument.quote
    if style == "concat":
        return f"{base}{quote}"
    if style == "dash":
        return f"{base}-{quote}"
    if style == "slash":
        return f"{base}/{quote}"
    raise ValueError(f"unknown symbol style: {style}")
