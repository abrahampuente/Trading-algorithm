from collections.abc import Iterable

from algo_trading.data.market_bar import MarketBar


def validate_market_bars(bars: Iterable[MarketBar]) -> list[MarketBar]:
    validated_bars = list(bars)

    if not validated_bars:
        raise ValueError("La colección de barras no puede estar vacía")

    seen_keys: set[tuple[str, object, str, str]] = set()

    for bar in validated_bars:
        identity = (
            bar.symbol,
            bar.timestamp,
            bar.timeframe,
            bar.source,
        )

        if identity in seen_keys:
            raise ValueError(f"Barra duplicada: {identity}")

        seen_keys.add(identity)

    ordered_bars = sorted(
        validated_bars,
        key=lambda bar: (bar.symbol, bar.timestamp),
    )

    return ordered_bars
