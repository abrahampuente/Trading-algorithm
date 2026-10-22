from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.portfolio.exit_levels import ExitLevels, calculate_long_exit_levels
from algo_trading.portfolio.portfolio_state import PortfolioState, Position
from algo_trading.portfolio.position_sizing import (
    PositionSizer,
    PositionSizingResult,
)
from algo_trading.strategy.long_signal import LongSignal


class EntryCandidate(BaseModel):
    """
    Señal emitida en `t` con el precio de entrada conocido en la apertura `t+1`.

    `entry_price` es el precio esperado de ejecución (apertura más slippage).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal: LongSignal
    entry_price: Decimal = Field(gt=0)

    @property
    def symbol(self) -> str:
        return self.signal.symbol


class RejectionReason(StrEnum):
    ALREADY_OPEN = "ALREADY_OPEN"
    MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED"
    BELOW_MINIMUM_QUANTITY = "BELOW_MINIMUM_QUANTITY"


class PlannedEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: EntryCandidate
    exit_levels: ExitLevels
    sizing: PositionSizingResult


class RejectedCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: EntryCandidate
    reason: RejectionReason
    sizing: PositionSizingResult | None = None


class AllocationResult(BaseModel):
    """Entradas aprobadas y rechazadas, en orden de prioridad."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: tuple[PlannedEntry, ...]
    rejected: tuple[RejectedCandidate, ...]
    resulting_state: PortfolioState


def prioritize_candidates(
    candidates: Sequence[EntryCandidate],
) -> tuple[EntryCandidate, ...]:
    """Ordena por mayor momentum y, en empate, por ticker alfabético."""
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (-candidate.signal.momentum, candidate.symbol),
        )
    )


class PortfolioAllocator:
    """
    Decide qué señales simultáneas se abren y con qué cantidad.

    Los candidatos se procesan en orden de prioridad. El equity se fija al
    inicio del lote para que el sizing no dependa del orden de compra; el
    cash se descuenta tras cada entrada aprobada.
    """

    def __init__(self, sizer: PositionSizer | None = None) -> None:
        self._sizer = sizer or PositionSizer()

    def allocate(
        self,
        candidates: Sequence[EntryCandidate],
        state: PortfolioState,
    ) -> AllocationResult:
        symbols = [candidate.symbol for candidate in candidates]

        if len(set(symbols)) != len(symbols):
            raise ValueError("No puede haber más de un candidato por símbolo")

        max_positions = self._sizer.parameters.max_positions
        equity = state.equity
        cash = state.cash
        positions = list(state.positions)
        accepted: list[PlannedEntry] = []
        rejected: list[RejectedCandidate] = []

        for candidate in prioritize_candidates(candidates):
            if any(position.symbol == candidate.symbol for position in positions):
                rejected.append(
                    RejectedCandidate(
                        candidate=candidate,
                        reason=RejectionReason.ALREADY_OPEN,
                    )
                )
                continue

            if len(positions) >= max_positions:
                rejected.append(
                    RejectedCandidate(
                        candidate=candidate,
                        reason=RejectionReason.MAX_POSITIONS_REACHED,
                    )
                )
                continue

            exit_levels = calculate_long_exit_levels(
                entry_price=candidate.entry_price,
                entry_atr=candidate.signal.atr,
            )
            sizing = self._sizer.size(
                equity=equity,
                cash=cash,
                entry_price=candidate.entry_price,
                stop_loss=exit_levels.stop_loss,
            )

            if not sizing.is_tradable:
                rejected.append(
                    RejectedCandidate(
                        candidate=candidate,
                        reason=RejectionReason.BELOW_MINIMUM_QUANTITY,
                        sizing=sizing,
                    )
                )
                continue

            cash -= sizing.total_cost
            positions.append(
                Position(
                    symbol=candidate.symbol,
                    quantity=sizing.quantity,
                    entry_price=candidate.entry_price,
                    stop_loss=exit_levels.stop_loss,
                    take_profit=exit_levels.take_profit,
                    market_price=candidate.entry_price,
                )
            )
            accepted.append(
                PlannedEntry(
                    candidate=candidate,
                    exit_levels=exit_levels,
                    sizing=sizing,
                )
            )

        return AllocationResult(
            accepted=tuple(accepted),
            rejected=tuple(rejected),
            resulting_state=PortfolioState(cash=cash, positions=tuple(positions)),
        )
