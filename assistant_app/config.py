"""Runtime configuration parsing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .errors import AssistantError
from .utils import parse_bool, parse_float, parse_int


@dataclass
class QuietHours:
    start: time
    end: time

    @classmethod
    def parse(cls, raw: str) -> "QuietHours":
        parts = [part.strip() for part in raw.split("-", 1)]
        if len(parts) != 2:
            raise AssistantError(
                "QUIET_HOURS должен быть в формате HH:MM-HH:MM, например 23:00-08:00."
            )
        return cls(start=parse_hhmm(parts[0]), end=parse_hhmm(parts[1]))

    def is_quiet(self, moment: datetime) -> bool:
        current = moment.time()
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= current < self.end
        return current >= self.start or current < self.end


def parse_hhmm(value: str) -> time:
    try:
        hour, minute = value.strip().split(":", 1)
        parsed = time(hour=int(hour), minute=int(minute))
    except Exception as exc:  # noqa: BLE001
        raise AssistantError(
            f"Неверный формат времени '{value}'. Ожидается HH:MM."
        ) from exc
    return parsed


@dataclass
class Config:
    base_url: str
    token: str
    verify_ssl: bool
    aliases_file: Path
    telegram_bot_token: str
    telegram_owner_file: Path
    scenarios_file: Path
    alert_state_file: Path
    alert_poll_seconds: int
    alert_digest_time: time
    quiet_hours: QuietHours
    battery_warn: float
    battery_critical: float
    unavailable_warn_min: int
    alert_dedup_min: int
    timezone: str

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_runtime(
        cls,
        base_url: Optional[str],
        token: Optional[str],
        aliases: Optional[str],
    ) -> "Config":
        load_dotenv()

        resolved_base_url = (
            base_url or os.getenv("HA_BASE_URL", "http://homeassistant.local:8123")
        ).rstrip("/")
        resolved_token = (token or os.getenv("HA_TOKEN", "")).strip()

        if not resolved_token:
            raise AssistantError(
                "Отсутствует токен Home Assistant. Укажи HA_TOKEN в .env или передай --token."
            )

        verify_ssl = parse_bool(os.getenv("HA_VERIFY_SSL", "false"))
        aliases_file = Path(aliases or os.getenv("HA_ALIASES_FILE", "aliases.json"))

        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        telegram_owner_file = Path(os.getenv("TELEGRAM_OWNER_FILE", "bot_owner.json"))
        scenarios_file = Path(os.getenv("SCENARIOS_FILE", "scenarios.json"))
        alert_state_file = Path(os.getenv("ALERT_STATE_FILE", "alert_state.json"))

        alert_poll_seconds = max(10, parse_int(os.getenv("ALERT_POLL_SECONDS", "60"), 60))
        alert_digest_time = parse_hhmm(os.getenv("ALERT_DIGEST_TIME", "09:00"))
        quiet_hours = QuietHours.parse(os.getenv("QUIET_HOURS", "23:00-08:00"))

        battery_warn = parse_float(os.getenv("BATTERY_WARN", "30"), 30.0)
        battery_critical = parse_float(os.getenv("BATTERY_CRITICAL", "20"), 20.0)
        unavailable_warn_min = max(
            1,
            parse_int(os.getenv("UNAVAILABLE_WARN_MIN", "10"), 10),
        )
        alert_dedup_min = max(10, parse_int(os.getenv("ALERT_DEDUP_MIN", "360"), 360))

        if battery_critical >= battery_warn:
            raise AssistantError("BATTERY_CRITICAL должен быть меньше BATTERY_WARN.")

        timezone = os.getenv("TIMEZONE", "Europe/Minsk").strip() or "Europe/Minsk"

        return cls(
            base_url=resolved_base_url,
            token=resolved_token,
            verify_ssl=verify_ssl,
            aliases_file=aliases_file,
            telegram_bot_token=telegram_bot_token,
            telegram_owner_file=telegram_owner_file,
            scenarios_file=scenarios_file,
            alert_state_file=alert_state_file,
            alert_poll_seconds=alert_poll_seconds,
            alert_digest_time=alert_digest_time,
            quiet_hours=quiet_hours,
            battery_warn=battery_warn,
            battery_critical=battery_critical,
            unavailable_warn_min=unavailable_warn_min,
            alert_dedup_min=alert_dedup_min,
            timezone=timezone,
        )
