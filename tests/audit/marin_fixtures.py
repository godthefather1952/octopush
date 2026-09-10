"""Deterministic instruments for the Phase 7 MARIN validation audit.

Audit-only. Nothing here is imported by production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from agents.marin import Marin
from agents.marin.source import ReconciliationSource, SourceCapture
from core.models.common import Millis, OrderType, Side, TimeInForce
from core.models.execution import ExecutionCommandResult, FillEvent, OrderStatus
from core.models.reconciliation import (
    AccountSnapshot,
    ReconciliationSourceKind,
    SourceAuthority,
    SourceHealth,
    VenueTruthSnapshot,
)
from execution.oms import OrderManager
from execution.paper import PaperAccount

T0: Millis = 1_700_100_000_000


def authority_for(kind: ReconciliationSourceKind) -> SourceAuthority:
    if kind in (
        ReconciliationSourceKind.VENUE,
        ReconciliationSourceKind.OPERATOR,
    ):
        return SourceAuthority.AUTHORITATIVE
    if kind is ReconciliationSourceKind.RECORDED:
        return SourceAuthority.DERIVED
    return SourceAuthority.INTERNAL


class StaticSource(ReconciliationSource):
    """One deterministic source whose capture outcome is fully controlled."""

    def __init__(
        self,
        *,
        kind: ReconciliationSourceKind,
        name: str,
        factory: Callable[[Millis], object] | None = None,
        available: bool = True,
        complete: bool = True,
        explode: bool = False,
    ) -> None:
        self._kind = kind
        self._name = name
        self._factory = factory
        self._available = available
        self._complete = complete
        self._explode = explode

    @property
    def kind(self) -> ReconciliationSourceKind:
        return self._kind

    @property
    def name(self) -> str:
        return self._name

    @property
    def authority(self) -> SourceAuthority:
        return authority_for(self.kind)

    def capture(self, now_ms: Millis) -> SourceCapture:
        if self._explode:
            raise RuntimeError(f"{self.name} audit-source failure")
        health = SourceHealth(
            kind=self.kind,
            name=self.name,
            authority=self.authority,
            available=self._available,
            complete=self._complete,
            captured_at=now_ms,
            detail="" if self._available else "audit source unavailable",
        )
        snapshot = (
            self._factory(now_ms)
            if self._available and self._factory is not None
            else None
        )
        return SourceCapture(health=health, snapshot=snapshot)


def venue_source(
    venue: str,
    *,
    available: bool = True,
    complete: bool = True,
    explode: bool = False,
) -> StaticSource:
    return StaticSource(
        kind=ReconciliationSourceKind.VENUE,
        name=f"venue-{venue}",
        factory=lambda now: VenueTruthSnapshot(
            created_at=now,
            venue=venue,
            complete=complete,
        ),
        available=available,
        complete=complete,
        explode=explode,
    )


def empty_execution_source(*, available: bool = True) -> StaticSource:
    from core.models.execution import ExecutionSnapshot

    return StaticSource(
        kind=ReconciliationSourceKind.EXECUTION,
        name="execution",
        factory=lambda now: ExecutionSnapshot(created_at=now),
        available=available,
        complete=available,
    )


def empty_account_source(*, available: bool = True) -> StaticSource:
    return StaticSource(
        kind=ReconciliationSourceKind.ACCOUNT,
        name="account",
        factory=lambda now: AccountSnapshot(
            created_at=now,
            initial_balance=100_000.0,
            cash=100_000.0,
            equity=100_000.0,
            peak_equity=100_000.0,
        ),
        available=available,
        complete=available,
    )


def build_marin(*, bus, clock, health, attach_local: bool = False) -> Marin:
    oms = OrderManager(clock=clock)
    account = PaperAccount(clock=clock, initial_balance=100_000.0)
    marin = Marin(bus=bus, clock=clock, health=health, oms=oms, account=account)
    if attach_local:
        marin.attach_sources(
            execution=empty_execution_source(),
            account=empty_account_source(),
        )
    return marin


def matched_trade(
    marin: Marin,
    *,
    quantity: float = 1.0,
    price: float = 100.0,
    fee: float = 0.5,
    side: Side = Side.BUY,
    now_ms: Millis = T0,
):
    order = marin.oms.create(
        venue="VENUE_A",
        symbol="BTC-USD",
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        expected_price=price,
        now_ms=now_ms,
    )
    for status in (
        OrderStatus.SUBMITTING,
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.OPEN,
    ):
        marin.oms.transition(order.client_order_id, status, now_ms=now_ms)
    fill = FillEvent(
        created_at=now_ms,
        client_order_id=order.client_order_id,
        venue=order.venue,
        symbol=order.symbol,
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
    )
    marin.oms.apply_fill(fill, now_ms=now_ms)
    marin.account.apply_fill(fill)
    return order, fill


def make_unknown(marin: Marin, *, now_ms: Millis = T0):
    order = marin.oms.create(
        venue="VENUE_A",
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=1.0,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        expected_price=100.0,
        limit_price=99.0,
        now_ms=now_ms,
    )
    marin.oms.transition(order.client_order_id, OrderStatus.SUBMITTING, now_ms=now_ms)
    marin.oms.mark_unknown(order.client_order_id, now_ms=now_ms)
    return order


@dataclass
class ResolutionVeska:
    """Tiny VESKA resolution seam used to audit MARIN authority policy."""

    accepted: bool = True
    explode: bool = False
    calls: list[tuple[str, OrderStatus, Millis]] | None = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    async def resolve_unknown(
        self,
        client_order_id: str,
        authoritative_status: OrderStatus,
        now_ms: Millis,
    ) -> ExecutionCommandResult:
        assert self.calls is not None
        self.calls.append((client_order_id, authoritative_status, now_ms))
        if self.explode:
            raise RuntimeError("audit veska resolution failure")
        return ExecutionCommandResult(
            accepted=self.accepted,
            client_order_id=client_order_id,
            status=authoritative_status if self.accepted else OrderStatus.UNKNOWN,
            reason="" if self.accepted else "audit rejection",
            at_ms=now_ms,
        )
