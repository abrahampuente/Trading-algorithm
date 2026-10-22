from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

from algo_trading.data.market_bar import MarketBar
from algo_trading.data.providers.snapshot_checksum import (
    CHECKSUM_VERSION,
    calculate_snapshot_checksum,
)
from algo_trading.domain.corporate_actions.dto import (
    CorporateDividend,
    CorporateSplit,
)


def build_bar(
    symbol: str,
    timestamp: datetime,
    close: Decimal,
) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        timestamp=timestamp,
        timeframe="1d",
        source="fake",
        open=Decimal("100"),
        high=close + Decimal("2"),
        low=Decimal("99"),
        close=close,
        volume=Decimal("1000000"),
    )


def test_returns_same_checksum_for_same_content_in_different_order() -> None:
    first_bar = build_bar(
        symbol="AAA",
        timestamp=datetime(2025, 1, 2, tzinfo=UTC),
        close=Decimal("103"),
    )
    second_bar = build_bar(
        symbol="BBB",
        timestamp=datetime(2025, 1, 3, tzinfo=UTC),
        close=Decimal("203"),
    )
    split = CorporateSplit(
        symbol="AAA",
        ex_date=date(2025, 1, 4),
        ratio=Decimal("2"),
        source="fake",
    )
    dividend = CorporateDividend(
        symbol="BBB",
        ex_date=date(2025, 1, 5),
        amount=Decimal("0.25"),
        source="fake",
    )

    first_checksum = calculate_snapshot_checksum(
        bars=(first_bar, second_bar),
        splits=(split,),
        dividends=(dividend,),
    )
    reordered_checksum = calculate_snapshot_checksum(
        bars=(second_bar, first_bar),
        splits=(split,),
        dividends=(dividend,),
    )

    assert first_checksum == reordered_checksum


def test_returns_different_checksum_when_bar_content_changes() -> None:
    original_bar = build_bar(
        symbol="AAA",
        timestamp=datetime(2025, 1, 2, tzinfo=UTC),
        close=Decimal("103"),
    )
    changed_bar = build_bar(
        symbol="AAA",
        timestamp=datetime(2025, 1, 2, tzinfo=UTC),
        close=Decimal("104"),
    )

    original_checksum = calculate_snapshot_checksum(
        bars=(original_bar,),
        splits=(),
        dividends=(),
    )
    changed_checksum = calculate_snapshot_checksum(
        bars=(changed_bar,),
        splits=(),
        dividends=(),
    )

    assert original_checksum != changed_checksum


def test_returns_same_checksum_for_equivalent_decimal_representations() -> None:
    first_bar = build_bar(
        symbol="AAA",
        timestamp=datetime(2025, 1, 2, tzinfo=UTC),
        close=Decimal("103.0"),
    )
    equivalent_bar = build_bar(
        symbol="AAA",
        timestamp=datetime(2025, 1, 2, tzinfo=UTC),
        close=Decimal("103.000000"),
    )

    first_checksum = calculate_snapshot_checksum(
        bars=(first_bar,),
        splits=(),
        dividends=(),
    )
    equivalent_checksum = calculate_snapshot_checksum(
        bars=(equivalent_bar,),
        splits=(),
        dividends=(),
    )

    assert first_checksum == equivalent_checksum


def test_returns_different_checksum_when_corporate_action_changes() -> None:
    dividend = CorporateDividend(
        symbol="AAA",
        ex_date=date(2025, 1, 3),
        amount=Decimal("0.25"),
        source="fake",
    )
    changed_dividend = CorporateDividend(
        symbol="AAA",
        ex_date=date(2025, 1, 3),
        amount=Decimal("0.30"),
        source="fake",
    )

    original_checksum = calculate_snapshot_checksum(
        bars=(),
        splits=(),
        dividends=(dividend,),
    )
    changed_checksum = calculate_snapshot_checksum(
        bars=(),
        splits=(),
        dividends=(changed_dividend,),
    )

    assert original_checksum != changed_checksum


def test_treats_naive_timestamps_as_utc() -> None:
    aware_bar = build_bar(
        symbol="AAA",
        timestamp=datetime(2025, 1, 2, 14, 30, tzinfo=UTC),
        close=Decimal("103"),
    )
    naive_bar = build_bar(
        symbol="AAA",
        timestamp=datetime(2025, 1, 2, 14, 30),
        close=Decimal("103"),
    )

    aware_checksum = calculate_snapshot_checksum(
        bars=(aware_bar,),
        splits=(),
        dividends=(),
    )
    naive_checksum = calculate_snapshot_checksum(
        bars=(naive_bar,),
        splits=(),
        dividends=(),
    )

    assert aware_checksum == naive_checksum


def test_normalizes_non_utc_timezones_to_utc() -> None:
    utc_bar = build_bar(
        symbol="AAA",
        timestamp=datetime(2025, 1, 2, 14, 30, tzinfo=UTC),
        close=Decimal("103"),
    )
    santiago_bar = build_bar(
        symbol="AAA",
        timestamp=datetime(2025, 1, 2, 11, 30, tzinfo=timezone(timedelta(hours=-3))),
        close=Decimal("103"),
    )

    utc_checksum = calculate_snapshot_checksum(
        bars=(utc_bar,),
        splits=(),
        dividends=(),
    )
    santiago_checksum = calculate_snapshot_checksum(
        bars=(santiago_bar,),
        splits=(),
        dividends=(),
    )

    assert utc_checksum == santiago_checksum


def test_distinguishes_splits_from_dividends_with_same_identity() -> None:
    split_checksum = calculate_snapshot_checksum(
        bars=(),
        splits=(
            CorporateSplit(
                symbol="AAA",
                ex_date=date(2025, 1, 3),
                ratio=Decimal("2"),
                source="fake",
            ),
        ),
        dividends=(),
    )
    dividend_checksum = calculate_snapshot_checksum(
        bars=(),
        splits=(),
        dividends=(
            CorporateDividend(
                symbol="AAA",
                ex_date=date(2025, 1, 3),
                amount=Decimal("2"),
                source="fake",
            ),
        ),
    )

    assert split_checksum != dividend_checksum


def test_checksum_algorithm_v1_is_stable() -> None:
    """Fija el resultado de v1: si cambia, hay que versionar el algoritmo."""
    checksum = calculate_snapshot_checksum(
        bars=(
            build_bar(
                symbol="AAA",
                timestamp=datetime(2025, 1, 2, tzinfo=UTC),
                close=Decimal("103"),
            ),
        ),
        splits=(
            CorporateSplit(
                symbol="AAA",
                ex_date=date(2025, 1, 4),
                ratio=Decimal("2"),
                source="fake",
            ),
        ),
        dividends=(
            CorporateDividend(
                symbol="AAA",
                ex_date=date(2025, 1, 5),
                amount=Decimal("0.25"),
                source="fake",
            ),
        ),
    )

    assert CHECKSUM_VERSION == "market-data-snapshot-v1"
    assert checksum == (
        "318e7549023e60c3a9ece150292a0bf7bc765dfbaa4a8095b1d154f96e56f3b0"
    )
