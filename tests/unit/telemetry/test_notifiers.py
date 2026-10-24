import logging
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from algo_trading.cli.live import build_alert_emitter
from algo_trading.config.settings import ApplicationSettings
from algo_trading.telemetry.alerts import Alert, AlertLevel
from algo_trading.telemetry.notifiers import (
    MAX_MESSAGE_LENGTH,
    AlertFilter,
    DiscordWebhookSink,
    TelegramSink,
    format_alert,
)

NOW = datetime(2026, 10, 26, 13, 16, tzinfo=UTC)
WEBHOOK = SecretStr("https://discord.example/api/webhooks/123/secret-token")
BOT_TOKEN = SecretStr("123456:secret-bot-token")


def alert(level: AlertLevel = AlertLevel.INFO, code: str = "PLAN_GENERATED") -> Alert:
    return Alert(
        level=level,
        code=code,
        message="Plan 2026-10-26: 1 órdenes OPG de 2 señales del cierre 2026-10-23",
        timestamp=NOW,
        details={
            "orders": [{"symbol": "AAPL", "quantity": 54, "limit_price": "176.43"}]
        },
    )


class Poster:
    def __init__(self, status: int = 204, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[tuple[str, Mapping[str, object], float]] = []

    def __call__(self, url: str, payload: Mapping[str, object], timeout: float) -> int:
        self.calls.append((url, payload, timeout))

        if self.error is not None:
            raise self.error

        return self.status


@pytest.mark.parametrize(
    ("level", "code", "accepted"),
    [
        (AlertLevel.INFO, "PLAN_GENERATED", True),  # código de la lista
        (AlertLevel.INFO, "RECONCILIATION_OK", False),
        (AlertLevel.WARNING, "STALE_LOCAL_ORDERS", True),  # nivel mínimo
        (AlertLevel.CRITICAL, "TRADING_HALTED", True),
    ],
)
def test_filter_by_level_or_code(level: AlertLevel, code: str, accepted: bool) -> None:
    assert AlertFilter().accepts(alert(level, code)) is accepted


def test_message_format_includes_orders_and_is_bounded() -> None:
    text = format_alert(alert())

    assert "[INFO] PLAN_GENERATED" in text
    assert "• AAPL x54 límite 176.43" in text
    assert "2026-10-26 13:16:00 UTC" in text

    long_alert = alert().model_copy(update={"message": "x" * 5000})
    assert len(format_alert(long_alert)) == MAX_MESSAGE_LENGTH


def test_discord_payload() -> None:
    poster = Poster()
    sink = DiscordWebhookSink(WEBHOOK, post=poster)

    sink.emit(alert())
    sink.emit(alert(AlertLevel.INFO, "RECONCILIATION_OK"))  # filtrada

    ((url, payload, timeout),) = poster.calls
    assert url == WEBHOOK.get_secret_value()
    assert payload["allowed_mentions"] == {"parse": []}
    assert "PLAN_GENERATED" in str(payload["content"])
    assert timeout == 5.0
    assert sink.sent == 1


def test_telegram_payload() -> None:
    poster = Poster(status=200)
    sink = TelegramSink(BOT_TOKEN, "-100123", post=poster)

    sink.emit(alert(AlertLevel.CRITICAL, "TRADING_HALTED"))

    ((url, payload, _),) = poster.calls
    assert url == "https://api.telegram.org/bot123456:secret-bot-token/sendMessage"
    assert payload["chat_id"] == "-100123"
    assert "TRADING_HALTED" in str(payload["text"])


@pytest.mark.parametrize(
    "poster", [Poster(error=OSError("sin red")), Poster(status=500), Poster(status=401)]
)
def test_failures_never_raise_or_leak_secrets(
    poster: Poster, caplog: pytest.LogCaptureFixture
) -> None:
    sink = DiscordWebhookSink(WEBHOOK, post=poster)

    with caplog.at_level(logging.WARNING):
        sink.emit(alert())

    assert sink.failed == 1
    assert "secret-token" not in caplog.text
    assert "PLAN_GENERATED" in caplog.text


def test_emitter_adds_external_sinks_only_with_secrets() -> None:
    local = ApplicationSettings.from_yaml()
    poster = Poster()
    plain = local.model_copy(
        update={"discord_webhook_url": SecretStr(""), "telegram_bot_token": None}
    )
    configured = local.model_copy(
        update={
            "discord_webhook_url": WEBHOOK,
            "telegram_bot_token": BOT_TOKEN,
            "telegram_chat_id": "-100123",
        }
    )

    build_alert_emitter(plain, lambda: NOW, poster).emit(
        AlertLevel.CRITICAL, "TRADING_HALTED", "bloqueado"
    )
    assert poster.calls == []

    build_alert_emitter(configured, lambda: NOW, poster).emit(
        AlertLevel.CRITICAL, "TRADING_HALTED", "bloqueado"
    )
    assert [url.split("/")[2] for url, _, _ in poster.calls] == [
        "discord.example",
        "api.telegram.org",
    ]
