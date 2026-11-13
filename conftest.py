"""Общие настройки тестов: имитатор Telegram Bot API и VirusTotal на локальном порту."""

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["STATE_FILE"] = str(Path(tempfile.mkdtemp()) / "state.json")
os.environ["REPLY_DELAY_MIN"] = "0"
os.environ["REPLY_DELAY_MAX"] = "0"
os.environ["COOLDOWN_HOURS"] = "6"
os.environ["HANDOFF_MINUTES"] = "30"
for name in ("AWAY_FROM", "AWAY_TO", "IDLE_MINUTES", "OWNER_ID", "OWNER_USERNAME", "MODE", "VT_API_KEY"):
    os.environ.pop(name, None)

import pytest

import secretary as ar
from botapi import BotAPI
from triggers import Config
from virustotal import VirusTotal

CONFIG_FILE = Path(__file__).with_name("config.json")
OWNER = {"id": 1, "first_name": "Owner", "is_bot": False}
CONN = {"id": "c1", "user": OWNER, "user_chat_id": 1, "date": 0, "can_reply": True, "is_enabled": True}
CLEAN = {"malicious": 0, "suspicious": 0, "undetected": 60, "harmless": 10}
EVIL = {"malicious": 12, "suspicious": 1, "undetected": 50, "harmless": 7}


