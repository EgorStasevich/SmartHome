"""Runtime state and local (CLI-style) command execution."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Dict, List, Tuple

from .clients import HomeAssistantClient
from .config import Config
from .constants import HELP_TEXT
from .entities import EntityIndex, format_state_info, read_aliases
from .errors import AssistantError
from .stores import AlertStateStore, OwnerStore, ProblemDetector, ScenarioStore
from .utils import normalize


class AssistantRuntime:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = HomeAssistantClient(config)
        self.aliases = read_aliases(config.aliases_file)
        self.index = EntityIndex([], self.aliases)
        self.states: List[Dict[str, Any]] = []
        self.state_lock = asyncio.Lock()

        self.scenarios = ScenarioStore(config.scenarios_file)
        self.owner_store = OwnerStore(config.telegram_owner_file)
        self.alert_store = AlertStateStore(config.alert_state_file)
        self.alert_state = self.alert_store.load()
        self.detector = ProblemDetector(config)

    async def close(self) -> None:
        await self.client.close()

    def now(self) -> datetime:
        return datetime.now(tz=self.config.tzinfo)

    async def refresh_states(self) -> List[Dict[str, Any]]:
        states = await self.client.get_states()
        async with self.state_lock:
            self.states = states
            self.index.reload(states, self.aliases)
        return states

    async def get_snapshot(self) -> Tuple[List[Dict[str, Any]], EntityIndex]:
        async with self.state_lock:
            return list(self.states), self.index

    def save_alert_state(self) -> None:
        self.alert_store.save(self.alert_state)

    async def run_assistant_scenario(self, scenario: Dict[str, Any]) -> str:
        steps = scenario.get("steps", [])
        if not isinstance(steps, list) or not steps:
            raise AssistantError("В сценарии нет шагов для выполнения.")

        await self.refresh_states()
        _, index = await self.get_snapshot()

        executed = 0
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                raise AssistantError(f"Шаг #{idx} имеет неверный формат.")

            step_type = str(step.get("type", "")).strip().lower()

            try:
                if step_type in {"on", "off"}:
                    entity_id = str(step.get("entity_id", "")).strip()
                    if entity_id not in index.states:
                        raise AssistantError(f"Шаг #{idx}: устройство {entity_id} не найдено.")
                    domain = entity_id.split(".", 1)[0]
                    service = "turn_on" if step_type == "on" else "turn_off"
                    await self.client.call_service(domain, service, {"entity_id": entity_id})

                elif step_type == "temp":
                    entity_id = str(step.get("entity_id", "")).strip()
                    value = float(step.get("value"))
                    if not entity_id.startswith("climate."):
                        raise AssistantError(
                            f"Шаг #{idx}: {entity_id} не является climate сущностью."
                        )
                    await self.client.call_service(
                        "climate",
                        "set_temperature",
                        {"entity_id": entity_id, "temperature": value},
                    )

                elif step_type == "delay":
                    seconds = int(step.get("seconds"))
                    if seconds <= 0:
                        raise AssistantError(f"Шаг #{idx}: delay должен быть > 0.")
                    await asyncio.sleep(seconds)

                elif step_type == "script":
                    script_id = str(step.get("entity_id", "")).strip()
                    if not script_id.startswith("script."):
                        raise AssistantError(
                            f"Шаг #{idx}: {script_id} не является script сущностью."
                        )
                    await self.client.call_service(
                        "script",
                        "turn_on",
                        {"entity_id": script_id},
                    )

                else:
                    raise AssistantError(f"Шаг #{idx}: неизвестный тип '{step_type}'.")

                executed += 1
            except AssistantError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise AssistantError(f"Шаг #{idx} завершился с ошибкой: {exc}") from exc

        return f"Сценарий '{scenario.get('id')}' выполнен. Шагов: {executed}."


async def execute_local_command(text: str, runtime: AssistantRuntime) -> str:
    command = normalize(text)
    if not command:
        return ""

    if command in {"help", "помощь", "?"}:
        return HELP_TEXT

    if command in {"выход", "exit", "quit"}:
        return "__EXIT__"

    await runtime.refresh_states()
    _, index = await runtime.get_snapshot()

    if command == "обнови":
        return f"Список устройств обновлён. Всего сущностей: {len(index.states)}."

    match = re.match(r"^(включи|выключи)\s+(.+)$", command, flags=re.IGNORECASE)
    if match:
        verb = match.group(1)
        target_raw = match.group(2).strip()
        entity_id, error = index.resolve(target_raw)
        if error:
            return error
        assert entity_id is not None
        domain = entity_id.split(".", 1)[0]
        service = "turn_on" if verb == "включи" else "turn_off"
        await runtime.client.call_service(domain, service, {"entity_id": entity_id})
        return f"Готово: {entity_id} -> {service}."

    match = re.match(
        r"^(установи|поставь)\s+температур[ауы]\s+(-?\d+(?:[.,]\d+)?)\s*(?:в|на)?\s*(.*)$",
        command,
        flags=re.IGNORECASE,
    )
    if match:
        raw_value = match.group(2).replace(",", ".")
        target_raw = match.group(3).strip()

        try:
            value = float(raw_value)
        except ValueError as exc:
            raise AssistantError(f"Неверное значение температуры: {raw_value}") from exc

        if target_raw:
            entity_id, error = index.resolve(target_raw, allowed_domains={"climate"})
            if error:
                return error
            assert entity_id is not None
        else:
            guessed = index.guess_default_climate()
            if not guessed:
                return (
                    "Не удалось выбрать климат-устройство автоматически. "
                    "Укажи его явно, например: установи температуру 22 в climate.office"
                )
            entity_id = guessed

        await runtime.client.call_service(
            "climate",
            "set_temperature",
            {"entity_id": entity_id, "temperature": value},
        )
        return f"Установлена температура {value:.1f}°C для {entity_id}."

    match = re.match(r"^(сцена|активируй)\s+(.+)$", command, flags=re.IGNORECASE)
    if match:
        target_raw = match.group(2).strip()
        entity_id, error = index.resolve(target_raw, allowed_domains={"scene"})
        if error:
            return error
        assert entity_id is not None
        await runtime.client.call_service("scene", "turn_on", {"entity_id": entity_id})
        return f"Сцена активирована: {entity_id}."

    match = re.match(r"^(статус|состояние|проверь)\s+(.+)$", command, flags=re.IGNORECASE)
    if match:
        target_raw = match.group(2).strip()
        entity_id, error = index.resolve(target_raw)
        if error:
            return error
        assert entity_id is not None
        state = await runtime.client.get_state(entity_id)
        return format_state_info(state)

    match = re.match(r"^(список|list)(?:\s+([a-z_]+))?$", command, flags=re.IGNORECASE)
    if match:
        domain = (match.group(2) or "").strip() or None
        entities = index.list_entities(domain)
        if not entities:
            return "Ничего не найдено."
        preview = "\n".join(entities[:80])
        suffix = "" if len(entities) <= 80 else f"\n... и ещё {len(entities) - 80}"
        return f"Найдено {len(entities)} сущностей:\n{preview}{suffix}"

    return "Не понял команду. Напиши 'помощь', чтобы увидеть список команд."
