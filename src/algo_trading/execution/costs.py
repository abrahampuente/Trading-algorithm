from decimal import Decimal, localcontext

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.portfolio.position_sizing import MONEY_PRECISION, MONEY_ROUNDING


class CostModel(BaseModel):
    """
    Costes por ejecución; los valores por defecto son el perfil base de la v1.

    - Compra: `precio_ejecutado = precio_teórico * (1 + slippage)`.
    - Venta: `precio_ejecutado = precio_teórico * (1 - slippage)`.
    - Comisión: `precio_ejecutado * cantidad * commission_rate`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commission_rate: Decimal = Field(default=Decimal("0.0005"), ge=0, lt=1)
    slippage_rate: Decimal = Field(default=Decimal("0.0005"), ge=0, lt=1)

    def buy_price(self, theoretical_price: Decimal) -> Decimal:
        with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
            return theoretical_price * (1 + self.slippage_rate)

    def sell_price(self, theoretical_price: Decimal) -> Decimal:
        with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
            return theoretical_price * (1 - self.slippage_rate)

    def commission(self, executed_price: Decimal, quantity: int) -> Decimal:
        with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
            return executed_price * quantity * self.commission_rate