class FakeTelegram:
    """Имитирует Telegram (/bot…/метод, /file/bot…/путь) и VirusTotal (/api/v3/…)."""

    def __init__(self):
        self.calls, self.batches, self.connections, self.fail = [], [], {}, {}
        self.paths = {}                  # file_path -> байты файла
        self.vt_known = {}               # sha256 -> stats
        self.vt_uploads = []             # байты загруженных файлов
        self.vt_polls_before_done = 0    # сколько раз анализ «в очереди»
        self.vt_upload_stats = CLEAN
        self.vt_fail = {}                # "lookup"/"upload"/"analysis" -> (код, сообщение)
        self.vt_key_seen = set()
        self._polls = 0
        self._next_id = 100
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, payload=None, raw=None):
                body = raw if raw is not None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _vt_error(self, key):
                code, message = outer.vt_fail[key]
                self._send(code, {"error": {"code": "Error", "message": message}})

            def do_GET(self):
                if self.path.startswith("/file/bot"):
                    data = outer.paths.get(self.path.split("/", 3)[3])
                    return self._send(200, raw=data) if data is not None else self._send(404, {})
                outer.vt_key_seen.add(self.headers.get("x-apikey"))
                if self.path.startswith("/api/v3/files/"):
                    if "lookup" in outer.vt_fail:
                        return self._vt_error("lookup")
                    stats = outer.vt_known.get(self.path.rsplit("/", 1)[-1])
                    if stats is None:
                        return self._send(404, {"error": {"code": "NotFoundError", "message": "not found"}})
                    return self._send(200, {"data": {"attributes": {"last_analysis_stats": stats}}})
                if self.path.startswith("/api/v3/analyses/"):
                    if "analysis" in outer.vt_fail:
                        return self._vt_error("analysis")
                    outer._polls += 1
                    done = outer._polls > outer.vt_polls_before_done
                    attrs = {"status": "completed" if done else "queued", "stats": outer.vt_upload_stats if done else {}}
                    return self._send(200, {"data": {"attributes": attrs}})
                self._send(404, {})

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/api/v3/files":
                    outer.vt_key_seen.add(self.headers.get("x-apikey"))
                    if "upload" in outer.vt_fail:
                        return self._vt_error("upload")
                    outer.vt_uploads.append(body)
                    return self._send(200, {"data": {"type": "analysis", "id": "an1"}})
                params = json.loads(body)
                method = self.path.rsplit("/", 1)[-1]
                outer.calls.append((method, params))
                error = outer.fail.get((method, params.get("chat_id"))) or outer.fail.get((method, None))
                if error:
                    return self._send(error["error_code"], {"ok": False, **error})
                if method == "getUpdates":
                    batch = outer.batches.pop(0) if outer.batches else None
                    if batch is None:
                        time.sleep(0.05)
                    result = batch or []
                elif method == "getMe":
                    result = {"id": 9, "is_bot": True, "username": "sekretar_bot", "can_connect_to_business": True}
                elif method == "getBusinessConnection":
                    result = outer.connections.get(params["business_connection_id"])
                    if result is None:
                        return self._send(400, {"ok": False, "error_code": 400,
                                                "description": "Bad Request: no connection"})
                elif method == "getFile":
                    file_path = f"docs/{params['file_id']}"
                    if file_path not in outer.paths:
                        return self._send(400, {"ok": False, "error_code": 400,
                                                "description": "Bad Request: file is too big"})
                    result = {"file_id": params["file_id"], "file_path": file_path}
                elif method == "sendMessage":
                    outer._next_id += 1
                    result = {"message_id": outer._next_id}
                else:
                    result = True
                self._send(200, {"ok": True, "result": result})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True).start()

    def add_file(self, file_id, data: bytes):
        self.paths[f"docs/{file_id}"] = data
        return hashlib.sha256(data).hexdigest()

    def sent(self, to=None):
        """Параметры всех sendMessage (можно отфильтровать по chat_id)."""
        return [p for m, p in self.calls if m == "sendMessage" and (to is None or p["chat_id"] == to)]

    def edits(self):
        return [p for m, p in self.calls if m == "editMessageText"]

    def answers(self):
        return [p for m, p in self.calls if m == "answerCallbackQuery"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def tg():
    fake = FakeTelegram()
    ar.api = BotAPI("TOKEN", base_url=fake.url)
    ar.vt = None
    ar.state = ar.default_state()
    ar.cfg = Config.load(CONFIG_FILE)
    ar.webhook_secret = None
    ar.IDLE_MINUTES = 0
    ar.OWNER_ID = 0
    ar.OWNER_USERNAME = ""
    ar.VT_AUTO_UPLOAD = False
    ar.VT_POLL_INTERVAL = 0
    ar.VT_DAILY_LIMIT = 100
    if ar.STATE_FILE.exists():
        ar.STATE_FILE.unlink()
    ar.remember_connection(CONN)          # подключение уже известно
    yield fake
    fake.stop()


def enable_vt(fake):
    """Включить проверку файлов, направив клиент VirusTotal на имитатор."""
    ar.vt = VirusTotal("VTKEY", base_url=fake.url + "/api/v3", min_interval=0)


# ------------------------------------------------------------------ сборка обновлений

def biz(text, uid=100, name="Ali", username=None, conn="c1", sender_id=None, **extra):
    sender = {"id": sender_id or uid, "first_name": name, "is_bot": False}
    if username:
        sender["username"] = username
    return {"update_id": 1, "business_message": {
        "business_connection_id": conn, "message_id": 1, "date": 0,
        "chat": {"id": uid, "type": "private"}, "from": sender, "text": text, **extra}}


def dm(text, sender_id=1, chat_type="private", username=None, name="X", **extra):
    sender = {"id": sender_id, "first_name": name, "is_bot": False}
    if username:
        sender["username"] = username
    return {"update_id": 2, "message": {"message_id": 1, "date": 0, "chat": {"id": sender_id, "type": chat_type},
                                        "from": sender, "text": text, **extra}}


def doc_update(file_id="f1", file_name="report.pdf", size=100, uid=100, name="Ali", as_dm=False, sender_id=None):
    document = {"file_id": file_id, "file_unique_id": file_id, "file_name": file_name, "file_size": size}
    if as_dm:
        update = dm("", sender_id=sender_id or uid, name=name)
        update["message"].pop("text")
        update["message"]["document"] = document
        return update
    update = biz("", uid=uid, name=name)
    update["business_message"].pop("text")
    update["business_message"]["document"] = document
    return update


def callback(data, sender_id=1, chat_id=1, message_id=50, cq_id="q1", username=None):
    sender = {"id": sender_id, "first_name": "X", "is_bot": False}
    if username:
        sender["username"] = username
    return {"update_id": 3, "callback_query": {"id": cq_id, "from": sender, "data": data,
                                               "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"}}}}


def run(update):
    """Обработать обновление и дождаться фоновых задач (проверка файлов и т.п.)."""
    async def scenario():
        await ar.process_update(update)
        if ar._tasks:
            await asyncio.gather(*ar._tasks)

    asyncio.run(scenario())
