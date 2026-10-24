"""
Orden de verificación visual en Alpaca Paper Trading.

Envía una compra limitada muy por debajo del mercado (por defecto 1 SPY a
300 USD) y la deja abierta para verla en el panel de Alpaca
(https://app.alpaca.markets, cuenta paper → Orders). No se ejecuta: el
límite debe quedar por debajo del último cierre guardado en MySQL o el script
se niega a enviarla.

    python scripts/test_visible_order.py              # envía la orden, muestra su ID
    python scripts/test_visible_order.py --status     # órdenes de prueba abiertas
    python scripts/test_visible_order.py --cancel     # cancela todas las de prueba
    python scripts/test_visible_order.py --cancel ID  # cancela una (client_order_id)

La orden pasa por `ExecutionService`, así que queda también en
`broker_orders`: la reconciliación de `algo-live` la reconoce y no bloquea el
trading por una orden desconocida. `time_in_force = day` por defecto: enviada
con el mercado cerrado, queda en cola y Alpaca la expira al cierre de la
siguiente sesión (`--gtc` la mantiene hasta cancelarla). Sólo paper trading:
la guardia exige `ALPACA_PAPER=true`, URL de paper y cuenta `PA…`.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text

from algo_trading.config.settings import ApplicationSettings
from algo_trading.execution.alpaca_broker import (
    LiveTradingNotAllowedError,
    build_alpaca_paper_broker,
)
from algo_trading.execution.orders import (
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)
from algo_trading.execution.service import ACCEPTED_BROKER_STATUSES, ExecutionService
from algo_trading.persistence.database import SessionFactory

PREFIX = "algo-visible-test-"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--cancel",
        nargs="?",
        const="ALL",
        metavar="CLIENT_ORDER_ID",
        help="Cancela la orden indicada o, sin ID, todas las de prueba abiertas.",
    )
    action.add_argument(
        "--status", action="store_true", help="Muestra las órdenes de prueba."
    )
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--limit", type=Decimal, default=Decimal("300"))
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument(
        "--gtc", action="store_true", help="Hasta cancelarla (por defecto: day)."
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    settings = ApplicationSettings.from_yaml()

    try:
        broker = build_alpaca_paper_broker(settings)
    except LiveTradingNotAllowedError as error:
        print(f"Cancelado por la guardia de paper trading: {error}")
        return 1

    with SessionFactory() as session:
        service = ExecutionService(broker, session)
        test_orders = [
            order
            for order in service.open_orders()
            if order.client_order_id.startswith(PREFIX)
        ]

        if arguments.status:
            for order in test_orders:
                service.sync(order.client_order_id)

            print(f"Órdenes de prueba abiertas: {len(test_orders)}")
            for order in service.open_orders():
                if order.client_order_id.startswith(PREFIX):
                    print(
                        f"  {order.client_order_id}  {order.side.value} "
                        f"{order.quantity.normalize()} {order.symbol} límite "
                        f"{order.limit_price}  estado {order.broker_status}  "
                        f"broker_id {order.broker_order_id}"
                    )
            return 0

        if arguments.cancel is not None:
            targets = (
                [order.client_order_id for order in test_orders]
                if arguments.cancel == "ALL"
                else [arguments.cancel]
            )

            if not targets:
                print("No hay órdenes de prueba abiertas.")
                return 0

            for client_order_id in targets:
                service.cancel(client_order_id)
                final = service.wait_for_status(
                    client_order_id, {"canceled"}, timeout_seconds=15
                )
                print(f"Cancelada {client_order_id}: {final.state.broker_status}")

            return 0

        last_close = session.execute(
            text(
                "SELECT close FROM market_bars_adjusted WHERE symbol = :symbol "
                "ORDER BY timestamp DESC LIMIT 1"
            ),
            {"symbol": arguments.symbol},
        ).scalar()

        if last_close is None or arguments.limit >= Decimal(last_close) * Decimal(
            "0.9"
        ):
            print(
                f"Límite {arguments.limit} demasiado cerca del último cierre de "
                f"{arguments.symbol} ({last_close}): la orden podría ejecutarse."
            )
            return 1

        request = OrderRequest(
            client_order_id=f"{PREFIX}{arguments.symbol}-"
            f"{datetime.now(UTC):%Y%m%dT%H%M%S}",
            symbol=arguments.symbol,
            side=OrderSide.BUY,
            quantity=arguments.quantity,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC if arguments.gtc else TimeInForce.DAY,
            limit_price=arguments.limit,
            theoretical_price=arguments.limit,
            strategy_id="visual_check",
        )
        dispatched = service.submit(request)
        accepted = service.wait_for_status(
            request.client_order_id, ACCEPTED_BROKER_STATUSES, timeout_seconds=15
        )
        state = accepted.state
        timing = dispatched.timing

        print("Orden de prueba enviada a Alpaca Paper (queda abierta):")
        print(f"  client_order_id : {state.client_order_id}")
        print(f"  broker_order_id : {state.broker_order_id}")
        print(
            f"  orden           : compra {arguments.quantity} {arguments.symbol} "
            f"límite {arguments.limit} ({request.time_in_force.value}); último "
            f"cierre {Decimal(last_close):.2f}"
        )
        print(f"  estado          : {state.broker_status}")
        if timing is not None and timing.order_request_latency_ms is not None:
            print(f"  latencia POST   : {timing.order_request_latency_ms:.0f} ms")
        print("Véala en https://app.alpaca.markets (paper) → Orders.")
        print("Para cancelarla: python scripts/test_visible_order.py --cancel")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
