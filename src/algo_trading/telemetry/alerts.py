"""
Alertas operativas (Hito 4).

`AlertSink` es el destino de las alertas: `LogAlertSink` las escribe como una
línea JSON por alerta en el logger `algo_trading.alerts`; `MemoryAlertSink`
las guarda para tests e informes. Los notificadores externos (Telegram,
Discord) implementarán el mismo contrato. Las alertas nunca contienen
secretos.
"""

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

ALERT_LOGGER = "algo_trading.alerts"


class AlertLevel(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Alert(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    level: AlertLevel
    code: str = Field(min_length=1, max_length=64)
    message: str
    timestamp: datetime
    details: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
        )


class AlertSink(Protocol):
    def emit(self, alert: Alert) -> None: ...


class MemoryAlertSink:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def emit(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def codes(self) -> list[str]:
        return [alert.code for alert in self.alerts]


class LogAlertSink:
    """Una línea JSON por alerta, con el nivel de logging equivalente."""

    LEVELS = {
        AlertLevel.INFO: logging.INFO,
        AlertLevel.WARNING: logging.WARNING,
        AlertLevel.CRITICAL: logging.CRITICAL,
    }

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(ALERT_LOGGER)

    def emit(self, alert: Alert) -> None:
        self._logger.log(self.LEVELS[alert.level], alert.to_json())


class AlertEmitter:
    """Crea alertas con marca de tiempo y las envía a varios destinos."""

    def __init__(
        self,
        *sinks: AlertSink,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sinks = sinks
        self._clock = clock or (lambda: datetime.now(UTC))

    def emit(
        self,
        level: AlertLevel,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> Alert:
        alert = Alert(
            level=level,
            code=code,
            message=message,
            timestamp=self._clock(),
            details=dict(details or {}),
        )

        for sink in self._sinks:
            sink.emit(alert)

        return alert
