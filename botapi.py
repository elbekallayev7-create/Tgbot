"""Минимальный клиент Telegram Bot API — только стандартная библиотека, без установки пакетов."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request


class BotAPIError(Exception):
    def __init__(self, method: str, code: int, description: str, retry_after: int | None = None):
        super().__init__(f"{method}: [{code}] {description}")
        self.method, self.code, self.description, self.retry_after = method, code, description, retry_after


class BotAPI:
    def __init__(self, token: str, base_url: str = "https://api.telegram.org", timeout: float = 40):
        base = base_url.rstrip("/")
        self._url = f"{base}/bot{token}/"  # токен нигде не выводится в логи
        self._file_url = f"{base}/file/bot{token}/"
        self.timeout = timeout

    async def download(self, file_path: str, max_bytes: int) -> bytes:
        """Скачать файл по file_path из ответа getFile (Bot API отдаёт файлы до 20 МБ)."""
        return await asyncio.to_thread(self._download_sync, file_path, max_bytes)

    def _download_sync(self, file_path: str, max_bytes: int) -> bytes:
        url = self._file_url + urllib.parse.quote(file_path, safe="/")
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read(max_bytes + 1)
        except urllib.error.HTTPError as e:
            raise BotAPIError("download", e.code, f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BotAPIError("download", 0, f"нет связи: {e.__class__.__name__}") from None
        if len(data) > max_bytes:
            raise BotAPIError("download", 0, "файл слишком большой")
        return data

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
