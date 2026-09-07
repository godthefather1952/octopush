"""The information-source seam — a shape, with nothing networked behind it.

WHAT THIS FILE IS FOR
=====================
Today LUMEN's only information input is ``Lumen.headlines``: an in-memory list
a caller appends to. That is fine, and it stays. But it means every future news
feed, exchange announcement stream or research source would have to learn
LUMEN's internal list shape, and LUMEN would accumulate one bespoke ingestion
path per source.

:class:`IntelligenceSource` names the alternative:

    source -> IntelligenceEvidence -> IntelligenceEvidenceBundle -> LUMEN

so that adding a feed is adding an adapter, not editing the agent.

WHAT IS DELIBERATELY ABSENT
===========================
**No network source is implemented, and none may be added here.** No Reuters,
Bloomberg, X, Reddit, Google News, RSS, CoinDesk or exchange-announcement
client. No HTTP, no credentials, no API keys, no rate limiter, no retry.

That absence is the design. A source seam carrying a half-written credential
path is not a seam, it is a liability — and every real information feed brings
a trust question ("do we believe this publisher?") that this phase is not
allowed to answer and must not appear to have answered.

LUMEN DOES NOT DEPEND ON THIS YET
=================================
``Lumen.evaluate`` and ``Lumen._context`` read ``self.headlines`` exactly as
they always have. :class:`LocalHeadlineSource` wraps that same list so the
existing input is expressible in the new vocabulary, and nothing in LUMEN calls
it. Migrating the agent onto the interface is a later, validated step.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from core.models.common import Millis
from core.models.intelligence import (
    EvidenceFreshness,
    IntelligenceEvidence,
    IntelligenceSourceHealth,
    IntelligenceSourceKind,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agents.lumen.agent import NewsItem


def evidence_from_news_item(
    item: NewsItem | Any, now_ms: Millis, *, symbol: str = ""
) -> IntelligenceEvidence:
    """Adapt a ``NewsItem`` into neutral evidence. Pure, and a copy only.

    ``NewsItem`` stays exactly where it is, in ``agents/lumen/agent.py``, and
    every current caller keeps working. This is the bridge to the neutral
    vocabulary, chosen over moving the class because compatibility wins in a
    construction pass: one canonical representation is desirable eventually,
    and breaking every existing producer to get there today is not.

    ``freshness`` is left UNKNOWN rather than computed from ``now_ms``.
    ``Lumen._context`` already applies the one-hour window that decides what a
    provider actually sees, and a second freshness rule that could disagree
    with it would be worse than none.
    """
    return IntelligenceEvidence(
        kind=IntelligenceSourceKind.HEADLINE,
        source=getattr(item, "source", ""),
        title=getattr(item, "headline", ""),
        published_at=getattr(item, "published_ms", None),
        captured_at=now_ms,
        symbol=symbol,
        body_excerpt=str(getattr(item, "body", ""))[:1000],
        freshness=EvidenceFreshness.UNKNOWN,
    )


class IntelligenceSource(ABC):
    """What a future information adapter must provide.

    **Nothing external implements this.** It exists so the obligations of an
    information feed are written down before anyone writes one, and so a
    reader can see that the platform's current single source already fits the
    shape.

    :meth:`capture` takes ``now_ms`` for the same reason every other framework
    surface does: a logical instant supplied by the caller is reconstructible
    under replay, and a clock read taken inside the call is not.

    A real implementation would additionally have to answer questions this
    build has never had to ask — how the feed authenticates, what happens when
    it rate-limits, whether the publisher can be believed, and how the same
    story arriving twice is handled. None of those is answered here, and none
    should be guessed at.
    """

    #: Identifies the source in provenance records.
    name: str = "abstract"
    kind: IntelligenceSourceKind = IntelligenceSourceKind.HEADLINE
    #: True when capturing reaches outside the process. Every source in this
    #: build is False, and the composition root is what keeps it that way.
    is_external: bool = False

    @abstractmethod
    def capture(self, symbol: str, now_ms: Millis) -> list[IntelligenceEvidence]:
        """Return the evidence this source currently has for ``symbol``.

        Returns an empty list rather than raising when it has nothing.
        Absence of news is a normal state and not an error, and a source that
        raised on quiet would make quiet look like a fault.
        """

    def health(self, now_ms: Millis) -> IntelligenceSourceHealth:
        """How the last capture went. Reporting only; no retry, no probe."""
        return IntelligenceSourceHealth(
            source=self.name,
            kind=self.kind,
            available=True,
            complete=True,
            captured_at=now_ms,
        )


class LocalHeadlineSource(IntelligenceSource):
    """The in-memory headline list, expressed as a source.

    Wraps the same ``list`` that ``Lumen.headlines`` is, so a caller can obtain
    the current input as :class:`IntelligenceEvidence` without LUMEN changing.

    **Purely additive.** ``Lumen._context`` still reads ``self.headlines``
    directly, still takes the last ten, and still filters to the last hour —
    this class applies no window of its own, because a second filter that
    disagreed with the agent's would produce a provenance record describing
    information the provider never saw.
    """

    name = "local-headlines"
    kind = IntelligenceSourceKind.HEADLINE
    is_external = False

    def __init__(self, headlines: list, *, symbol: str = "") -> None:
        #: The very list LUMEN holds, not a copy: a copy would drift the moment
        #: a headline was added.
        self._headlines = headlines
        self._symbol = symbol

    def capture(self, symbol: str, now_ms: Millis) -> list[IntelligenceEvidence]:
        """Adapt every held headline into evidence, in order.

        No filtering and no deduplication. ``freshness`` is left ``UNKNOWN``
        rather than computed from ``now_ms``: classifying it here would create
        a second freshness rule beside LUMEN's existing one-hour window, and
        two rules that can disagree are worse than one.
        """
        return [
            evidence_from_news_item(item, now_ms, symbol=symbol or self._symbol)
            for item in self._headlines
        ]

    def health(self, now_ms: Millis) -> IntelligenceSourceHealth:
        return IntelligenceSourceHealth(
            source=self.name,
            kind=self.kind,
            available=True,
            complete=True,
            captured_at=now_ms,
            last_success_at=now_ms,
            items_captured=len(self._headlines),
        )


__all__ = [
    "IntelligenceSource",
    "LocalHeadlineSource",
    "evidence_from_news_item",
]
