"""Reconciliation policy — what a discrepancy means, in one place.

WHY THIS MODULE IS ALMOST EMPTY
===============================
Policy is where the dangerous decisions live: which disagreements stop trading,
which may be corrected without a human, which evidence is good enough to
overwrite the ledger. Those decisions need measurement, and no measurement has
been taken.

So the module exists, and it is conservative to the point of being boring:
:func:`is_resolvable_automatically` returns False for everything. That is not a
placeholder to be filled in casually — a reconciler that can silently correct
its own books is a reconciler whose books cannot be trusted, and every entry
that ever becomes True here should have to justify itself.

WHAT IS DELIBERATELY NOT HERE
=============================
Nothing in this module is wired into the kill switch, the risk gates, the
orchestrator's protection path, or any account mutation. It changes no
threshold, and it does not touch ``CASH_TOLERANCE`` or ``QTY_TOLERANCE``. The
existing reconciliation algorithm decides what is critical, exactly as before;
these helpers read that decision rather than making a second one.
"""

from __future__ import annotations

from core.models.ops import Severity
from core.models.reconciliation import (
    DiscrepancyEntityType,
    ReconciliationDiscrepancy,
    ReconciliationSourceKind,
    ResolutionAction,
    SourceAuthority,
)

#: Entity types whose truth only an external venue can settle. An internal
#: source disagreeing with another internal source about an order cannot
#: establish which of them is right — both were written by this platform.
NEEDS_EXTERNAL_EVIDENCE: frozenset[DiscrepancyEntityType] = frozenset(
    {
        DiscrepancyEntityType.ORDER,
        DiscrepancyEntityType.FILL,
        DiscrepancyEntityType.POSITION,
    }
)

#: Sources whose word is external evidence rather than internal opinion.
AUTHORITATIVE_KINDS: frozenset[ReconciliationSourceKind] = frozenset(
    {ReconciliationSourceKind.VENUE, ReconciliationSourceKind.OPERATOR}
)


def is_blocking(discrepancy: ReconciliationDiscrepancy) -> bool:
    """Whether this disagreement should stop new trading.

    Reads the severity the existing algorithm already assigned rather than
    re-deciding it. A CRITICAL mismatch has meant "suspend new trading" since
    Phase 1, and that meaning is unchanged.

    Construction-phase note: nothing calls this. The orchestrator's protection
    path still reads ``marin.last_result.ok`` exactly as before.
    """
    return discrepancy.is_open and discrepancy.severity is Severity.CRITICAL


def requires_authoritative_source(discrepancy: ReconciliationDiscrepancy) -> bool:
    """Whether settling this needs evidence from outside the platform.

    Two internal views disagreeing about whether an order filled cannot be
    settled by consulting either of them again. Cash and P&L are different:
    they are arithmetic over a fill log the platform owns, so a recomputation
    can genuinely establish which side is wrong.
    """
    return discrepancy.entity_type in NEEDS_EXTERNAL_EVIDENCE


def is_authoritative(kind: ReconciliationSourceKind) -> bool:
    """Whether a source kind counts as external evidence."""
    return kind in AUTHORITATIVE_KINDS


def is_resolvable_automatically(discrepancy: ReconciliationDiscrepancy) -> bool:
    """Whether the platform may settle this without a human.

    **Always False.** Deliberately, and not as an oversight.

    Automatic resolution means a component deciding, unsupervised, that one
    account of reality is wrong and editing it to match another. Getting that
    right requires knowing which source is authoritative for which question,
    how stale each capture was, and whether the disagreement is a real
    divergence or two views taken a moment apart. None of that has been
    established, and a wrong answer silently corrupts the books that every
    later check depends on.

    A later pass may make specific, narrow cases return True. Each one should
    have to say what evidence justifies it.
    """
    return False


def suggested_action(discrepancy: ReconciliationDiscrepancy) -> ResolutionAction:
    """The action a human would most likely take. A suggestion, never applied.

    Constructing a resolution with this action does nothing; something has to
    carry it out, and in this build only an explicit caller can.
    """
    if not discrepancy.is_open:
        return ResolutionAction.NO_ACTION
    if discrepancy.entity_type is DiscrepancyEntityType.ORDER:
        # An order whose state nobody knows is settled by asking whoever does.
        return ResolutionAction.REQUERY
    if discrepancy.entity_type is DiscrepancyEntityType.FILL:
        # A fill present in one account and absent from another is either a
        # delivery failure or a bug; the event log is the tiebreaker.
        return ResolutionAction.REBUILD_FROM_EVENTS
    if discrepancy.severity is Severity.CRITICAL:
        return ResolutionAction.ESCALATE_OPERATOR
    return ResolutionAction.REFRESH


def authority_of(kind: ReconciliationSourceKind) -> SourceAuthority:
    """The structural weight a source kind carries."""
    if kind in AUTHORITATIVE_KINDS:
        return SourceAuthority.AUTHORITATIVE
    if kind is ReconciliationSourceKind.RECORDED:
        return SourceAuthority.DERIVED
    return SourceAuthority.INTERNAL


__all__ = [
    "AUTHORITATIVE_KINDS",
    "NEEDS_EXTERNAL_EVIDENCE",
    "authority_of",
    "is_authoritative",
    "is_blocking",
    "is_resolvable_automatically",
    "requires_authoritative_source",
    "suggested_action",
]
