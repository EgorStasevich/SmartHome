"""External API clients (Home Assistant and Telegram)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

from .config import Config
from .errors import AssistantError
from .utils import chunk_text


class HomeAssistantClient:
    def __init__(self, config: Config) -> None:
        self.base_url = config.base_url
        self._headers = {
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            verify=config.verify_ssl,
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise AssistantError(f"Ошибка сети при запросе к HA: {exc}") from exc

        if response.status_code == 401:
            raise AssistantError("Токен отклонён (401). Проверь Long-Lived Access Token.")
        if response.status_code >= 400:
            message = response.text.strip() or response.reason_phrase
            raise AssistantError(
                f"Ошибка API Home Assistant ({response.status_code}): {message}"
            )

        if not response.text:
            return None

        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text

    async def get_states(self) -> List[Dict[str, Any]]:
        data = await self._request("GET", "/api/states")
        if not isinstance(data, list):
            raise AssistantError("Home Assistant вернул неожиданный формат /api/states.")
        return data

    async def get_state(self, entity_id: str) -> Dict[str, Any]:
        data = await self._request("GET", f"/api/states/{entity_id}")
        if not isinstance(data, dict):
            raise AssistantError("Home Assistant вернул неожиданный формат состояния.")
        return data

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        data = await self._request(
            "POST",
            f"/api/services/{domain}/{service}",
            json=service_data,
        )
        return data if isinstance(data, list) else []


class TelegramBotClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise AssistantError("TELEGRAM_BOT_TOKEN не задан. Невозможно запустить daemon.")
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}",
            timeout=45.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, json_payload: Dict[str, Any]) -> Any:
        try:
            response = await self._client.request(method, path, json=json_payload)
        except httpx.HTTPError as exc:
            raise AssistantError(f"Ошибка Telegram API: {exc}") from exc

        if response.status_code >= 400:
            raise AssistantError(
                f"Telegram API вернул {response.status_code}: {response.text.strip()}"
            )

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise AssistantError("Telegram API вернул не-JSON ответ.") from exc

        if not payload.get("ok", False):
            raise AssistantError(f"Telegram API error: {payload}")

        return payload.get("result")

    async def get_updates(self, offset: Optional[int], timeout: int = 25) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            body["offset"] = offset

        result = await self._request("POST", "/getUpdates", body)
        return result if isinstance(result, list) else []

    async def send_message(self, chat_id: int, text: str) -> None:
        for part in chunk_text(text):
            await self._request(
                "POST",
                "/sendMessage",
                {
                    "chat_id": chat_id,
                    "text": part,
                    "disable_web_page_preview": True,
                },
            )
