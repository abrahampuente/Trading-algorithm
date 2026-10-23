from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algo_trading.data.market_calendar import (
    SessionCoverageError,
    check_session_coverage,
)
from algo_trading.domain.corporate_actions.dto import CorporateDividend
from algo_trading.execution.costs import CostModel
from algo_trading.execution.exit_rules import (
    ExitDecision,
    ExitReason,
    evaluate_bar_exit,
    evaluate_time_exit,
)
from algo_trading.execution.gap_filter import is_gap_filtered
from algo_trading.execution.trade import OpenTrade, Trade, close_trade
from algo_trading.indicators.price_series import PriceBar
from algo_trading.portfolio.allocator import (
    EntryCandidate,
    PortfolioAllocator,
    RejectedCandidate,
)
from algo_trading.portfolio.portfolio_state import PortfolioState, Position
from algo_trading.portfolio.position_sizing import (
    MONEY_PRECISION,
    MONEY_ROUNDING,
    PositionSizer,
    RiskParameters,
)
from algo_trading.strategy.long_signal import (
    LongSignal,
    LongSignalGenerator,
    SignalParameters,
)


class ExecutionBar(PriceBar, Protocol):
    """Barra diaria con apertura, necesaria para simular la ejecución."""

    @property
    def open(self) -> Decimal: ...


