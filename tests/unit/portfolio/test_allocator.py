from datetime import UTC, datetime
from decimal import Decimal

import pytest

from algo_trading.portfolio import (
    EntryCandidate,
    PortfolioAllocator,
    PortfolioState,
    Position,
    RejectionReason,
    prioritize_candidates,
)
from algo_trading.strategy import LongSignal

SIGNAL_TIMESTAMP = datetime(2024, 3, 1, tzinfo=UTC)


def build_candidate(
    symbol: str,
    momentum: str,
    entry_price: str = "50",
    atr: str = "5",
) -> EntryCandidate:
    return EntryCandidate(
        signal=LongSignal(
            symbol=symbol,
            signal_timestamp=SIGNAL_TIMESTAMP,
            close=Decimal(entry_price),
            upper_band=Decimal(entry_price) - 1,
            previous_close=Decimal(entry_price) - 2,
            previous_upper_band=Decimal(entry_price) - 1,
            atr=Decimal(atr),
            momentum=Decimal(momentum),
        ),
        entry_price=Decimal(entry_price),
    )


def build_position(symbol: str, market_price: str = "100") -> Position:
    return Position(
        symbol=symbol,
        quantity=10,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        market_price=Decimal(market_price),
    )


def test_portfolio_equity_is_cash_plus_market_value() -> None:
    state = PortfolioState(
        cash=Decimal("1000"),
        positions=(
            build_position("AAA", market_price="120"),
            build_position("BBB", market_price="80"),
        ),
    )

    assert state.market_value == Decimal("2000")
    assert state.equity == Decimal("3000")


def test_portfolio_rejects_two_positions_in_same_symbol() -> None:
    with pytest.raises(ValueError, match="una posición por símbolo"):
        PortfolioState(
            cash=Decimal("1000"),
            positions=(build_position("AAA"), build_position("AAA")),
        )


def test_prioritizes_by_momentum_then_ticker() -> None:
    candidates = [
        build_candidate("CCC", momentum="0.10"),
        build_candidate("BBB", momentum="0.25"),
        build_candidate("AAA", momentum="0.10"),
        build_candidate("DDD", momentum="0.30"),
    ]

    ordered = prioritize_candidates(candidates)

    assert [candidate.symbol for candidate in ordered] == [
        "DDD",
        "BBB",
        "AAA",
        "CCC",
    ]


def test_accepts_candidates_and_updates_cash_and_positions() -> None:
    state = PortfolioState(cash=Decimal("100000"))

    result = PortfolioAllocator().allocate(
        [build_candidate("AAA", momentum="0.10")],
        state,
    )

    assert len(result.accepted) == 1
    entry = result.accepted[0]
    # Riesgo 500 / ATR 5 = 100 unidades.
    assert entry.sizing.quantity == 100
    assert entry.exit_levels.stop_loss == Decimal("45")
    assert entry.exit_levels.take_profit == Decimal("60")
    assert result.resulting_state.cash == Decimal("95000")
    assert result.resulting_state.open_symbols == frozenset({"AAA"})
    assert result.resulting_state.equity == state.equity


def test_fills_only_remaining_slots_up_to_five_positions() -> None:
    state = PortfolioState(
        cash=Decimal("100000"),
        positions=tuple(build_position(symbol) for symbol in ("P1", "P2", "P3")),
    )
    candidates = [
        build_candidate("LOW", momentum="0.05"),
        build_candidate("HIGH", momentum="0.40"),
        build_candidate("MID", momentum="0.20"),
    ]

    result = PortfolioAllocator().allocate(candidates, state)

    assert [entry.candidate.symbol for entry in result.accepted] == ["HIGH", "MID"]
    assert [(r.candidate.symbol, r.reason) for r in result.rejected] == [
        ("LOW", RejectionReason.MAX_POSITIONS_REACHED),
    ]
    assert len(result.resulting_state.positions) == 5


def test_rejects_all_candidates_when_five_positions_are_open() -> None:
    state = PortfolioState(
        cash=Decimal("100000"),
        positions=tuple(build_position(f"P{index}") for index in range(5)),
    )

    result = PortfolioAllocator().allocate(
        [build_candidate("AAA", momentum="0.50")],
        state,
    )

    assert result.accepted == ()
    assert result.rejected[0].reason is RejectionReason.MAX_POSITIONS_REACHED
    assert result.resulting_state == state


def test_rejects_symbol_already_open_and_gives_slot_to_next_candidate() -> None:
    state = PortfolioState(
        cash=Decimal("100000"),
        positions=tuple(build_position(symbol) for symbol in ("AAA", "P2", "P3", "P4")),
    )
    candidates = [
        build_candidate("AAA", momentum="0.90"),
        build_candidate("BBB", momentum="0.10"),
    ]

    result = PortfolioAllocator().allocate(candidates, state)

    assert [entry.candidate.symbol for entry in result.accepted] == ["BBB"]
    assert result.rejected[0].candidate.symbol == "AAA"
    assert result.rejected[0].reason is RejectionReason.ALREADY_OPEN


def test_insufficient_cash_rejects_lower_priority_candidate() -> None:
    # Equity 100000 (4000 en cash). AAA compra 80 unidades (4000) y agota el cash.
    state = PortfolioState(
        cash=Decimal("4000"),
        positions=(build_position("P1", market_price="9600"),),
    )
    candidates = [
        build_candidate("AAA", momentum="0.30"),
        build_candidate("BBB", momentum="0.20"),
    ]

    result = PortfolioAllocator().allocate(candidates, state)

    assert state.equity == Decimal("100000")
    assert [entry.candidate.symbol for entry in result.accepted] == ["AAA"]
    assert result.accepted[0].sizing.quantity == 80
    rejection = result.rejected[0]
    assert rejection.candidate.symbol == "BBB"
    assert rejection.reason is RejectionReason.BELOW_MINIMUM_QUANTITY
    assert rejection.sizing is not None
    assert rejection.sizing.quantity_by_capital == 0
    assert result.resulting_state.cash == Decimal("0")


def test_rejected_expensive_candidate_does_not_block_cheaper_one() -> None:
    state = PortfolioState(cash=Decimal("10000"))
    candidates = [
        # 10 % de 10000 = 1000 < precio 1500 → 0 unidades.
        build_candidate("EXP", momentum="0.50", entry_price="1500", atr="10"),
        build_candidate("CHP", momentum="0.10", entry_price="20", atr="1"),
    ]

    result = PortfolioAllocator().allocate(candidates, state)

    assert [entry.candidate.symbol for entry in result.accepted] == ["CHP"]
    assert result.rejected[0].reason is RejectionReason.BELOW_MINIMUM_QUANTITY


def test_equity_is_fixed_at_batch_start_for_every_candidate() -> None:
    state = PortfolioState(cash=Decimal("100000"))
    candidates = [
        build_candidate("AAA", momentum="0.30"),
        build_candidate("BBB", momentum="0.20"),
    ]

    result = PortfolioAllocator().allocate(candidates, state)

    assert [entry.sizing.equity for entry in result.accepted] == [
        Decimal("100000"),
        Decimal("100000"),
    ]
    assert [entry.sizing.cash for entry in result.accepted] == [
        Decimal("100000"),
        Decimal("95000"),
    ]


def test_rejects_duplicate_candidates_for_same_symbol() -> None:
    candidates = [
        build_candidate("AAA", momentum="0.30"),
        build_candidate("AAA", momentum="0.20"),
    ]

    with pytest.raises(ValueError, match="un candidato por símbolo"):
        PortfolioAllocator().allocate(candidates, PortfolioState(cash=Decimal("1")))
