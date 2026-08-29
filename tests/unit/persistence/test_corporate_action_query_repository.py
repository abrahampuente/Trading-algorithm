from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from algo_trading.persistence.models import Base, CorporateAction, CorporateActionType
from algo_trading.persistence.repositories import CorporateActionQueryRepository


def build_split(
    ex_date: date,
    symbol: str = "AAPL",
    source: str = "test",
    ratio: str = "2",
) -> CorporateAction:
    return CorporateAction(
        symbol=symbol,
        action_type=CorporateActionType.SPLIT,
        ex_date=ex_date,
        split_ratio=Decimal(ratio),
        source=source,
    )


def test_get_splits_by_symbol_returns_ordered_results() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [
                build_split(date(2024, 3, 1)),
                build_split(date(2024, 1, 1)),
                CorporateAction(
                    symbol="AAPL",
                    action_type=CorporateActionType.DIVIDEND,
                    ex_date=date(2024, 2, 1),
                    dividend_amount=Decimal("0.25"),
                    source="test",
                ),
            ]
        )
        session.commit()

        repository = CorporateActionQueryRepository(session)

        splits = repository.get_splits_by_symbol("AAPL")

        assert len(splits) == 2
        assert [split.ex_date for split in splits] == [
            date(2024, 1, 1),
            date(2024, 3, 1),
        ]

    engine.dispose()


def test_get_splits_filters_by_half_open_date_range_and_source() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [
                build_split(date(2024, 1, 1), source="provider-a"),
                build_split(date(2024, 2, 1), source="provider-a"),
                build_split(date(2024, 3, 1), source="provider-b"),
            ]
        )
        session.commit()

        repository = CorporateActionQueryRepository(session)

        splits = repository.get_splits_by_symbol(
            symbol="AAPL",
            start=date(2024, 1, 1),
            end=date(2024, 3, 1),
            source="provider-a",
        )

        assert len(splits) == 2
        assert [split.ex_date for split in splits] == [
            date(2024, 1, 1),
            date(2024, 2, 1),
        ]

    engine.dispose()