class BacktestParameters(BaseModel):
    """Parámetros completos de un backtest; se registran junto al resultado."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_cash: Decimal = Field(gt=0)
    cost_profile: str | None = Field(default=None, max_length=32)
    signal: SignalParameters = SignalParameters()
    risk: RiskParameters = RiskParameters(commission_rate=Decimal("0.0005"))
    costs: CostModel = CostModel()
    gap_threshold_atr: Decimal = Field(default=Decimal("0.5"), ge=0)
    max_holding_sessions: int = Field(default=20, ge=1)

    @model_validator(mode="after")
    def validate_commission_consistency(self) -> Self:
        if self.risk.commission_rate != self.costs.commission_rate:
            raise ValueError(
                "risk.commission_rate debe coincidir con costs.commission_rate"
            )

        return self


class GapFilteredEntry(BaseModel):
    """Entrada cancelada por el escudo anti-gap (`GAP_FILTERED`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = "GAP_FILTERED"
    signal: LongSignal
    session_timestamp: datetime
    entry_open: Decimal
    gap: Decimal
    max_allowed_gap: Decimal


class EquityPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    open_positions: int = Field(ge=0)


class DividendPayment(BaseModel):
    """Dividendo acreditado en cash a una posición abierta."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    ex_date: date
    payment_timestamp: datetime
    amount_per_share: Decimal
    quantity: int = Field(ge=1)
    total: Decimal


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    parameters: BacktestParameters
    trades: tuple[Trade, ...]
    gap_filtered: tuple[GapFilteredEntry, ...]
    rejected: tuple[RejectedCandidate, ...]
    equity_curve: tuple[EquityPoint, ...]
    final_cash: Decimal
    dividends: tuple[DividendPayment, ...] = ()
    uses_trading_calendar: bool = False


class BacktestEngine:
    """
    Simulador diario de la estrategia Long sobre varios símbolos.

    En cada sesión `D`, en este orden:

    0. Dividendos: las posiciones abiertas antes de `D` cobran en cash los
       dividendos con fecha ex-dividendo posterior a la sesión anterior y
       hasta `D` (normalmente, ex_date = `D`).
    1. Apertura: salidas temporales de posiciones con `max_holding_sessions`
       sesiones cumplidas.
    2. Apertura: entradas de las señales emitidas al cierre de la sesión
       anterior del símbolo, tras el escudo anti-gap y la asignación de
       portfolio (prioridad, límites y sizing).
    3. Durante la barra: SL/TP de todas las posiciones abiertas, incluidas
       las abiertas en `D`. El SL tiene prioridad si la barra toca ambos.
    4. Cierre: valoración a mercado y punto de la curva de equity.

    Las señales se precalculan con `LongSignalGenerator`, que es invariante a
    barras futuras; la señal de la barra `k` sólo se usa en la barra `k+1`.

    Con `trading_sessions` (fechas del calendario de mercado), cada serie
    debe cubrir todas las sesiones entre su primera y última barra y no tener
    barras en días sin sesión: así el índice de barra de los indicadores
    (`t-10`, `t-20`, ventanas de 20) equivale a sesiones de mercado, y la
    salida temporal cuenta sesiones del calendario. Sin calendario, se
    cuentan las barras de cada símbolo.

    Los dividendos deben expresarse en la misma escala que las barras (para
    barras ajustadas por splits, dividendos ajustados por splits).

    Con `trading_start`, las barras anteriores sólo sirven de historial para
    los indicadores: no se abren posiciones ni se registra equity antes de
    esa fecha. Permite evaluar una ventana (p. ej. walk-forward) con los
    indicadores ya calentados y sin usar datos posteriores a la ventana, que
    se limita pasando sólo las barras hasta su fin.
    """

    def __init__(self, parameters: BacktestParameters) -> None:
        self._parameters = parameters
        self._signal_generator = LongSignalGenerator(parameters.signal)
        self._allocator = PortfolioAllocator(PositionSizer(parameters.risk))

    def run(
        self,
        bars_by_symbol: Mapping[str, Sequence[ExecutionBar]],
        trading_sessions: Sequence[date] | None = None,
        dividends_by_symbol: Mapping[str, Sequence[CorporateDividend]] | None = None,
        trading_start: date | None = None,
    ) -> BacktestResult:
        parameters = self._parameters
        costs = parameters.costs
        series = self._validate_series(bars_by_symbol)
        calendar_index = self._validate_calendar(series, trading_sessions)
        dividends = self._validate_dividends(series, dividends_by_symbol or {})

        def session_number(symbol: str, index: int) -> int:
            if calendar_index is None:
                return index

            return calendar_index[series[symbol][index].timestamp.date()]

        index_by_symbol = {
            symbol: {bar.timestamp: index for index, bar in enumerate(bars)}
            for symbol, bars in series.items()
        }
        signals_by_symbol = {
            symbol: {
                index_by_symbol[symbol][signal.signal_timestamp]: signal
                for signal in self._signal_generator.generate(bars)
            }
            for symbol, bars in series.items()
        }
        sessions = sorted({bar.timestamp for bars in series.values() for bar in bars})

        cash = parameters.initial_cash
        open_trades: dict[str, OpenTrade] = {}
        marks: dict[str, Decimal] = {}
        trades: list[Trade] = []
        gap_filtered: list[GapFilteredEntry] = []
        rejected: list[RejectedCandidate] = []
        equity_curve: list[EquityPoint] = []
        dividend_payments: list[DividendPayment] = []
        previous_session_date: date | None = None

        for session in sessions:
            todays_index = {
                symbol: indexes[session]
                for symbol, indexes in index_by_symbol.items()
                if session in indexes
            }
            session_date = session.date()

            # 0. Dividendos de posiciones abiertas antes de esta sesión.
            for symbol in sorted(open_trades):
                open_trade = open_trades[symbol]
                entry_date = open_trade.entry_timestamp.date()

                for dividend in dividends.get(symbol, ()):
                    is_new = (
                        previous_session_date is None
                        or dividend.ex_date > previous_session_date
                    )

                    if not (is_new and entry_date < dividend.ex_date <= session_date):
                        continue

                    with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
                        total = dividend.amount * open_trade.quantity

                    cash += total
                    open_trade = open_trade.model_copy(
                        update={"dividend_income": open_trade.dividend_income + total}
                    )
                    dividend_payments.append(
                        DividendPayment(
                            symbol=symbol,
                            ex_date=dividend.ex_date,
                            payment_timestamp=session,
                            amount_per_share=dividend.amount,
                            quantity=open_trade.quantity,
                            total=total,
                        )
                    )

                open_trades[symbol] = open_trade

            previous_session_date = session_date

            # 1. Salidas temporales a la apertura.
            for symbol in sorted(open_trades):
                if symbol not in todays_index:
                    continue

                index = todays_index[symbol]
                open_trade = open_trades[symbol]

                if (
                    session_number(symbol, index) - open_trade.entry_session_index
                    < parameters.max_holding_sessions
                ):
                    continue

                decision = evaluate_time_exit(
                    series[symbol][index].open,
                    open_trade.exit_levels,
                )
                trade = close_trade(
                    open_trade,
                    decision,
                    session,
                    session_number(symbol, index),
                    costs,
                )
                cash += trade.exit_proceeds
                trades.append(trade)
                del open_trades[symbol]

            # 2. Entradas a la apertura (sólo dentro de la ventana de trading).
            candidates: list[EntryCandidate] = []
            is_trading = trading_start is None or session_date >= trading_start

            for symbol in sorted(todays_index if is_trading else ()):
                index = todays_index[symbol]
                signal = signals_by_symbol[symbol].get(index - 1)

                if signal is None:
                    continue

                entry_open = series[symbol][index].open

                if is_gap_filtered(
                    entry_open=entry_open,
                    signal_close=signal.close,
                    signal_atr=signal.atr,
                    threshold_atr=parameters.gap_threshold_atr,
                ):
                    gap_filtered.append(
                        GapFilteredEntry(
                            signal=signal,
                            session_timestamp=session,
                            entry_open=entry_open,
                            gap=entry_open - signal.close,
                            max_allowed_gap=parameters.gap_threshold_atr * signal.atr,
                        )
                    )
                    continue

                candidates.append(
                    EntryCandidate(
                        signal=signal,
                        entry_price=costs.buy_price(entry_open),
                    )
                )

            if candidates:
                state = PortfolioState(
                    cash=cash,
                    positions=tuple(
                        Position(
                            symbol=symbol,
                            quantity=open_trade.quantity,
                            entry_price=open_trade.entry_executed_price,
                            stop_loss=open_trade.exit_levels.stop_loss,
                            take_profit=open_trade.exit_levels.take_profit,
                            market_price=marks[symbol],
                        )
                        for symbol, open_trade in sorted(open_trades.items())
                    ),
                )
                allocation = self._allocator.allocate(candidates, state)
                rejected.extend(allocation.rejected)
                cash = allocation.resulting_state.cash

                for planned in allocation.accepted:
                    symbol = planned.candidate.symbol
                    index = todays_index[symbol]
                    open_trades[symbol] = OpenTrade(
                        symbol=symbol,
                        signal_timestamp=planned.candidate.signal.signal_timestamp,
                        entry_timestamp=session,
                        entry_session_index=session_number(symbol, index),
                        quantity=planned.sizing.quantity,
                        entry_theoretical_price=series[symbol][index].open,
                        entry_executed_price=planned.candidate.entry_price,
                        entry_commission=planned.sizing.commission,
                        exit_levels=planned.exit_levels,
                        planned_risk=planned.sizing.planned_risk,
                    )

            # 3. SL / TP durante la barra.
            for symbol in sorted(open_trades):
                if symbol not in todays_index:
                    continue

                index = todays_index[symbol]
                bar = series[symbol][index]
                open_trade = open_trades[symbol]
                exit_decision = evaluate_bar_exit(
                    open_price=bar.open,
                    high=bar.high,
                    low=bar.low,
                    levels=open_trade.exit_levels,
                )

                if exit_decision is None:
                    continue

                trade = close_trade(
                    open_trade,
                    exit_decision,
                    session,
                    session_number(symbol, index),
                    costs,
                )
                cash += trade.exit_proceeds
                trades.append(trade)
                del open_trades[symbol]

            # 4. Valoración al cierre.
            for symbol, index in todays_index.items():
                marks[symbol] = series[symbol][index].close

            market_value = sum(
                (
                    marks[symbol] * trade.quantity
                    for symbol, trade in open_trades.items()
                ),
                Decimal("0"),
            )
            if not is_trading:
                continue

            equity_curve.append(
                EquityPoint(
                    timestamp=session,
                    cash=cash,
                    market_value=market_value,
                    equity=cash + market_value,
                    open_positions=len(open_trades),
                )
            )

        # Posiciones abiertas al final de los datos: cierre al último cierre.
        for symbol in sorted(open_trades):
            last_index = len(series[symbol]) - 1
            last_bar = series[symbol][last_index]
            trade = close_trade(
                open_trades[symbol],
                ExitDecision(
                    reason=ExitReason.END_OF_DATA,
                    theoretical_price=last_bar.close,
                ),
                last_bar.timestamp,
                session_number(symbol, last_index),
                costs,
            )
            cash += trade.exit_proceeds
            trades.append(trade)

        return BacktestResult(
            parameters=parameters,
            trades=tuple(trades),
            gap_filtered=tuple(gap_filtered),
            rejected=tuple(rejected),
            equity_curve=tuple(equity_curve),
            final_cash=cash,
            dividends=tuple(dividend_payments),
            uses_trading_calendar=calendar_index is not None,
        )

    @staticmethod
    def _validate_calendar(
        series: Mapping[str, Sequence[ExecutionBar]],
        trading_sessions: Sequence[date] | None,
    ) -> dict[date, int] | None:
        if trading_sessions is None:
            return None

        ordered_sessions = sorted(set(trading_sessions))
        coverages = [
            check_session_coverage(
                symbol,
                [bar.timestamp.date() for bar in bars],
                ordered_sessions,
            )
            for symbol, bars in series.items()
        ]
        incomplete = [coverage for coverage in coverages if not coverage.is_complete]

        if incomplete:
            raise SessionCoverageError(incomplete)

        return {session: index for index, session in enumerate(ordered_sessions)}

    @staticmethod
    def _validate_dividends(
        series: Mapping[str, Sequence[ExecutionBar]],
        dividends_by_symbol: Mapping[str, Sequence[CorporateDividend]],
    ) -> dict[str, tuple[CorporateDividend, ...]]:
        dividends: dict[str, tuple[CorporateDividend, ...]] = {}

        for symbol, symbol_dividends in dividends_by_symbol.items():
            if symbol not in series:
                continue

            if any(dividend.symbol != symbol for dividend in symbol_dividends):
                raise ValueError(
                    f"Todos los dividendos de {symbol} deben tener ese símbolo"
                )

            dividends[symbol] = tuple(
                sorted(symbol_dividends, key=lambda dividend: dividend.ex_date)
            )

        return dividends

    @staticmethod
    def _validate_series(
        bars_by_symbol: Mapping[str, Sequence[ExecutionBar]],
    ) -> dict[str, tuple[ExecutionBar, ...]]:
        series: dict[str, tuple[ExecutionBar, ...]] = {}

        for symbol, bars in sorted(bars_by_symbol.items()):
            if any(bar.symbol != symbol for bar in bars):
                raise ValueError(
                    f"Todas las barras de {symbol} deben tener ese símbolo"
                )

            series[symbol] = tuple(bars)

        return series
