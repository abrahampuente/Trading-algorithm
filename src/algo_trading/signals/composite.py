from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from algo_trading.indicators.price_series import PriceBar, validate_price_series
from algo_trading.signals.base import Signal, SignalGenerator
from algo_trading.signals.context import SignalContext
from algo_trading.signals.filters import FilterResult, SignalFilter


class FilteredSignal(BaseModel):
    """Señal base bloqueada por al menos un filtro, con todos los resultados."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal: Signal
    results: tuple[FilterResult, ...]

    @property
    def failed_filters(self) -> tuple[str, ...]:
        return tuple(result.name for result in self.results if not result.passed)


class CompositeSignalResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: tuple[Signal, ...]
    filtered: tuple[FilteredSignal, ...]


class CompositeSignalGenerator:
    """
    Señal base + filtros (AND).

    1. El generador base emite sus señales sobre la serie completa (es
       invariante a barras futuras).
    2. Para cada señal en `t`, cada filtro recibe el historial del símbolo
       hasta `t` y `context.until(t)`: ningún filtro ve datos posteriores.
    3. Se evalúan todos los filtros, también tras el primer rechazo, para
       registrar por qué se bloquea cada señal.
    4. Las señales aceptadas añaden a `factors` los valores de cada filtro con
       la clave `<filtro>.<valor>`; `strategy_id` puede sustituirse para
       distinguir la variante filtrada de la señal base.

    Sin filtros, emite exactamente las señales del generador base.
    """

    def __init__(
        self,
        base: SignalGenerator,
        filters: Sequence[SignalFilter] = (),
        strategy_id: str | None = None,
    ) -> None:
        names = [signal_filter.name for signal_filter in filters]

        if len(set(names)) != len(names):
            raise ValueError("Los filtros deben tener nombres distintos")

        self._base = base
        self._filters = tuple(filters)
        self._strategy_id = strategy_id or base.strategy_id

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    @property
    def filters(self) -> tuple[SignalFilter, ...]:
        return self._filters

    def generate(
        self,
        bars: Sequence[PriceBar],
        context: SignalContext | None = None,
    ) -> tuple[Signal, ...]:
        return self.run(bars, context).accepted

    def run(
        self,
        bars: Sequence[PriceBar],
        context: SignalContext | None = None,
    ) -> CompositeSignalResult:
        validate_price_series(bars)
        context = context or SignalContext()
        index_by_timestamp = {bar.timestamp: index for index, bar in enumerate(bars)}
        accepted: list[Signal] = []
        filtered: list[FilteredSignal] = []

        for signal in self._base.generate(bars, context):
            index = index_by_timestamp[signal.signal_timestamp]
            history = bars[: index + 1]
            view = context.until(signal.signal_timestamp)
            results = tuple(
                signal_filter.evaluate(signal, history, view)
                for signal_filter in self._filters
            )

            if all(result.passed for result in results):
                factors = dict(signal.factors)

                for result in results:
                    for key, value in result.values.items():
                        factors[f"{result.name}.{key}"] = value

                accepted.append(
                    signal.model_copy(
                        update={"strategy_id": self._strategy_id, "factors": factors}
                    )
                )
            else:
                filtered.append(FilteredSignal(signal=signal, results=results))

        return CompositeSignalResult(accepted=tuple(accepted), filtered=tuple(filtered))
