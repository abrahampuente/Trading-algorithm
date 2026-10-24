"""
Notificaciones externas de alertas (Hito 4): webhook de Discord y bot de
Telegram.

- Sólo se notifican las alertas que pasan el filtro: nivel mínimo (por
  defecto WARNING) o un código de la lista (plan diario, ejecuciones,
  salidas de protección, bloqueos, resumen diario).
- Un fallo de red o del servicio nunca interrumpe el trading: se registra
  en el log y la alerta se descarta (timeout corto, sin reintentos).
- Los secretos (URL del webhook, token del bot) sólo vienen del entorno y
  nunca aparecen en los mensajes ni en el log.
"""

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Collection, Mapping

from pydantic import SecretStr

from algo_trading.telemetry.alerts import Alert, AlertLevel

LEVEL_ORDER = {AlertLevel.INFO: 0, AlertLevel.WARNING: 1, AlertLevel.CRITICAL: 2}
DEFAULT_NOTIFY_CODES = frozenset(
    {
        "PLAN_GENERATED",
        "ORDER_FILLED",
        "PROTECTIVE_EXIT_SUBMITTED",
        "DAILY_SUMMARY",
        "DAEMON_STARTED",
        "DAEMON_STOPPED",
    }
)
TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 1_900  # Discord admite 2.000 caracteres; Telegram 4.096

JsonPoster = Callable[[str, Mapping[str, object], float], int]

_log = logging.getLogger(__name__)


def urllib_post_json(url: str, payload: Mapping[str, object], timeout: float) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return error.code


class AlertFilter:
    def __init__(
        self,
        min_level: AlertLevel = AlertLevel.WARNING,
        codes: Collection[str] = DEFAULT_NOTIFY_CODES,
    ) -> None:
        self._min_level = min_level
        self._codes = frozenset(codes)

    def accepts(self, alert: Alert) -> bool:
        return (
            LEVEL_ORDER[alert.level] >= LEVEL_ORDER[self._min_level]
            or alert.code in self._codes
        )


def format_alert(alert: Alert) -> str:
    """Texto breve: nivel, código, mensaje y detalles relevantes."""
    icon = {AlertLevel.INFO: "ℹ️", AlertLevel.WARNING: "⚠️", AlertLevel.CRITICAL: "🚨"}
    lines = [f"{icon[alert.level]} [{alert.level.value}] {alert.code}", alert.message]

    for order in alert.details.get("orders", []) or []:
        if isinstance(order, Mapping):
            lines.append(
                f"• {order.get('symbol')} x{order.get('quantity')} "
                f"límite {order.get('limit_price')}"
            )

    lines.append(f"{alert.timestamp:%Y-%m-%d %H:%M:%S} UTC")
    text = "\n".join(lines)
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text

    return text[: MAX_MESSAGE_LENGTH - 1] + "…"


class _WebhookSink:
    def __init__(
        self,
        alert_filter: AlertFilter | None,
        post: JsonPoster | None,
        timeout_seconds: float,
    ) -> None:
        self._filter = alert_filter or AlertFilter()
        self._post = post or urllib_post_json
        self._timeout = timeout_seconds
        self.sent = 0
        self.failed = 0

    def emit(self, alert: Alert) -> None:
        if not self._filter.accepts(alert):
            return

        try:
            status = self._post(self._url(), self._payload(alert), self._timeout)
        except (OSError, ValueError) as error:
            self.failed += 1
            _log.warning(
                "Notificación %s no enviada: %s", alert.code, type(error).__name__
            )
            return

        if 200 <= status < 300:
            self.sent += 1
        else:
            self.failed += 1
            _log.warning("Notificación %s rechazada (HTTP %s)", alert.code, status)

    def _url(self) -> str:
        raise NotImplementedError

    def _payload(self, alert: Alert) -> dict[str, object]:
        raise NotImplementedError


class DiscordWebhookSink(_WebhookSink):
    def __init__(
        self,
        webhook_url: SecretStr,
        alert_filter: AlertFilter | None = None,
        post: JsonPoster | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(alert_filter, post, timeout_seconds)
        self._webhook_url = webhook_url

    def _url(self) -> str:
        return self._webhook_url.get_secret_value()

    def _payload(self, alert: Alert) -> dict[str, object]:
        return {"content": format_alert(alert), "allowed_mentions": {"parse": []}}


class TelegramSink(_WebhookSink):
    def __init__(
        self,
        bot_token: SecretStr,
        chat_id: str,
        alert_filter: AlertFilter | None = None,
        post: JsonPoster | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(alert_filter, post, timeout_seconds)
        self._bot_token = bot_token
        self._chat_id = chat_id

    def _url(self) -> str:
        return f"{TELEGRAM_API}/bot{self._bot_token.get_secret_value()}/sendMessage"

    def _payload(self, alert: Alert) -> dict[str, object]:
        return {
            "chat_id": self._chat_id,
            "text": format_alert(alert),
            "disable_web_page_preview": True,
        }
