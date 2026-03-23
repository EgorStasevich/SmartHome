"""Persistent stores and monitoring logic primitives."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .constants import DEFAULT_DENYLIST_PATTERNS, WORKING_UNAVAILABLE_DOMAINS
from .errors import AssistantError
from .utils import atomic_write_json, now_iso, parse_iso_datetime


@dataclass
class Issue:
    key: str
    severity: str
    title: str
    details: str
    entity_id: str
    first_seen: str
    last_notified: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "severity": self.severity,
            "title": self.title,
            "details": self.details,
            "entity_id": self.entity_id,
            "first_seen": self.first_seen,
            "last_notified": self.last_notified,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Issue":
        return cls(
            key=str(payload.get("key", "")),
            severity=str(payload.get("severity", "warning")),
            title=str(payload.get("title", "")),
            details=str(payload.get("details", "")),
            entity_id=str(payload.get("entity_id", "")),
            first_seen=str(payload.get("first_seen", "")),
            last_notified=payload.get("last_notified"),
        )


@dataclass
class AlertRuntimeState:
    open_issues: Dict[str, Issue]
    unavailable_since: Dict[str, str]
    last_digest_date: Optional[str]

    @classmethod
    def default(cls) -> "AlertRuntimeState":
        return cls(open_issues={}, unavailable_since={}, last_digest_date=None)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "AlertRuntimeState":
        open_payload = payload.get("open_issues", {}) if isinstance(payload, dict) else {}
        unavailable_payload = (
            payload.get("unavailable_since", {}) if isinstance(payload, dict) else {}
        )
        digest_date = payload.get("last_digest_date") if isinstance(payload, dict) else None

        open_issues: Dict[str, Issue] = {}
        if isinstance(open_payload, dict):
            for key, value in open_payload.items():
                if isinstance(value, dict):
                    issue = Issue.from_dict({**value, "key": key})
                    if issue.key:
                        open_issues[issue.key] = issue

        unavailable_since: Dict[str, str] = {}
        if isinstance(unavailable_payload, dict):
            for key, value in unavailable_payload.items():
                if isinstance(key, str) and isinstance(value, str):
                    unavailable_since[key] = value

        return cls(
            open_issues=open_issues,
            unavailable_since=unavailable_since,
            last_digest_date=digest_date if isinstance(digest_date, str) else None,
        )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "open_issues": {key: issue.to_dict() for key, issue in self.open_issues.items()},
            "unavailable_since": self.unavailable_since,
            "last_digest_date": self.last_digest_date,
        }


class AlertStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AlertRuntimeState:
        if not self.path.exists():
            return AlertRuntimeState.default()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AlertRuntimeState.default()
        if not isinstance(payload, dict):
            return AlertRuntimeState.default()
        return AlertRuntimeState.from_payload(payload)

    def save(self, state: AlertRuntimeState) -> None:
        atomic_write_json(self.path, state.to_payload())


class OwnerStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._owner = self._load_owner()

    def _load_owner(self) -> Optional[Dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        required = {"user_id", "chat_id", "first_seen_at"}
        if not required.issubset(payload.keys()):
            return None
        return payload

    def get(self) -> Optional[Dict[str, Any]]:
        return self._owner

    def claim_first_user(
        self,
        user: Dict[str, Any],
        chat: Dict[str, Any],
        now: datetime,
    ) -> Tuple[Dict[str, Any], bool]:
        if self._owner:
            return self._owner, False

        username = user.get("username")
        if not isinstance(username, str) or not username.strip():
            username = user.get("first_name") or "unknown"

        owner = {
            "version": 1,
            "user_id": int(user.get("id")),
            "chat_id": int(chat.get("id")),
            "username": str(username),
            "first_seen_at": now_iso(now),
        }
        self._owner = owner
        atomic_write_json(self.path, owner)
        return owner, True

    def is_owner(self, user_id: int) -> bool:
        if not self._owner:
            return False
        return int(self._owner.get("user_id")) == int(user_id)

    def reset(self) -> bool:
        self._owner = None
        if self.path.exists():
            self.path.unlink()
            return True
        return False


class ScenarioStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = self._load()

    def _default(self) -> Dict[str, Any]:
        return {"version": 1, "scenarios": []}

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return self._default()

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._default()

        if not isinstance(payload, dict):
            return self._default()

        scenarios = payload.get("scenarios", [])
        if not isinstance(scenarios, list):
            scenarios = []

        clean_scenarios: List[Dict[str, Any]] = []
        for item in scenarios:
            if not isinstance(item, dict):
                continue
            scenario_id = item.get("id")
            name = item.get("name")
            enabled = item.get("enabled", True)
            updated_at = item.get("updated_at")
            steps = item.get("steps", [])
            if not isinstance(scenario_id, str) or not isinstance(name, str):
                continue
            if not isinstance(enabled, bool):
                enabled = True
            if not isinstance(updated_at, str):
                updated_at = ""
            if not isinstance(steps, list):
                steps = []
            clean_scenarios.append(
                {
                    "id": scenario_id,
                    "name": name,
                    "enabled": enabled,
                    "updated_at": updated_at,
                    "steps": steps,
                }
            )

        return {"version": 1, "scenarios": clean_scenarios}

    def save(self) -> None:
        atomic_write_json(self.path, self._data)

    def list_scenarios(self) -> List[Dict[str, Any]]:
        scenarios = self._data.get("scenarios", [])
        if not isinstance(scenarios, list):
            return []
        return sorted(scenarios, key=lambda item: str(item.get("id", "")))

    def _find_index(self, scenario_id: str) -> Optional[int]:
        for idx, item in enumerate(self._data.get("scenarios", [])):
            if item.get("id") == scenario_id:
                return idx
        return None

    def get_scenario(self, scenario_id: str) -> Optional[Dict[str, Any]]:
        idx = self._find_index(scenario_id)
        if idx is None:
            return None
        return self._data["scenarios"][idx]

    def create_scenario(self, scenario_id: str, name: str, now: datetime) -> Dict[str, Any]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", scenario_id):
            raise AssistantError(
                "ID сценария должен содержать только буквы, цифры, '_' и '-'."
            )
        if self.get_scenario(scenario_id):
            raise AssistantError(f"Сценарий '{scenario_id}' уже существует.")

        scenario = {
            "id": scenario_id,
            "name": name.strip() or scenario_id,
            "enabled": True,
            "updated_at": now_iso(now),
            "steps": [],
        }
        self._data.setdefault("scenarios", []).append(scenario)
        self.save()
        return scenario

    def delete_scenario(self, scenario_id: str) -> None:
        idx = self._find_index(scenario_id)
        if idx is None:
            raise AssistantError(f"Сценарий '{scenario_id}' не найден.")
        del self._data["scenarios"][idx]
        self.save()

    def add_step(self, scenario_id: str, step: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        scenario = self.get_scenario(scenario_id)
        if not scenario:
            raise AssistantError(f"Сценарий '{scenario_id}' не найден.")
        steps = scenario.setdefault("steps", [])
        if not isinstance(steps, list):
            steps = []
            scenario["steps"] = steps
        steps.append(step)
        scenario["updated_at"] = now_iso(now)
        self.save()
        return scenario

    def remove_step(self, scenario_id: str, index_1based: int, now: datetime) -> Dict[str, Any]:
        scenario = self.get_scenario(scenario_id)
        if not scenario:
            raise AssistantError(f"Сценарий '{scenario_id}' не найден.")

        steps = scenario.get("steps", [])
        if not isinstance(steps, list) or not steps:
            raise AssistantError("В сценарии нет шагов.")

        idx = index_1based - 1
        if idx < 0 or idx >= len(steps):
            raise AssistantError(f"Неверный индекс шага: {index_1based}.")

        steps.pop(idx)
        scenario["updated_at"] = now_iso(now)
        self.save()
        return scenario


class ProblemDetector:
    unavailable_states = {"unavailable", "unknown", "none"}

    def __init__(self, config: Config) -> None:
        self.config = config
        self._denylist = [re.compile(pattern, re.IGNORECASE) for pattern in DEFAULT_DENYLIST_PATTERNS]

    def detect(
        self,
        states: List[Dict[str, Any]],
        now: datetime,
        previous_unavailable_since: Dict[str, str],
    ) -> Tuple[Dict[str, Issue], Dict[str, str]]:
        issues: Dict[str, Issue] = {}
        unavailable_since: Dict[str, str] = {}

        for state in states:
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str) or "." not in entity_id:
                continue

            raw_state = str(state.get("state", "")).strip().lower()
            domain = entity_id.split(".", 1)[0]
            attrs = state.get("attributes", {}) if isinstance(state.get("attributes"), dict) else {}
            friendly = str(attrs.get("friendly_name", entity_id))

            self._collect_binary_sensor_issues(issues, entity_id, raw_state, attrs, friendly)
            self._collect_battery_issues(issues, entity_id, raw_state, attrs, friendly, now)

            if (
                domain in WORKING_UNAVAILABLE_DOMAINS
                and raw_state in self.unavailable_states
                and not self._is_denylisted(entity_id)
            ):
                first_seen_iso = previous_unavailable_since.get(entity_id, now_iso(now))
                unavailable_since[entity_id] = first_seen_iso

                first_seen_dt = parse_iso_datetime(first_seen_iso) or now
                elapsed = now - first_seen_dt
                if elapsed >= timedelta(minutes=self.config.unavailable_warn_min):
                    key = f"unavailable:{entity_id}"
                    issues[key] = Issue(
                        key=key,
                        severity="warning",
                        title=f"Устройство недоступно: {friendly}",
                        details=(
                            f"{entity_id} находится в состоянии '{raw_state}' "
                            f"уже {int(elapsed.total_seconds() // 60)} мин."
                        ),
                        entity_id=entity_id,
                        first_seen=first_seen_iso,
                        last_notified=None,
                    )

        return issues, unavailable_since

    def _collect_binary_sensor_issues(
        self,
        issues: Dict[str, Issue],
        entity_id: str,
        raw_state: str,
        attrs: Dict[str, Any],
        friendly: str,
    ) -> None:
        if not entity_id.startswith("binary_sensor."):
            return
        if raw_state != "on":
            return

        device_class = str(attrs.get("device_class", "")).strip().lower()

        if device_class in {"moisture", "safety"}:
            key = f"critical_binary:{entity_id}"
            issues[key] = Issue(
                key=key,
                severity="critical",
                title=f"Критичный датчик сработал: {friendly}",
                details=f"{entity_id} (device_class={device_class}) перешёл в состояние ON.",
                entity_id=entity_id,
                first_seen="",
                last_notified=None,
            )
            return

        if device_class == "problem":
            severity = "critical" if self._is_critical_problem(entity_id, friendly) else "warning"
            key = f"problem_binary:{entity_id}"
            issues[key] = Issue(
                key=key,
                severity=severity,
                title=f"Проблема устройства: {friendly}",
                details=f"{entity_id} сообщает о проблеме (state=on).",
                entity_id=entity_id,
                first_seen="",
                last_notified=None,
            )

    def _collect_battery_issues(
        self,
        issues: Dict[str, Issue],
        entity_id: str,
        raw_state: str,
        attrs: Dict[str, Any],
        friendly: str,
        now: datetime,
    ) -> None:
        value = self._extract_battery_value(entity_id, raw_state, attrs, friendly)
        if value is None:
            return

        if value < self.config.battery_critical:
            severity = "critical"
            level = "критический"
        elif value < self.config.battery_warn:
            severity = "warning"
            level = "низкий"
        else:
            return

        key = f"battery:{entity_id}"
        issues[key] = Issue(
            key=key,
            severity=severity,
            title=f"{level.capitalize()} заряд: {friendly}",
            details=f"{entity_id}: {value:.1f}%.",
            entity_id=entity_id,
            first_seen=now_iso(now),
            last_notified=None,
        )

    def _extract_battery_value(
        self,
        entity_id: str,
        raw_state: str,
        attrs: Dict[str, Any],
        friendly: str,
    ) -> Optional[float]:
        device_class = str(attrs.get("device_class", "")).strip().lower()
        unit = str(attrs.get("unit_of_measurement", "")).strip().lower()
        marker = f"{entity_id} {friendly}".lower()
        battery_like = (
            device_class == "battery"
            or "battery" in marker
            or "batare" in marker
            or "батар" in marker
            or "заряд" in marker
            or "zariad" in marker
        )

        if not battery_like:
            return None

        match = re.search(r"-?\d+(?:[.,]\d+)?", raw_state)
        if not match:
            return None

        value = float(match.group(0).replace(",", "."))
        if unit and unit not in {"%", "percent"} and device_class != "battery":
            return None
        if value < 0 or value > 100:
            return None
        return value

    def _is_critical_problem(self, entity_id: str, friendly: str) -> bool:
        marker = f"{entity_id} {friendly}".lower()
        critical_tokens = [
            "rpi_power_status",
            "протеч",
            "leak",
            "smoke",
            "gas",
            "авар",
            "power",
            "питани",
            "error",
            "ошиб",
        ]
        return any(token in marker for token in critical_tokens)

    def _is_denylisted(self, entity_id: str) -> bool:
        return any(pattern.search(entity_id) for pattern in self._denylist)
