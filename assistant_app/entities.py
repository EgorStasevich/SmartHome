"""Entity indexing and alias utilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import AssistantError
from .utils import normalize


class EntityIndex:
    def __init__(self, states: List[Dict[str, Any]], aliases: Dict[str, str]) -> None:
        self._states_by_entity: Dict[str, Dict[str, Any]] = {}
        self._friendly_to_entities: Dict[str, List[str]] = {}
        self._aliases: Dict[str, str] = {}
        self._all_aliases: Dict[str, str] = aliases
        self.reload(states, aliases)

    def reload(self, states: List[Dict[str, Any]], aliases: Dict[str, str]) -> None:
        self._states_by_entity = {}
        self._friendly_to_entities = {}
        self._aliases = {}
        self._all_aliases = aliases

        for state in states:
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str):
                continue

            self._states_by_entity[entity_id] = state
            attrs = state.get("attributes", {})
            if isinstance(attrs, dict):
                friendly_name = attrs.get("friendly_name")
                if isinstance(friendly_name, str) and friendly_name.strip():
                    key = normalize(friendly_name)
                    self._friendly_to_entities.setdefault(key, []).append(entity_id)

        for alias, entity_id in aliases.items():
            alias_key = normalize(alias)
            if entity_id in self._states_by_entity:
                self._aliases[alias_key] = entity_id

    @property
    def states(self) -> Dict[str, Dict[str, Any]]:
        return self._states_by_entity

    def resolve(
        self,
        user_target: str,
        allowed_domains: Optional[Iterable[str]] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        target = normalize(user_target)
        if not target:
            return None, "Пустая цель команды."

        domain_filter = set(allowed_domains or [])

        def domain_ok(entity_id: str) -> bool:
            if not domain_filter:
                return True
            domain = entity_id.split(".", 1)[0]
            return domain in domain_filter

        if target in self._aliases:
            entity_id = self._aliases[target]
            if domain_ok(entity_id):
                return entity_id, None

        if target in self._states_by_entity:
            if domain_ok(target):
                return target, None
            return None, "Устройство найдено, но его домен не подходит для команды."

        friendly_exact = self._friendly_to_entities.get(target, [])
        friendly_exact = [item for item in friendly_exact if domain_ok(item)]
        if len(friendly_exact) == 1:
            return friendly_exact[0], None
        if len(friendly_exact) > 1:
            preview = ", ".join(sorted(friendly_exact[:5]))
            return None, f"Найдено несколько устройств: {preview}. Уточни название."

        candidates: List[str] = []
        for friendly_name, entity_ids in self._friendly_to_entities.items():
            if target in friendly_name:
                for entity_id in entity_ids:
                    if domain_ok(entity_id):
                        candidates.append(entity_id)

        candidates = sorted(set(candidates))
        if len(candidates) == 1:
            return candidates[0], None
        if len(candidates) > 1:
            preview = ", ".join(candidates[:5])
            return None, f"Нашлось несколько вариантов: {preview}. Уточни цель."

        return None, f"Устройство '{user_target}' не найдено."

    def list_entities(self, domain: Optional[str] = None) -> List[str]:
        entity_ids = sorted(self._states_by_entity.keys())
        if domain:
            prefix = f"{domain.lower().strip()}."
            entity_ids = [entity_id for entity_id in entity_ids if entity_id.startswith(prefix)]
        return entity_ids

    def guess_default_climate(self) -> Optional[str]:
        climates = self.list_entities("climate")
        if len(climates) == 1:
            return climates[0]
        return None


def read_aliases(file_path: Path) -> Dict[str, str]:
    if not file_path.exists():
        return {}

    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssistantError(f"Не удалось разобрать aliases файл: {exc}") from exc

    if not isinstance(payload, dict):
        raise AssistantError("aliases файл должен быть JSON-объектом {alias: entity_id}.")

    clean_aliases: Dict[str, str] = {}
    for alias, entity_id in payload.items():
        if isinstance(alias, str) and isinstance(entity_id, str):
            clean_aliases[alias] = entity_id
    return clean_aliases


def format_state_info(state: Dict[str, Any]) -> str:
    entity_id = state.get("entity_id", "<unknown>")
    value = state.get("state", "<unknown>")
    attrs = state.get("attributes", {}) if isinstance(state.get("attributes"), dict) else {}
    friendly_name = attrs.get("friendly_name", entity_id)
    unit = attrs.get("unit_of_measurement", "")
    display = f"{value} {unit}".strip()
    return f"{friendly_name} ({entity_id}): {display}"
