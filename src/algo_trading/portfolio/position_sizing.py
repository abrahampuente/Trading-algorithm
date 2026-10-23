from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

MONEY_PRECISION = 28
MONEY_ROUNDING = ROUND_HALF_EVEN


class RiskParameters(BaseModel):
    """Parámetros de gestión monetaria; los valores por defecto son los de la v1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_per_trade: Decimal = Field(default=Decimal("0.005"), gt=0, le=1)
    max_position_exposure: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    max_positions: int = Field(default=5, ge=1)
    commission_rate: Decimal = Field(default=Decimal("0"), ge=0, lt=1)
    stop_loss_atr_multiple: Decimal = Field(default=Decimal("1"), gt=0)
    take_profit_atr_multiple: Decimal = Field(default=Decimal("2"), gt=0)


class SizingLimit(StrEnum):
    """Restricción que determina la cantidad final."""

    RISK = "risk"
    CAPITAL = "capital"
    EXPOSURE = "exposure"


class PositionSizingResult(BaseModel):
    """
    Resultado trazable del cálculo de tamaño de posición.

    `quantity` es el mínimo de las tres cantidades. Si es menor que una
    unidad, la operación no debe abrirse (`is_tradable` es False).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    equity: Decimal
    cash: Decimal
    entry_price: Decimal
    stop_loss: Decimal
    risk_amount: Decimal
    risk_per_unit: Decimal
    quantity_by_risk: int = Field(ge=0)
    quantity_by_capital: int = Field(ge=0)
    quantity_by_exposure: int = Field(ge=0)
    quantity: int = Field(ge=0)
    limiting_factor: SizingLimit
    planned_risk: Decimal
    notional: Decimal
    commission: Decimal

    @property
    def is_tradable(self) -> bool:
        return self.quantity >= 1

    @property
    def total_cost(self) -> Decimal:
        """Efectivo necesario para abrir la posición, incluida la comisión."""
        return self.notional + self.commission


class PositionSizer:
    """
    Calcula la cantidad de una entrada Long.

    - `cantidad_por_riesgo = floor(equity * risk_per_trade / (entrada - SL))`
    - `cantidad_por_capital = floor(cash / (entrada * (1 + comisión)))`
    - `cantidad_por_exposicion = floor(equity * max_position_exposure / entrada)`
    - `cantidad_final = min(las tres)`

    Todas las cantidades se redondean hacia abajo a unidades enteras.
    """

    def __init__(self, parameters: RiskParameters | None = None) -> None:
        self._parameters = parameters or RiskParameters()

    @property
    def parameters(self) -> RiskParameters:
        return self._parameters

    def size(
        self,
        equity: Decimal,
        cash: Decimal,
        entry_price: Decimal,
        stop_loss: Decimal,
    ) -> PositionSizingResult:
        if equity < 0:
            raise ValueError("equity no puede ser negativo")

        if cash < 0:
            raise ValueError("cash no puede ser negativo")

        if entry_price <= 0:
            raise ValueError("entry_price debe ser mayor que 0")

        if stop_loss >= entry_price:
            raise ValueError("stop_loss debe ser menor que entry_price en un Long")

        parameters = self._parameters

        with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
            risk_amount = equity * parameters.risk_per_trade
            risk_per_unit = entry_price - stop_loss
            unit_cost = entry_price * (1 + parameters.commission_rate)

            quantities = {
                SizingLimit.RISK: _floor_units(risk_amount / risk_per_unit),
                SizingLimit.CAPITAL: _floor_units(cash / unit_cost),
                SizingLimit.EXPOSURE: _floor_units(
                    equity * parameters.max_position_exposure / entry_price
                ),
            }
            # En empate se informa el primero en este orden: riesgo, capital,
            # exposición.
            limiting_factor = min(quantities, key=lambda limit: quantities[limit])
            quantity = quantities[limiting_factor]

            notional = entry_price * quantity
            commission = notional * parameters.commission_rate

            return PositionSizingResult(
                equity=equity,
                cash=cash,
                entry_price=entry_price,
                stop_loss=stop_loss,
                risk_amount=risk_amount,
                risk_per_unit=risk_per_unit,
                quantity_by_risk=quantities[SizingLimit.RISK],
                quantity_by_capital=quantities[SizingLimit.CAPITAL],
                quantity_by_exposure=quantities[SizingLimit.EXPOSURE],
                quantity=quantity,
                limiting_factor=limiting_factor,
                planned_risk=risk_per_unit * quantity,
                notional=notional,
                commission=commission,
            )


def _floor_units(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))
