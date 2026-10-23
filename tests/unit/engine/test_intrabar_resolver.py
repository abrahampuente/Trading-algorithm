from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from algo_trading.data.market_bar import MarketBar
from algo_trading.engine import IntrabarResolution, IntrabarResolver
from algo_trading.execution import ExitReason
from algo_trading.portfolio.exit_levels import ExitLevels

SESSION = datetime(2024, 3, 4, tzinfo=UTC)
MARKET_OPEN = datetime(2024, 3, 4, 14, 30, tzinfo=UTC)
LEVELS = ExitLevels(
    entry_price=Decimal("100"),
    entry_atr=Decimal("5"),
    stop_loss=Decimal("95"),
    take_profit=Decimal("105"),
)
# Vela diaria que toca ambos niveles: Low 94 <= SL 95 y High 106 >= TP 105.
DAILY_OPEN = Decimal("100")
DAILY_HIGH = Decimal("106")
DAILY_LOW = Decimal("94")


def minute(
    index: int, low: str = "99", high: str = "101", open_: str = "100"
) -> MarketBar:
    return MarketBar(
        symbol="AAA",
        timestamp=MARKET_OPEN + timedelta(minutes=index),
        timeframe="1m",
        source="test",
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(open_),
        volume=Decimal("100"),
    )


def session_path(**touches: tuple[str, str]) -> list[MarketBar]:
    """390 minutos planos; `m<i>=(low, high)` sustituye el minuto i."""
    bars = [minute(index) for index in range(390)]

    for name, (low, high) in touches.items():
        index = int(name.removeprefix("m"))
        bars[index] = minute(index, low=low, high=high)

    return bars


class FakeSource:
    def __init__(self, bars: Sequence[MarketBar] = (), error: Exception | None = None):
        self.bars = list(bars)
        self.error = error
        self.calls: list[tuple[str, date]] = []

    def get_session_bars(self, symbol: str, session_date: date) -> Sequence[MarketBar]:
        self.calls.append((symbol, session_date))

        if self.error is not None:
            raise self.error

        return self.bars


def resolve(
    resolver: IntrabarResolver,
    open_price: Decimal = DAILY_OPEN,
    high: Decimal = DAILY_HIGH,
    low: Decimal = DAILY_LOW,
):  # type: ignore[no-untyped-def]
    return resolver.resolve("AAA", SESSION, open_price, high, low, LEVELS)


# --- Sin conflicto: nunca se descargan datos intradía -----------------------


def test_bar_without_levels_does_not_download() -> None:
    source = FakeSource(session_path())

    decision, record = resolve(
        IntrabarResolver(source), high=Decimal("104"), low=Decimal("96")
    )

    assert decision is None
    assert record is None
    assert source.calls == []


@pytest.mark.parametrize(
    ("high", "low", "reason", "price"),
    [
        ("104", "94", ExitReason.STOP_LOSS, "95"),
        ("106", "96", ExitReason.TAKE_PROFIT, "105"),
    ],
)
def test_single_level_executes_without_download(
    high: str, low: str, reason: ExitReason, price: str
) -> None:
    source = FakeSource(session_path())

    decision, record = resolve(
        IntrabarResolver(source), high=Decimal(high), low=Decimal(low)
    )

    assert decision is not None
    assert (decision.reason, decision.theoretical_price) == (reason, Decimal(price))
    assert record is None
    assert source.calls == []


# --- Ambos niveles: trayectoria de 1 minuto ---------------------------------


def test_stop_loss_at_minute_10_before_take_profit_at_minute_45() -> None:
    source = FakeSource(session_path(m10=("94", "100"), m45=("100", "106")))

    decision, record = resolve(IntrabarResolver(source))

    assert source.calls == [("AAA", date(2024, 3, 4))]
    assert decision is not None
    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.theoretical_price == Decimal("95")
    assert record is not None
    assert record.resolution is IntrabarResolution.INTRADAY_PATH
    assert record.first_touch_timestamp == MARKET_OPEN + timedelta(minutes=10)
    assert record.intraday_bars == 390


def test_take_profit_at_minute_10_before_stop_loss_at_minute_45() -> None:
    source = FakeSource(session_path(m10=("100", "106"), m45=("94", "100")))

    decision, record = resolve(IntrabarResolver(source))

    assert decision is not None
    assert decision.reason is ExitReason.TAKE_PROFIT
    assert decision.theoretical_price == Decimal("105")
    assert record is not None
    assert record.resolution is IntrabarResolution.INTRADAY_PATH
    assert record.exit_reason is ExitReason.TAKE_PROFIT
    assert record.first_touch_timestamp == MARKET_OPEN + timedelta(minutes=10)


def test_both_levels_in_the_same_minute_resolve_as_stop_loss() -> None:
    source = FakeSource(session_path(m10=("94", "106")))

    decision, record = resolve(IntrabarResolver(source))

    assert decision is not None
    assert decision.reason is ExitReason.STOP_LOSS
    assert record is not None
    assert record.resolution is IntrabarResolution.SAME_MINUTE


def test_stop_loss_minute_opening_below_level_executes_at_its_open() -> None:
    bars = session_path(m45=("100", "106"))
    bars[10] = minute(10, low="93", high="94", open_="94")

    decision, _ = resolve(IntrabarResolver(FakeSource(bars)), low=Decimal("93"))

    assert decision is not None
    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.theoretical_price == Decimal("94")


