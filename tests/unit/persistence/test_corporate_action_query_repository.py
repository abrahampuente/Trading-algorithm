from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from algo_trading.domain.corporate_actions.dto import CorporateDividend
from algo_trading.persistence.models import Base, CorporateAction, CorporateActionType
from algo_trading.persistence.repositories import CorporateActionQueryRepository


def test_get_split_dtos_by_symbol_returns_domain_objects() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            build_split(
                ex_date=date(2024, 1, 1),
                ratio="2",
            )
        )
        session.commit()

        repository = CorporateActionQueryRepository(session)

        splits = repository.get_split_dtos_by_symbol("AAPL")

        assert len(splits) == 1
        assert splits[0].symbol == "AAPL"
        assert splits[0].ratio == Decimal("2")
        assert splits[0].source == "test"

    engine.dispose()


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


def test_get_dividend_dtos_by_symbol_returns_domain_dtos() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            CorporateAction(
                symbol="AAPL",
                action_type=CorporateActionType.DIVIDEND,
                ex_date=date(2024, 2, 15),
                split_ratio=None,
                dividend_amount=Decimal("0.24"),
                source="test-source",
                announced_at=None,
            )
        )
        session.commit()

        repository = CorporateActionQueryRepository(session)

        result = repository.get_dividend_dtos_by_symbol(
            symbol="AAPL",
            source="test-source",
        )

        assert len(result) == 1
        assert isinstance(result[0], CorporateDividend)
        assert result[0].symbol == "AAPL"
        assert result[0].ex_date == date(2024, 2, 15)
        assert result[0].amount == Decimal("0.24")
        assert result[0].source == "test-source"

    engine.dispose()


def build_dividend(
    ex_date: date,
    symbol: str = "AAPL",
    source: str = "test",
    amount: str = "0.25",
) -> CorporateAction:
    return CorporateAction(
        symbol=symbol,
        action_type=CorporateActionType.DIVIDEND,
        ex_date=ex_date,
        dividend_amount=Decimal(amount),
        source=source,
    )


def test_get_dividend_dtos_by_symbol_returns_ordered_dividends_only() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [
                build_dividend(date(2024, 3, 1), amount="0.30"),
                build_split(date(2024, 2, 1)),
                build_dividend(date(2024, 1, 1), amount="0.20"),
                build_dividend(date(2024, 2, 1), symbol="MSFT"),
            ]
        )
        session.commit()

        repository = CorporateActionQueryRepository(session)

        dividends = repository.get_dividend_dtos_by_symbol("AAPL")

        assert all(isinstance(dividend, CorporateDividend) for dividend in dividends)
        assert [(dividend.ex_date, dividend.amount) for dividend in dividends] == [
            (date(2024, 1, 1), Decimal("0.20")),
            (date(2024, 3, 1), Decimal("0.30")),
        ]

    engine.dispose()


def test_get_dividend_dtos_filters_by_half_open_date_range_and_source() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [
                build_dividend(date(2023, 12, 31), source="provider-a"),
                build_dividend(date(2024, 1, 1), source="provider-a"),
                build_dividend(date(2024, 2, 1), source="provider-b"),
                build_dividend(date(2024, 2, 15), source="provider-a"),
                build_dividend(date(2024, 3, 1), source="provider-a"),
            ]
        )
        session.commit()

        repository = CorporateActionQueryRepository(session)

        dividends = repository.get_dividend_dtos_by_symbol(
            symbol="AAPL",
            start=date(2024, 1, 1),
            end=date(2024, 3, 1),
            source="provider-a",
        )

        assert [dividend.ex_date for dividend in dividends] == [
            date(2024, 1, 1),
            date(2024, 2, 15),
        ]
        assert all(dividend.source == "provider-a" for dividend in dividends)

    engine.dispose()


def test_get_dividend_dtos_returns_empty_sequence_without_dividends() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(build_split(date(2024, 1, 1)))
        session.commit()

        repository = CorporateActionQueryRepository(session)

        assert list(repository.get_dividend_dtos_by_symbol("AAPL")) == []

    engine.dispose()


def test_get_dividend_dtos_rejects_dividend_without_amount() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            CorporateAction(
                symbol="AAPL",
                action_type=CorporateActionType.DIVIDEND,
                ex_date=date(2024, 1, 1),
                dividend_amount=None,
                source="test",
            )
        )
        session.commit()

        repository = CorporateActionQueryRepository(session)

        with pytest.raises(ValueError, match="dividend_amount"):
            repository.get_dividend_dtos_by_symbol("AAPL")

    engine.dispose()
