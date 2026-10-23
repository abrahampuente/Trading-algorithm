"""
Del plan de la estrategia a órdenes del broker (Hito 3).

- Entrada: compra limitada `OPG` (subasta de apertura de `t+1`) con
  `límite = Close[t] + umbral_gap * ATR[t]`. Es el escudo anti-gap de la v1
  aplicado por el propio broker: si la apertura supera el límite, la orden no
  se ejecuta (equivale a `GAP_FILTERED`). El límite se redondea hacia abajo
  al tick, para no aceptar nunca un gap mayor que el permitido.
- Salida SL / TP: orden `OCO` GTC tras la ejecución de la entrada (Alpaca no
  admite órdenes bracket con `OPG`). Los niveles se recalculan con el precio
  medio ejecutado, como en la v1 (`SL = entrada - k_SL * ATR`, ATR de la
  señal congelado). El TP se redondea hacia arriba y el SL también (stop algo
  más alto: pérdida algo menor, nunca mayor que la planificada).
- Salida temporal: venta a mercado `OPG` de la cantidad en cartera, tras
  cancelar la OCO.

`client_order_id` es determinista: `<prefijo>-<intención>-<símbolo>-<fecha>-
<hash>`, de modo que reenviar el mismo plan el mismo día no duplica órdenes.
"""

import hashlib
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from algo_trading.execution.orders import (
    OrderClass,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
    round_to_tick,
)
from algo_trading.portfolio.allocator import PlannedEntry
from algo_trading.portfolio.exit_levels import ExitLevels, calculate_long_exit_levels
from algo_trading.portfolio.position_sizing import MONEY_PRECISION, MONEY_ROUNDING

DEFAULT_PREFIX = "algo"
ENTRY = "entry"
EXIT = "exit"
TIME_EXIT = "time"


def client_order_id(
    intent: str,
    symbol: str,
    session_date: date,
    strategy_id: str,
    prefix: str = DEFAULT_PREFIX,
) -> str:
    """Identificador estable de una intención de orden en una sesión."""
    digest = hashlib.sha256(
        f"{prefix}|{intent}|{symbol}|{session_date.isoformat()}|{strategy_id}".encode()
    ).hexdigest()[:12]
    return f"{prefix}-{intent}-{symbol}-{session_date:%Y%m%d}-{digest}"


class OrderPlanner:
    def __init__(
        self,
        gap_threshold_atr: Decimal = Decimal("0.5"),
        strategy_id: str = "momentum_breakout_v1",
        prefix: str = DEFAULT_PREFIX,
    ) -> None:
        if gap_threshold_atr < 0:
            raise ValueError("gap_threshold_atr no puede ser negativo")

        self._gap_threshold = gap_threshold_atr
        self._strategy_id = strategy_id
        self._prefix = prefix

    def entry_order(self, planned: PlannedEntry, session_date: date) -> OrderRequest:
        """Compra limitada en la apertura de `session_date` (la sesión `t+1`)."""
        signal = planned.candidate.signal

        with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
            max_entry = signal.close + self._gap_threshold * signal.atr

        return OrderRequest(
            client_order_id=self._id(ENTRY, signal.symbol, session_date),
            symbol=signal.symbol,
            side=OrderSide.BUY,
            quantity=planned.sizing.quantity,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.OPG,
            limit_price=round_to_tick(max_entry, ROUND_FLOOR),
            theoretical_price=signal.close,
            strategy_id=self._strategy_id,
            signal_timestamp=signal.signal_timestamp,
        )

    def exit_levels_after_fill(
        self, planned: PlannedEntry, fill_price: Decimal
    ) -> ExitLevels:
        """Niveles SL / TP con el precio ejecutado y el ATR congelado de la señal."""
        levels = planned.exit_levels
        return calculate_long_exit_levels(
            fill_price,
            levels.entry_atr,
            stop_loss_atr_multiple=levels.stop_loss_atr_multiple,
            take_profit_atr_multiple=levels.take_profit_atr_multiple,
        )

    def exit_order(
        self,
        symbol: str,
        quantity: int,
        levels: ExitLevels,
        session_date: date,
    ) -> OrderRequest:
        """OCO de salida: venta limitada en el TP y stop en el SL."""
        return OrderRequest(
            client_order_id=self._id(EXIT, symbol, session_date),
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.OCO,
            take_profit_limit_price=round_to_tick(levels.take_profit, ROUND_CEILING),
            stop_loss_stop_price=round_to_tick(levels.stop_loss, ROUND_CEILING),
            strategy_id=self._strategy_id,
        )

    def time_exit_order(
        self, symbol: str, quantity: int, session_date: date
    ) -> OrderRequest:
        """Venta a mercado en la apertura de la sesión de la salida temporal."""
        return OrderRequest(
            client_order_id=self._id(TIME_EXIT, symbol, session_date),
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.OPG,
            strategy_id=self._strategy_id,
        )

    def _id(self, intent: str, symbol: str, session_date: date) -> str:
        return client_order_id(
            intent, symbol, session_date, self._strategy_id, self._prefix
        )
