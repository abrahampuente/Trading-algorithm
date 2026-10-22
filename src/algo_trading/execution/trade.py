from datetime import datetime
from decimal import Decimal, localcontext

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.execution.costs import CostModel
from algo_trading.execution.exit_rules import ExitDecision, ExitReason
from algo_trading.portfolio.exit_levels import ExitLevels
from algo_trading.portfolio.position_sizing import MONEY_PRECISION, MONEY_ROUNDING


class OpenTrade(BaseModel):
    """Posición abierta por el simulador, a la espera de su salida."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    signal_timestamp: datetime
    entry_timestamp: datetime
    entry_session_index: int = Field(ge=0)
    quantity: int = Field(ge=1)
    entry_theoretical_price: Decimal = Field(gt=0)
    entry_executed_price: Decimal = Field(gt=0)
    entry_commission: Decimal = Field(ge=0)
    exit_levels: ExitLevels
    planned_risk: Decimal

    @property
    def entry_cost(self) -> Decimal:
        """Efectivo desembolsado en la entrada, incluida la comisión."""
        return self.entry_executed_price * self.quantity + self.entry_commission


class Trade(BaseModel):
    """
    Registro inmutable de una operación cerrada.

    - `gross_pnl = (salida_teórica - entrada_teórica) * cantidad`
    - `slippage_cost`: diferencia entre precios teóricos y ejecutados.
    - `total_costs = comisiones + slippage_cost`
    - `net_pnl = gross_pnl - total_costs`
      `= (salida_ejecutada - entrada_ejecutada) * cantidad - comisiones`
    - `return_pct = net_pnl / coste de entrada (incluida comisión)`
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    signal_timestamp: datetime
    entry_timestamp: datetime
    exit_timestamp: datetime
    sessions_since_entry: int = Field(ge=0)
    quantity: int = Field(ge=1)
    entry_theoretical_price: Decimal
    entry_executed_price: Decimal
    exit_theoretical_price: Decimal
    exit_executed_price: Decimal
    entry_atr: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    planned_risk: Decimal
    entry_commission: Decimal
    exit_commission: Decimal
    slippage_cost: Decimal
    total_costs: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    return_pct: Decimal
    exit_reason: ExitReason

    @property
    def exit_proceeds(self) -> Decimal:
        """Efectivo recibido en la salida, neto de comisión."""
        return self.exit_executed_price * self.quantity - self.exit_commission


def close_trade(
    open_trade: OpenTrade,
    decision: ExitDecision,
    exit_timestamp: datetime,
    exit_session_index: int,
    costs: CostModel,
) -> Trade:
    """Cierra una posición aplicando slippage y comisión a la salida."""
    quantity = open_trade.quantity
    exit_executed_price = costs.sell_price(decision.theoretical_price)
    exit_commission = costs.commission(exit_executed_price, quantity)

    with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
        gross_pnl = (
            decision.theoretical_price - open_trade.entry_theoretical_price
        ) * quantity
        slippage_cost = (
            open_trade.entry_executed_price - open_trade.entry_theoretical_price
        ) * quantity + (decision.theoretical_price - exit_executed_price) * quantity
        total_costs = open_trade.entry_commission + exit_commission + slippage_cost
        net_pnl = gross_pnl - total_costs
        return_pct = net_pnl / open_trade.entry_cost

    return Trade(
        symbol=open_trade.symbol,
        signal_timestamp=open_trade.signal_timestamp,
        entry_timestamp=open_trade.entry_timestamp,
        exit_timestamp=exit_timestamp,
        sessions_since_entry=exit_session_index - open_trade.entry_session_index,
        quantity=quantity,
        entry_theoretical_price=open_trade.entry_theoretical_price,
        entry_executed_price=open_trade.entry_executed_price,
        exit_theoretical_price=decision.theoretical_price,
        exit_executed_price=exit_executed_price,
        entry_atr=open_trade.exit_levels.entry_atr,
        stop_loss=open_trade.exit_levels.stop_loss,
        take_profit=open_trade.exit_levels.take_profit,
        planned_risk=open_trade.planned_risk,
        entry_commission=open_trade.entry_commission,
        exit_commission=exit_commission,
        slippage_cost=slippage_cost,
        total_costs=total_costs,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        return_pct=return_pct,
        exit_reason=decision.reason,
    )