def test_take_profit_without_visible_stop_loss_falls_back() -> None:
    """Feed parcial: el SL diario no aparece en los minutos; orden desconocido."""
    source = FakeSource(session_path(m10=("100", "106")))

    decision, record = resolve(IntrabarResolver(source))

    assert decision is not None
    assert decision.reason is ExitReason.STOP_LOSS
    assert record is not None
    assert record.resolution is IntrabarResolution.FALLBACK_UNRESOLVED
    assert record.first_touch_timestamp == MARKET_OPEN + timedelta(minutes=10)


def test_path_without_levels_falls_back() -> None:
    decision, record = resolve(IntrabarResolver(FakeSource(session_path())))

    assert decision is not None
    assert decision.reason is ExitReason.STOP_LOSS
    assert record is not None
    assert record.resolution is IntrabarResolution.FALLBACK_UNRESOLVED
    assert record.first_touch_timestamp is None


def test_unsorted_bars_and_other_sessions_are_handled() -> None:
    bars = session_path(m10=("100", "106"), m45=("94", "100"))
    previous_day = minute(0, low="90", high="91", open_="90").model_copy(
        update={"timestamp": MARKET_OPEN - timedelta(days=1)}
    )

    decision, _ = resolve(
        IntrabarResolver(FakeSource([previous_day, *reversed(bars)])),
        low=Decimal("90"),
    )

    assert decision is not None
    assert decision.reason is ExitReason.TAKE_PROFIT


# --- Gaps de apertura -------------------------------------------------------


@pytest.mark.parametrize(
    ("open_price", "reason", "price"),
    [
        ("93", ExitReason.STOP_LOSS, "93"),
        ("107", ExitReason.TAKE_PROFIT, "105"),
    ],
)
def test_opening_gap_decides_without_download(
    open_price: str, reason: ExitReason, price: str
) -> None:
    source = FakeSource(session_path())

    decision, record = resolve(
        IntrabarResolver(source),
        open_price=Decimal(open_price),
        high=Decimal("108"),
        low=Decimal("92"),
    )

    assert decision is not None
    assert (decision.reason, decision.theoretical_price) == (reason, Decimal(price))
    assert record is not None
    assert record.resolution is IntrabarResolution.OPENING_GAP
    assert source.calls == []


# --- Fallback de la v1 ------------------------------------------------------


def test_source_error_falls_back_to_stop_loss_and_is_cached() -> None:
    source = FakeSource(error=TimeoutError("Alpaca no responde"))
    resolver = IntrabarResolver(source)

    decision, record = resolve(resolver)
    resolve(resolver)

    assert decision is not None
    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.theoretical_price == Decimal("95")
    assert record is not None
    assert record.resolution is IntrabarResolution.FALLBACK_SOURCE_ERROR
    assert record.error == "TimeoutError: Alpaca no responde"
    assert len(source.calls) == 1


@pytest.mark.parametrize(
    ("resolver", "resolution"),
    [
        (IntrabarResolver(FakeSource([])), IntrabarResolution.FALLBACK_NO_DATA),
        (IntrabarResolver(None), IntrabarResolution.FALLBACK_NO_SOURCE),
    ],
)
def test_missing_intraday_data_falls_back(
    resolver: IntrabarResolver, resolution: IntrabarResolution
) -> None:
    decision, record = resolve(resolver)

    assert decision is not None
    assert decision.reason is ExitReason.STOP_LOSS
    assert record is not None
    assert record.resolution is resolution
    assert record.resolution.is_fallback


def test_bars_outside_daily_range_fall_back_as_inconsistent() -> None:
    """Minutos sin ajustar por un split 4:1: escala distinta a la diaria."""
    unadjusted = [
        bar.model_copy(
            update={
                "open": bar.open * 4,
                "high": bar.high * 4,
                "low": bar.low * 4,
                "close": bar.close * 4,
            }
        )
        for bar in session_path(m10=("100", "106"), m45=("94", "100"))
    ]

    decision, record = resolve(IntrabarResolver(FakeSource(unadjusted)))

    assert decision is not None
    assert decision.reason is ExitReason.STOP_LOSS
    assert record is not None
    assert record.resolution is IntrabarResolution.FALLBACK_INCONSISTENT


def test_each_session_is_downloaded_once() -> None:
    source = FakeSource(session_path(m10=("100", "106"), m45=("94", "100")))
    resolver = IntrabarResolver(source)

    first = resolve(resolver)
    second = resolve(resolver)

    assert first == second
    assert len(source.calls) == 1
    assert resolver.requested_sessions == 1


def test_negative_tolerance_is_rejected() -> None:
    with pytest.raises(ValueError, match="price_tolerance"):
        IntrabarResolver(None, price_tolerance=Decimal("-0.01"))


class CredentialsRejected(Exception):
    pass


def test_fatal_source_errors_are_raised_instead_of_falling_back() -> None:
    source = FakeSource(error=CredentialsRejected("401"))
    resolver = IntrabarResolver(source, fatal_errors=(CredentialsRejected,))

    with pytest.raises(CredentialsRejected):
        resolve(resolver)

    assert resolver.source is source
