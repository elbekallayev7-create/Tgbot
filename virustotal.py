"""Клиент VirusTotal API v3 — только стандартная библиотека.

Бесплатный (публичный) ключ: 4 запроса в минуту и 500 в сутки, только для некоммерческого использования.
Клиент сам выдерживает паузу между запросами (min_interval), поэтому лимит минуты не превышается.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
import uuid


class VTError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(f"VirusTotal [{code}] {description}")
        self.code, self.description = code, description


def sum_engines(stats: dict) -> int:
    """Сколько антивирусов дали вердикт."""
    return sum(stats.get(k, 0) for k in ("malicious", "suspicious", "undetected", "harmless"))


def summarize(stats: dict) -> tuple[str, str]:
    """(уровень, текст): ok / warn / bad / unknown."""
    malicious, suspicious, total = stats.get("malicious", 0), stats.get("suspicious", 0), sum_engines(stats)
    if total == 0:
        return "unknown", "❔ Результата пока нет"
    if malicious >= 3:
        return "bad", f"🚫 Вредоносный: {malicious} из {total} антивирусов. НЕ открывайте файл"
    if malicious or suspicious:
        return "warn", (f"⚠️ Подозрительный: {malicious + suspicious} из {total} "
                        "(возможно, ложное срабатывание). Лучше не открывать")
    return "ok", f"✅ Угроз не найдено (0 из {total})"


def report_url(sha256: str) -> str:
    return f"https://www.virustotal.com/gui/file/{sha256}"


def _multipart(name: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----tgsecretary" + uuid.uuid4().hex
    safe = name.replace('"', "'").replace("\r", " ").replace("\n", " ")[:200] or "file"
    safe = safe.encode("ascii", "replace").decode()  # для VirusTotal важен хэш, а не имя
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    return head + data + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


class VirusTotal:
    def __init__(self, api_key: str, base_url: str = "https://www.virustotal.com/api/v3",
                 min_interval: float = 16.0):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._interval = min_interval
        self._lock: asyncio.Lock | None = None
        self._last = 0.0

    async def _request(self, method: str, path: str, body: bytes | None = None,
                       content_type: str | None = None, timeout: float = 90):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:  # запросы идут по одному и не чаще раза в min_interval секунд
            wait = self._last + self._interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await asyncio.to_thread(self._sync, method, path, body, content_type, timeout)
            finally:
                self._last = time.monotonic()

    def _sync(self, method, path, body, content_type, timeout):
        headers = {"x-apikey": self._key, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self._base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            try:
                error = json.loads(e.read()).get("error", {})
                description = error.get("message") or error.get("code") or ""
            except ValueError:
                description = ""
            raise VTError(e.code, description or f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise VTError(0, f"нет связи: {e.__class__.__name__}") from None
        except ValueError:
            raise VTError(0, "ответ не похож на JSON") from None

    async def lookup(self, sha256: str) -> dict | None:
        """Что VirusTotal знает о файле. None — файл ему не знаком (загружать не нужно, пока не решите)."""
        try:
            data = await self._request("GET", f"/files/{sha256}")
        except VTError as e:
            if e.code == 404:
                return None
            raise
        return {"stats": data["data"]["attributes"].get("last_analysis_stats") or {}}

    async def upload(self, name: str, data: bytes) -> str:
        """Загрузить файл (до 32 МБ), вернуть id анализа."""
        body, content_type = _multipart(name, data)
        result = await self._request("POST", "/files", body, content_type, timeout=120)
        return result["data"]["id"]

    async def wait_analysis(self, analysis_id: str, interval: float = 20.0, attempts: int = 9) -> dict | None:
        """Дождаться результата (до ~3 минут). None — не успел."""
        for _ in range(attempts):
            result = await self._request("GET", f"/analyses/{analysis_id}")
            attrs = result["data"]["attributes"]
            if attrs.get("status") == "completed":
                return attrs.get("stats") or {}
            await asyncio.sleep(interval)
        return None
