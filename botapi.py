"""Минимальный клиент Telegram Bot API — только стандартная библиотека, без установки пакетов."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request


class BotAPIError(Exception):
    def __init__(self, method: str, code: int, description: str, retry_after: int | None = None):
        super().__init__(f"{method}: [{code}] {description}")
        self.method, self.code, self.description, self.retry_after = method, code, description, retry_after


class BotAPI:
    def __init__(self, token: str, base_url: str = "https://api.telegram.org", timeout: float = 40):
        self._url = f"{base_url.rstrip('/')}/bot{token}/"  # токен нигде не выводится в логи
        self.timeout = timeout

    async def call(self, method: str, params: dict | None = None, timeout: float | None = None):
        """Вызвать метод Bot API и вернуть result (или поднять BotAPIError)."""
        return await asyncio.to_thread(self._call_sync, method, params or {}, timeout or self.timeout)

    def _call_sync(self, method: str, params: dict, timeout: float):
        request = urllib.request.Request(
            self._url + method,
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as e:  # Telegram отвечает JSON и на ошибки
            try:
                body = json.loads(e.read())
            except ValueError:
                raise BotAPIError(method, e.code, f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BotAPIError(method, 0, f"нет связи: {e.__class__.__name__}") from None
        except ValueError:
            raise BotAPIError(method, 0, "ответ не похож на JSON") from None

        if not body.get("ok"):
            retry_after = (body.get("parameters") or {}).get("retry_after")
            raise BotAPIError(method, body.get("error_code", 0), body.get("description", ""), retry_after)
        return body["result"]
