"""
Métricas operativas del paper trading (Hito 4).

- Slippage real: diferencia entre el precio de referencia (el teórico del
  backtest) y el precio medio ejecutado, en puntos básicos y en dinero.
  Positivo = adverso (comprar más caro o vender más barato).

  `theoretical_price` de una entrada `OPG` es el cierre de la señal, porque
  la apertura de `t+1` no se conoce al enviarla. El precio teórico del
  backtest es esa apertura: pásela en `reference_prices` una vez ingerida la
  barra diaria para medir el slippage comparable con el backtest.
- Latencia de la API: estadísticos de las peticiones HTTP (media, p50, p95,
  máximo, errores), acumulables durante la vida del daemon.
- Tasa de acierto de órdenes: de las órdenes cerradas, cuántas se ejecutaron
  completas, parcialmente o no se ejecutaron (OPG fuera de límite = escudo
  anti-gap), y cuántas rechazó el broker.
"""

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.execution.alpaca_broker import RequestTiming
from algo_trading.execution.orders import OrderSide, OrderStatus
from algo_trading.persistence.repositories.broker_order_repository import (
    BrokerOrderRecord,
)

BASIS_POINTS = Decimal("10000")
BPS_QUANTUM = Decimal("0.01")
MONEY_QUANTUM = Decimal("0.00000001")


def slippage_bps(side: OrderSide, reference: Decimal, executed: Decimal) -> Decimal:
    """`signo · (ejecutado - referencia) / referencia · 10.000`; signo +1 compra."""
    if reference <= 0:
        raise ValueError("El precio de referencia debe ser positivo")

    sign = 1 if side is OrderSide.BUY else -1

    with localcontext(prec=28, rounding=ROUND_HALF_EVEN):
        return sign * (executed - reference) / reference * BASIS_POINTS


class OrderSlippage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    reference_price: Decimal
    executed_price: Decimal
    slippage_bps: Decimal
    slippage_cost: Decimal


class SlippageSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    orders: int = Field(ge=0)
    mean_bps: Decimal | None
    weighted_bps: Decimal | None
    median_bps: Decimal | None
    worst_bps: Decimal | None
    total_cost: Decimal


def order_slippage(
    records: Iterable[BrokerOrderRecord],
    reference_prices: Mapping[str, Decimal] | None = None,
) -> tuple[OrderSlippage, ...]:
    """
    Slippage de las órdenes con ejecución. La referencia es
    `reference_prices[client_order_id]` o, si no está, el precio teórico
    guardado; las órdenes sin referencia ni precio medio se omiten.
    """
    references = reference_prices or {}
    result: list[OrderSlippage] = []

    for record in records:
        state = record.state
        reference = references.get(state.client_order_id, record.theoretical_price)

        if (
            reference is None
            or state.filled_quantity <= 0
            or state.filled_average_price is None
        ):
            continue

        bps = slippage_bps(state.side, reference, state.filled_average_price)

        with localcontext(prec=28, rounding=ROUND_HALF_EVEN):
            sign = 1 if state.side is OrderSide.BUY else -1
            cost = (
                sign * (state.filled_average_price - reference) * state.filled_quantity
            )

        result.append(
            OrderSlippage(
                client_order_id=state.client_order_id,
                symbol=state.symbol,
                side=state.side,
                quantity=state.filled_quantity,
                reference_price=reference,
                executed_price=state.filled_average_price,
                slippage_bps=bps.quantize(BPS_QUANTUM, rounding=ROUND_HALF_EVEN),
                slippage_cost=cost.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN),
            )
        )

    return tuple(result)


def summarize_slippage(items: Sequence[OrderSlippage]) -> SlippageSummary:
    if not items:
        return SlippageSummary(
            orders=0,
            mean_bps=None,
            weighted_bps=None,
            median_bps=None,
            worst_bps=None,
            total_cost=Decimal("0"),
        )

    values = sorted(item.slippage_bps for item in items)
    middle = len(values) // 2

    with localcontext(prec=28, rounding=ROUND_HALF_EVEN):
        mean = sum(values, Decimal("0")) / len(values)
        median = (
            values[middle]
            if len(values) % 2
            else (values[middle - 1] + values[middle]) / 2
        )
        notional = sum(
            (item.reference_price * item.quantity for item in items), Decimal("0")
        )
        total_cost = sum((item.slippage_cost for item in items), Decimal("0"))
        weighted = total_cost / notional * BASIS_POINTS

    return SlippageSummary(
        orders=len(items),
        mean_bps=mean.quantize(BPS_QUANTUM, rounding=ROUND_HALF_EVEN),
        weighted_bps=weighted.quantize(BPS_QUANTUM, rounding=ROUND_HALF_EVEN),
        median_bps=median.quantize(BPS_QUANTUM, rounding=ROUND_HALF_EVEN),
        worst_bps=values[-1],
        total_cost=total_cost,
    )


class LatencyStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requests: int = Field(ge=0)
    errors: int = Field(ge=0)
    mean_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None

    @property
    def error_rate(self) -> float | None:
        return self.errors / self.requests if self.requests else None


def percentile(values: Sequence[float], quantile: float) -> float | None:
    """Percentil por rango más cercano sobre los valores ordenados."""
    if not values:
        return None

    if not 0 < quantile <= 1:
        raise ValueError("quantile debe estar en (0, 1]")

    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def latency_stats(timings: Sequence[RequestTiming]) -> LatencyStats:
    """Errores = sin respuesta (red) o respuesta 429 / 5xx."""
    answered = [t.latency_ms for t in timings if t.status is not None]
    errors = sum(
        1 for t in timings if t.status is None or t.status == 429 or t.status >= 500
    )
    return LatencyStats(
        requests=len(timings),
        errors=errors,
        mean_ms=sum(answered) / len(answered) if answered else None,
        p50_ms=percentile(answered, 0.5),
        p95_ms=percentile(answered, 0.95),
        max_ms=max(answered) if answered else None,
    )


class LatencyTracker:
    """
    Acumula las peticiones de un `request_log` que crece (p. ej. el de
    `AlpacaPaperBroker`) sin contar dos veces las ya vistas.
    """

    def __init__(self) -> None:
        self._timings: list[RequestTiming] = []
        self._consumed = 0

    def update(self, request_log: Sequence[RequestTiming]) -> int:
        new = request_log[self._consumed :]
        self._timings.extend(new)
        self._consumed = len(request_log)
        return len(new)

    @property
    def timings(self) -> tuple[RequestTiming, ...]:
        return tuple(self._timings)

    def stats(self) -> LatencyStats:
        return latency_stats(self._timings)


class OrderOutcomeStats(BaseModel):
    """
    `fill_rate` = ejecutadas completas / cerradas; `any_fill_rate` incluye las
    cerradas con ejecución parcial. Las sustituidas (REPLACED) no cuentan.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = Field(ge=0)
    open: int = Field(ge=0)
    filled: int = Field(ge=0)
    partially_filled: int = Field(ge=0)
    unfilled: int = Field(ge=0)
    rejected: int = Field(ge=0)
    fill_rate: float | None
    any_fill_rate: float | None


def order_outcomes(records: Iterable[BrokerOrderRecord]) -> OrderOutcomeStats:
    states = [record.state for record in records]
    closed = [
        s
        for s in states
        if s.status.is_terminal and s.status is not OrderStatus.REPLACED
    ]
    filled = sum(1 for s in closed if s.status is OrderStatus.FILLED)
    rejected = sum(1 for s in closed if s.status is OrderStatus.REJECTED)
    partial = sum(
        1
        for s in closed
        if s.status is not OrderStatus.FILLED and s.filled_quantity > 0
    )
    unfilled = len(closed) - filled - rejected - partial
    return OrderOutcomeStats(
        total=len(states),
        open=sum(1 for s in states if not s.status.is_terminal),
        filled=filled,
        partially_filled=partial,
        unfilled=unfilled,
        rejected=rejected,
        fill_rate=filled / len(closed) if closed else None,
        any_fill_rate=(filled + partial) / len(closed) if closed else None,
    )


class OperationalMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    computed_at: datetime
    slippage: SlippageSummary
    latency: LatencyStats
    orders: OrderOutcomeStats


def operational_metrics(
    records: Sequence[BrokerOrderRecord],
    timings: Sequence[RequestTiming],
    reference_prices: Mapping[str, Decimal] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> OperationalMetrics:
    return OperationalMetrics(
        computed_at=(clock or (lambda: datetime.now(UTC)))(),
        slippage=summarize_slippage(order_slippage(records, reference_prices)),
        latency=latency_stats(timings),
        orders=order_outcomes(records),
    )
