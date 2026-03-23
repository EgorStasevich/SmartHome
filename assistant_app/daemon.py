"""Daemon mode: monitoring loop + Telegram auto-notifications."""

from __future__ import annotations

import asyncio
import re
import signal
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .clients import TelegramBotClient
from .constants import DEFAULT_DENYLIST_PATTERNS, WORKING_UNAVAILABLE_DOMAINS
from .runtime import AssistantRuntime
from .utils import (
    format_local_timestamp,
    parse_iso_datetime,
)


class AssistantDaemon:
    def __init__(self, runtime: AssistantRuntime) -> None:
        self.runtime = runtime
        self.telegram = TelegramBotClient(runtime.config.telegram_bot_token)
        self._offset: Optional[int] = None
        self._stopping = asyncio.Event()
        self._last_entity_states: Dict[str, str] = {}
        self._activity_tracking_ready = False
        self._battery_levels: Dict[str, str] = {}
        self._unavailable_open: Dict[str, bool] = {}
        self._error_open: Dict[str, bool] = {}
        self._denylist_patterns = [re.compile(p, re.IGNORECASE) for p in DEFAULT_DENYLIST_PATTERNS]

    async def close(self) -> None:
        await self.telegram.close()

    async def run_forever(self) -> None:
        states = await self.runtime.refresh_states()
        self._prime_activity_tracking(states)
        self._prime_battery_tracking(states)
        print(
            f"Daemon запущен. HA={self.runtime.config.base_url}; "
            f"entities={len(self.runtime.index.states)}; poll={self.runtime.config.alert_poll_seconds}s"
        )

        monitor_task = asyncio.create_task(self._monitor_loop(), name="monitor-loop")
        telegram_task = asyncio.create_task(self._telegram_loop(), name="telegram-loop")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stopping.set)
            except NotImplementedError:
                pass

        await self._stopping.wait()

        for task in (monitor_task, telegram_task):
            task.cancel()
        await asyncio.gather(monitor_task, telegram_task, return_exceptions=True)

    async def _monitor_loop(self) -> None:
        while True:
            started = self.runtime.now()
            try:
                await self._monitor_once(started)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[monitor] ошибка: {exc}")

            elapsed = (self.runtime.now() - started).total_seconds()
            sleep_for = max(1, self.runtime.config.alert_poll_seconds - int(elapsed))
            await asyncio.sleep(sleep_for)

    async def _monitor_once(self, now: datetime) -> None:
        states = await self.runtime.refresh_states()
        state_map = self._build_state_map(states)

        health_messages = self._collect_health_notifications(states, state_map)
        for message in health_messages:
            await self._send_to_owner(message)

        activity_messages = self._collect_activity_notifications(states, state_map, now)
        for message in activity_messages:
            await self._send_to_owner(message)

        battery_messages = self._collect_battery_notifications(states)
        for message in battery_messages:
            await self._send_to_owner(message)

    async def _send_to_owner(self, text: str) -> bool:
        owner = self.runtime.owner_store.get()
        if not owner:
            return False

        chat_id = int(owner.get("chat_id"))
        try:
            await self.telegram.send_message(chat_id, text)
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[telegram] не удалось отправить сообщение владельцу: {exc}")
            return False

    def _prime_activity_tracking(self, states: List[Dict[str, Any]]) -> None:
        self._last_entity_states = self._build_state_map(states)
        self._activity_tracking_ready = True

    @staticmethod
    def _build_state_map(states: List[Dict[str, Any]]) -> Dict[str, str]:
        state_map: Dict[str, str] = {}
        for state in states:
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str) or "." not in entity_id:
                continue
            state_map[entity_id] = str(state.get("state", "")).strip().lower()
        return state_map

    @staticmethod
    def _safe_float(value: Optional[str]) -> Optional[float]:
        if value is None:
            return None
        text = str(value).strip().replace(",", ".")
        if not text or text.lower() in {"unknown", "unavailable", "none"}:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _prime_battery_tracking(self, states: List[Dict[str, Any]]) -> None:
        self._battery_levels = {}
        for state in states:
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str) or "." not in entity_id:
                continue
            attrs = state.get("attributes", {}) if isinstance(state.get("attributes"), dict) else {}
            raw_state = str(state.get("state", "")).strip().lower()
            friendly = str(attrs.get("friendly_name", entity_id))
            value = self._extract_battery_value(entity_id, raw_state, attrs, friendly)
            if value is None:
                continue
            self._battery_levels[entity_id] = self._battery_level(value)

    def _collect_battery_notifications(self, states: List[Dict[str, Any]]) -> List[str]:
        messages: List[str] = []
        seen_entities: set[str] = set()

        for state in states:
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str) or "." not in entity_id:
                continue

            attrs = state.get("attributes", {}) if isinstance(state.get("attributes"), dict) else {}
            raw_state = str(state.get("state", "")).strip().lower()
            friendly = str(attrs.get("friendly_name", entity_id))
            value = self._extract_battery_value(entity_id, raw_state, attrs, friendly)
            if value is None:
                continue

            seen_entities.add(entity_id)
            current_level = self._battery_level(value)
            previous_level = self._battery_levels.get(entity_id, "ok")
            self._battery_levels[entity_id] = current_level

            should_notify = False
            if current_level in {"warning", "critical"}:
                if previous_level == "ok":
                    should_notify = True
                elif previous_level == "warning" and current_level == "critical":
                    should_notify = True

            if not should_notify:
                continue

            device_name = self._clean_battery_device_name(friendly, entity_id)
            room_name = self._extract_room_name(state, entity_id, friendly)
            title = (
                "🚨 Критически низкий заряд"
                if current_level == "critical"
                else "🪫 Низкий заряд батареи"
            )

            lines = [
                title,
                f"📟 Устройство: {device_name}",
                f"📍 Комната: {room_name}",
                f"🔋 Заряд: {int(round(value))}%",
            ]
            messages.append("\n".join(lines))

        for entity_id in list(self._battery_levels.keys()):
            if entity_id not in seen_entities:
                self._battery_levels.pop(entity_id, None)

        return messages

    def _battery_level(self, value: float) -> str:
        if value < self.runtime.config.battery_critical:
            return "critical"
        if value < self.runtime.config.battery_warn:
            return "warning"
        return "ok"

    @staticmethod
    def _extract_battery_value(
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

    def _extract_room_name(self, state: Dict[str, Any], entity_id: str, friendly: str) -> str:
        attrs = state.get("attributes", {}) if isinstance(state.get("attributes"), dict) else {}

        for key in ("room_name", "room", "area_name", "area", "location"):
            value = attrs.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text and text.lower() not in {"unknown", "unavailable", "none"}:
                    return self._translate_room_name(text)

        marker = f"{friendly} {entity_id}".lower()
        token_map = {
            "kitchen": "Кухня",
            "кухн": "Кухня",
            "living room": "Гостиная",
            "гостин": "Гостиная",
            "hallway": "Коридор",
            "corridor": "Коридор",
            "корид": "Коридор",
            "bathroom": "Ванная",
            "ванн": "Ванная",
            "toilet": "Туалет",
            "туалет": "Туалет",
            "bedroom": "Спальня",
            "спальн": "Спальня",
            "children": "Детская",
            "детск": "Детская",
            "office": "Кабинет",
            "кабинет": "Кабинет",
            "balcony": "Балкон",
            "балкон": "Балкон",
        }
        for token, room_name in token_map.items():
            if token in marker:
                return room_name

        return "Не указана"

    @staticmethod
    def _clean_battery_device_name(friendly: str, entity_id: str) -> str:
        name = friendly.strip() or entity_id
        cleaned = re.sub(
            r"(?i)\b(battery level|battery|уровень батареи|батарея|заряд батареи)\b",
            "",
            name,
        )
        cleaned = re.sub(r"[\s\-_]{2,}", " ", cleaned).strip(" -_")
        return cleaned or name

    @staticmethod
    def _severity_emoji(severity: str) -> str:
        return "🚨" if severity == "critical" else "⚠️"

    def _collect_health_notifications(
        self,
        states: List[Dict[str, Any]],
        state_map: Dict[str, str],
    ) -> List[str]:
        messages: List[str] = []
        seen: set[str] = set()

        for state in states:
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str) or "." not in entity_id:
                continue
            seen.add(entity_id)

            if self._is_unavailable_candidate(entity_id):
                self._collect_unavailable_transition(messages, state, state_map.get(entity_id, ""))

            self._collect_error_transition(messages, state, state_map.get(entity_id, ""))

        for entity_id in list(self._unavailable_open.keys()):
            if entity_id not in seen:
                self._unavailable_open.pop(entity_id, None)
        for entity_id in list(self._error_open.keys()):
            if entity_id not in seen:
                self._error_open.pop(entity_id, None)

        return messages

    def _collect_unavailable_transition(
        self,
        messages: List[str],
        state: Dict[str, Any],
        raw_state: str,
    ) -> None:
        entity_id = str(state.get("entity_id", ""))
        is_unavailable = raw_state in {"unavailable", "unknown", "none"}
        is_open = self._unavailable_open.get(entity_id, False)

        attrs = state.get("attributes", {}) if isinstance(state.get("attributes"), dict) else {}
        friendly = self._friendly_name(state, entity_id)
        room_name = self._extract_room_name(state, entity_id, friendly)

        if is_unavailable and not is_open:
            self._unavailable_open[entity_id] = True
            lines = [
                "📡 Устройство недоступно",
                f"📟 Устройство: {friendly}",
                f"📍 Комната: {room_name}",
                f"⚠️ Статус: {raw_state}",
            ]
            messages.append("\n".join(lines))
            return

        if not is_unavailable and is_open:
            self._unavailable_open.pop(entity_id, None)
            lines = [
                "✅ Устройство снова на связи",
                f"📟 Устройство: {friendly}",
                f"📍 Комната: {room_name}",
                f"📶 Текущий статус: {raw_state}",
            ]
            messages.append("\n".join(lines))

    def _collect_error_transition(
        self,
        messages: List[str],
        state: Dict[str, Any],
        raw_state: str,
    ) -> None:
        entity_id = str(state.get("entity_id", ""))
        is_error, severity, reason = self._detect_entity_error(state, raw_state)
        is_open = self._error_open.get(entity_id, False)

        friendly = self._friendly_name(state, entity_id)
        room_name = self._extract_room_name(state, entity_id, friendly)

        if is_error and not is_open:
            self._error_open[entity_id] = True
            lines = [
                f"{self._severity_emoji(severity)} Ошибка устройства",
                f"📟 Устройство: {friendly}",
                f"📍 Комната: {room_name}",
                f"🔎 Ошибка: {reason}",
            ]
            messages.append("\n".join(lines))
            return

        if not is_error and is_open:
            self._error_open.pop(entity_id, None)
            lines = [
                "✅ Ошибка устройства устранена",
                f"📟 Устройство: {friendly}",
                f"📍 Комната: {room_name}",
                f"📶 Текущий статус: {raw_state}",
            ]
            messages.append("\n".join(lines))

    def _detect_entity_error(
        self,
        state: Dict[str, Any],
        raw_state: str,
    ) -> tuple[bool, str, str]:
        entity_id = str(state.get("entity_id", ""))
        attrs = state.get("attributes", {}) if isinstance(state.get("attributes"), dict) else {}
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""

        if raw_state in {"error", "failed", "fault", "problem"}:
            reason = self._extract_error_text(state) or f"state={raw_state}"
            severity = "critical" if raw_state in {"failed", "fault"} else "warning"
            return True, severity, reason

        if domain == "binary_sensor" and raw_state == "on":
            device_class = str(attrs.get("device_class", "")).strip().lower()
            if device_class in {"problem", "safety", "smoke", "gas", "moisture"}:
                reason = self._extract_error_text(state) or f"device_class={device_class}"
                severity = "critical" if device_class in {"safety", "smoke", "gas", "moisture"} else "warning"
                return True, severity, reason

        return False, "warning", ""

    def _extract_error_text(self, state: Dict[str, Any]) -> Optional[str]:
        attrs = state.get("attributes", {}) if isinstance(state.get("attributes"), dict) else {}

        hms_text = self._extract_hms_error_text(state)
        if hms_text:
            return hms_text

        for key in ("error", "error_message", "last_error", "message", "description", "problem"):
            value = attrs.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text and text.lower() not in {"unknown", "unavailable", "none"}:
                    return text

        return None

    def _is_unavailable_candidate(self, entity_id: str) -> bool:
        domain = entity_id.split(".", 1)[0]
        if domain not in WORKING_UNAVAILABLE_DOMAINS:
            return False
        return not self._is_denylisted(entity_id)

    def _is_denylisted(self, entity_id: str) -> bool:
        return any(pattern.search(entity_id) for pattern in self._denylist_patterns)

    @staticmethod
    def _format_hours(hours: float) -> str:
        total_minutes = max(0, int(round(hours * 60)))
        h = total_minutes // 60
        m = total_minutes % 60
        if h > 0 and m > 0:
            return f"{h} ч {m} мин"
        if h > 0:
            return f"{h} ч"
        return f"{m} мин"

    @staticmethod
    def _human_vacuum_state(state: str) -> str:
        mapping = {
            "cleaning": "идет уборка",
            "returning": "возвращается на базу",
            "paused": "уборка на паузе",
            "spot_cleaning": "локальная уборка",
            "mopping": "моет пол",
            "docked": "на базе",
            "idle": "ожидание",
            "error": "ошибка",
        }
        return mapping.get(state, state)

    @staticmethod
    def _human_print_state(state: str) -> str:
        mapping = {
            "running": "печатает",
            "prepare": "подготовка",
            "slicing": "обработка задания",
            "init": "инициализация",
            "pause": "на паузе",
            "finish": "завершено",
            "failed": "ошибка",
            "idle": "ожидание",
            "offline": "не в сети",
        }
        return mapping.get(state, state)

    @staticmethod
    def _translate_room_name(room: str) -> str:
        normalized = room.strip().lower()
        mapping = {
            "kitchen": "Кухня",
            "living room": "Гостиная",
            "hall": "Зал",
            "bathroom": "Ванная",
            "toilet": "Туалет",
            "bedroom": "Спальня",
            "children room": "Детская",
            "child room": "Детская",
            "kids room": "Детская",
            "corridor": "Коридор",
            "hallway": "Коридор",
            "balcony": "Балкон",
            "office": "Кабинет",
            "dining room": "Столовая",
            "guest room": "Гостевая",
        }
        return mapping.get(normalized, room)

    @staticmethod
    def _build_room_name_map(vacuum_attrs: Dict[str, Any]) -> Dict[int, str]:
        result: Dict[int, str] = {}
        rooms = vacuum_attrs.get("rooms")
        if not isinstance(rooms, dict):
            return result
        for value in rooms.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                room_id = item.get("id")
                room_name = item.get("name")
                if isinstance(room_id, int) and isinstance(room_name, str) and room_name.strip():
                    result[room_id] = room_name.strip()
        return result

    def _build_vacuum_scope_line(
        self,
        vacuum_attrs: Dict[str, Any],
        current_room_state: str,
    ) -> str:
        segment_cleaning = bool(vacuum_attrs.get("segment_cleaning"))
        active_segments = vacuum_attrs.get("active_segments")
        room_map = self._build_room_name_map(vacuum_attrs)

        if segment_cleaning and isinstance(active_segments, list) and active_segments:
            resolved: List[str] = []
            for segment in active_segments:
                if not isinstance(segment, int):
                    continue
                room_name = room_map.get(segment)
                if not room_name:
                    continue
                translated = self._translate_room_name(room_name)
                if translated not in resolved:
                    resolved.append(translated)

            if len(resolved) == 1:
                return f"📍 Комната: {resolved[0]}"
            if len(resolved) == 2:
                return f"📍 Комнаты: {resolved[0]} и {resolved[1]}"
            if len(resolved) > 2:
                preview = ", ".join(resolved[:3])
                suffix = f" +{len(resolved) - 3}" if len(resolved) > 3 else ""
                return f"📍 Комнаты: {preview}{suffix}"

        if current_room_state and current_room_state not in {"unknown", "unavailable", "none"}:
            return f"📍 Комната: {self._translate_room_name(current_room_state)}"

        return "📍 Комнаты: Вся квартира"

    @staticmethod
    def _select_entity(
        state_map: Dict[str, str],
        prefix: str,
        preferred: Optional[str] = None,
        suffix: Optional[str] = None,
    ) -> Optional[str]:
        if preferred and preferred in state_map:
            return preferred
        for entity_id in sorted(state_map.keys()):
            if not entity_id.startswith(prefix):
                continue
            if suffix and not entity_id.endswith(suffix):
                continue
            return entity_id
        return None

    @staticmethod
    def _state_by_entity(states: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for state in states:
            entity_id = state.get("entity_id")
            if isinstance(entity_id, str) and entity_id:
                result[entity_id] = state
        return result

    @staticmethod
    def _friendly_name(state: Optional[Dict[str, Any]], fallback: str) -> str:
        if not isinstance(state, dict):
            return fallback
        attrs = state.get("attributes", {}) if isinstance(state.get("attributes"), dict) else {}
        friendly = attrs.get("friendly_name")
        if isinstance(friendly, str) and friendly.strip():
            return friendly.strip()
        return fallback

    @staticmethod
    def _extract_hms_error_text(state: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(state, dict):
            return None

        attrs = state.get("attributes", {})
        if not isinstance(attrs, dict):
            return None

        numbered_errors: List[tuple[str, str]] = []
        for key, value in attrs.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if not key.endswith("-Error"):
                continue
            prefix = key.split("-", 1)[0]
            if not prefix.isdigit():
                continue
            text = value.strip()
            if not text or text.lower() in {"unknown", "unavailable", "none"}:
                continue
            numbered_errors.append((key, text))

        if numbered_errors:
            numbered_errors.sort(key=lambda item: item[0])
            return numbered_errors[0][1]

        for key in ("error", "message", "description"):
            value = attrs.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text and text.lower() not in {"unknown", "unavailable", "none"}:
                    return text

        return None

    def _collect_activity_notifications(
        self,
        states: List[Dict[str, Any]],
        state_map: Dict[str, str],
        now: datetime,
    ) -> List[str]:
        if not self._activity_tracking_ready:
            self._last_entity_states = state_map
            self._activity_tracking_ready = True
            return []

        states_by_id = self._state_by_entity(states)
        messages: List[str] = []
        messages.extend(self._collect_vacuum_notifications(states_by_id, state_map, now))
        messages.extend(self._collect_printer_notifications(states_by_id, state_map, now))

        self._last_entity_states = state_map
        return messages

    def _collect_vacuum_notifications(
        self,
        states_by_id: Dict[str, Dict[str, Any]],
        state_map: Dict[str, str],
        now: datetime,
    ) -> List[str]:
        vacuum_entity = self._select_entity(
            state_map,
            "vacuum.",
            preferred="vacuum.robocop_anthony",
        )
        if not vacuum_entity:
            return []

        previous = self._last_entity_states.get(vacuum_entity)
        current = state_map.get(vacuum_entity)
        if not previous or not current or previous == current:
            return []

        active_states = {"cleaning", "returning", "paused", "spot_cleaning", "mopping"}
        was_active = previous in active_states
        is_active = current in active_states

        vacuum_state = states_by_id.get(vacuum_entity)
        vacuum_name = self._friendly_name(vacuum_state, vacuum_entity)
        vacuum_slug = vacuum_entity.split(".", 1)[1]
        current_room_entity = f"sensor.{vacuum_slug}_current_room"
        area_entity = f"sensor.{vacuum_slug}_cleaned_area"
        time_entity = f"sensor.{vacuum_slug}_cleaning_time"
        total_time_entity = f"sensor.{vacuum_slug}_total_cleaning_time"
        count_entity = f"sensor.{vacuum_slug}_cleaning_count"

        if not was_active and is_active:
            room_state = state_map.get(current_room_entity, "")
            vacuum_attrs = (
                vacuum_state.get("attributes", {})
                if isinstance(vacuum_state, dict) and isinstance(vacuum_state.get("attributes"), dict)
                else {}
            )
            lines = [
                "▶️ Уборка началась",
                self._build_vacuum_scope_line(vacuum_attrs, room_state),
            ]

            total_minutes = self._safe_float(state_map.get(total_time_entity))
            cleaning_count = self._safe_float(state_map.get(count_entity))
            if total_minutes is not None and cleaning_count and cleaning_count > 0:
                avg_minutes = total_minutes / cleaning_count
                if avg_minutes > 1:
                    lines.append(f"⏱️ Примерное время уборки: {self._format_hours(avg_minutes / 60)}")
                    eta = now + timedelta(minutes=avg_minutes)
                    lines.append(f"🕒 Ориентировочно до: {eta.astimezone(self.runtime.config.tzinfo).strftime('%H:%M')}")
            return ["\n".join(lines)]

        if was_active and not is_active:
            lines = [
                "🧹 Дом · Уборка",
                "✅ Событие: завершение",
                f"🤖 Пылесос: {vacuum_name}",
                f"🏁 Итог: {self._human_vacuum_state(current)}",
            ]

            area_state = state_map.get(area_entity)
            area = self._safe_float(area_state)
            if area is not None:
                lines.append(f"📐 Площадь: {area:.1f} м²")

            cleaning_time_state = state_map.get(time_entity)
            cleaning_minutes = self._safe_float(cleaning_time_state)
            if cleaning_minutes is not None:
                lines.append(f"⏱️ Время уборки: {int(round(cleaning_minutes))} мин")

            return ["\n".join(lines)]

        return []

    def _collect_printer_notifications(
        self,
        states_by_id: Dict[str, Dict[str, Any]],
        state_map: Dict[str, str],
        now: datetime,
    ) -> List[str]:
        print_status_entity = self._select_entity(
            state_map,
            "sensor.",
            preferred="sensor.a1_03900d5a2809060_print_status",
            suffix="_print_status",
        )
        if not print_status_entity:
            return []

        previous = self._last_entity_states.get(print_status_entity)
        current = state_map.get(print_status_entity)
        if not previous or not current or previous == current:
            return []

        active_statuses = {"running", "prepare", "slicing", "init", "pause"}
        was_active = previous in active_statuses
        is_active = current in active_statuses

        prefix = print_status_entity[: -len("_print_status")]
        remaining_time_entity = f"{prefix}_remaining_time"
        end_time_entity = f"{prefix}_end_time"
        start_time_entity = f"{prefix}_start_time"
        progress_entity = f"{prefix}_print_progress"
        printer_slug = prefix.split(".", 1)[1]
        error_entity = f"binary_sensor.{printer_slug}_print_error"
        hms_error_entity = f"binary_sensor.{printer_slug}_hms_errors"

        hms_error_state = states_by_id.get(hms_error_entity)

        if not was_active and is_active:
            lines = [
                "🖨️ 3D печать",
                "▶️ Событие: печать началась",
                f"📊 Статус: {self._human_print_state(current)}",
            ]

            remaining_hours = self._safe_float(state_map.get(remaining_time_entity))
            if remaining_hours is not None and remaining_hours > 0:
                lines.append(f"⏱️ Оценка времени: {self._format_hours(remaining_hours)}")

            end_time_raw = state_map.get(end_time_entity, "")
            end_time = format_local_timestamp(end_time_raw, self.runtime.config.tzinfo)
            if end_time != "-":
                lines.append(f"🕒 Ориентировочно до: {end_time}")

            return ["\n".join(lines)]

        if was_active and not is_active:
            error_state = state_map.get(error_entity, "off")
            if current == "finish":
                title = "✅ печать завершена"
            elif current == "failed" or error_state == "on":
                title = "❌ печать завершилась с ошибкой"
            else:
                title = "⏹️ печать остановлена"

            lines = [
                "🖨️ 3D печать",
                f"Событие: {title}",
                f"🏁 Финальный статус: {self._human_print_state(current)}",
            ]

            start_raw = state_map.get(start_time_entity, "")
            end_dt = parse_iso_datetime(state_map.get(end_time_entity, "") or "")
            if end_dt is None:
                end_dt = now
            start_dt = parse_iso_datetime(start_raw)
            if start_dt:
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=self.runtime.config.tzinfo)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=self.runtime.config.tzinfo)
                delta = end_dt - start_dt
                if delta.total_seconds() > 0:
                    lines.append(f"⏱️ Длительность: {self._format_hours(delta.total_seconds() / 3600)}")

            progress = self._safe_float(state_map.get(progress_entity))
            if progress is not None:
                lines.append(f"📈 Прогресс: {int(round(progress))}%")

            if current == "failed" or error_state == "on":
                error_text = self._extract_hms_error_text(hms_error_state)
                if error_text:
                    lines.append(f"🔎 Ошибка: {error_text}")

            return ["\n".join(lines)]

        return []

    async def _telegram_loop(self) -> None:
        while True:
            try:
                updates = await self.telegram.get_updates(self._offset, timeout=25)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = update_id + 1
                    await self._handle_telegram_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[telegram] ошибка polling: {exc}")
                await asyncio.sleep(5)

    async def _handle_telegram_update(self, update: Dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return

        chat = message.get("chat", {}) if isinstance(message.get("chat"), dict) else {}
        user = message.get("from", {}) if isinstance(message.get("from"), dict) else {}

        chat_type = str(chat.get("type", ""))
        if chat_type != "private":
            return

        user_id = user.get("id")
        if not isinstance(user_id, int):
            return

        now = self.runtime.now()
        owner, claimed = self.runtime.owner_store.claim_first_user(user, chat, now)
        if claimed:
            await self.telegram.send_message(
                int(chat.get("id")),
                (
                    "Доступ активирован\n"
                    "Владелец бота успешно зафиксирован.\n"
                    "Только этот user_id имеет доступ к управлению домом."
                ),
            )

        if not self.runtime.owner_store.is_owner(user_id):
            await self.telegram.send_message(
                int(chat.get("id")),
                "Доступ запрещён\nБот уже привязан к другому user_id.",
            )
            return
        normalized = text.strip().lower()
        if normalized in {"/start", "/help"}:
            await self.telegram.send_message(
                int(owner.get("chat_id")),
                (
                    "Режим: только авто-уведомления\n"
                    "Команды управления и сценарии временно отключены.\n"
                    "Вы будете получать события уборки и 3D печати."
                ),
            )
