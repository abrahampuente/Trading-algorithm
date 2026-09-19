import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from algo_trading.data.market_bar import MarketBar
from algo_trading.domain.corporate_actions.dto import (
    CorporateDividend,
    CorporateSplit,
)

CHECKSUM_ALGORITHM = "sha256"
CHECKSUM_VERSION = "market-data-snapshot-v1"


def calculate_snapshot_checksum(
    bars: Sequence[MarketBar],
    splits: Sequence[CorporateSplit],
    dividends: Sequence[CorporateDividend],
) -> str:
    """Calcula un checksum canónico e independiente del orden del snapshot."""
    payload = {
        "version": CHECKSUM_VERSION,
        "bars": sorted(
            (_bar_to_payload(bar) for bar in bars),
            key=lambda item: (
                item["symbol"],
                item["timestamp"],
                item["timeframe"],
                item["source"],
            ),
        ),
        "splits": sorted(
            (_split_to_payload(split) for split in splits),
            key=lambda item: (
                item["symbol"],
                item["ex_date"],
                item["source"],
            ),
        ),
        "dividends": sorted(
            (_dividend_to_payload(dividend) for dividend in dividends),
            key=lambda item: (
                item["symbol"],
                item["ex_date"],
                item["source"],
            ),
        ),
    }

    serialized_payload = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )

    return hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()


def _bar_to_payload(bar: MarketBar) -> dict[str, Any]:
    return {
        "symbol": bar.symbol,
        "timestamp": _serialize_datetime(bar.timestamp),
        "timeframe": bar.timeframe,
        "source": bar.source,
        "open": _serialize_decimal(bar.open),
        "high": _serialize_decimal(bar.high),
        "low": _serialize_decimal(bar.low),
        "close": _serialize_decimal(bar.close),
        "volume": _serialize_decimal(bar.volume),
    }


def _split_to_payload(split: CorporateSplit) -> dict[str, str]:
    return {
        "symbol": split.symbol,
        "ex_date": _serialize_date(split.ex_date),
        "ratio": _serialize_decimal(split.ratio),
        "source": split.source,
    }


def _dividend_to_payload(dividend: CorporateDividend) -> dict[str, str]:
    return {
        "symbol": dividend.symbol,
        "ex_date": _serialize_date(dividend.ex_date),
        "amount": _serialize_decimal(dividend.amount),
        "source": dividend.source,
    }


def _serialize_datetime(value: datetime) -> str:
    """Normaliza timestamps aware a UTC y los serializa de forma estable."""
    if value.tzinfo is None:
        return value.isoformat(timespec="microseconds")

    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _serialize_date(value: date) -> str:
    return value.isoformat()


def _serialize_decimal(value: Decimal) -> str:
    """Conserva la equivalencia numérica de Decimal, sin ceros irrelevantes."""
    return format(value.normalize(), "f")
