"""Telegram-секретарь: Business-бот, который отвечает от вашего имени.

Как это работает
  Вы создаёте обычного бота в @BotFather, включаете ему Business Mode и подключаете его к своему
  аккаунту в Telegram («Telegram Business → Чат-боты»). Бот получает ваши личные сообщения и может
  отвечать в них от вашего имени. Токен от BotFather — единственный секрет, вход в аккаунт не нужен.

Что умеет
  * пока вы «не в сети» — отвечает на русском или узбекском (язык по тексту собеседника);
  * триггеры по ключевым словам (config.json): цена, адрес, график, «срочно» и т.д.;
  * передаёт вам каждое сообщение, пришедшее в ваше отсутствие (как секретарь);
  * не перебивает: если вы сами написали в чате, бот молчит в нём HANDOFF_MINUTES минут;
  * «не в сети» = вручную (/on, /off), по расписанию и (по желанию) по долгому молчанию владельца.

Запуск и настройка: README.md
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import random
import signal
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from botapi import BotAPI, BotAPIError
from triggers import SUPPORTED_LANGS, Config, ConfigError, cooldown_passed, detect_lang, pick_text, render

BASE_DIR = Path(__file__).resolve().parent


def _int_env(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        raise SystemExit(f"{name} должен быть числом")


# ------------------------------------------------------------------ настройки
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", BASE_DIR / "config.json"))
STATE_FILE = Path(os.environ.get("STATE_FILE", BASE_DIR / "state.json"))
TIMEZONE = os.environ.get("TZ_NAME", "Asia/Tashkent")
AWAY_FROM = os.environ.get("AWAY_FROM", "")        # «не в сети» с ЧЧ:ММ
AWAY_TO = os.environ.get("AWAY_TO", "")            # ... до ЧЧ:ММ
COOLDOWN_HOURS = float(os.environ.get("COOLDOWN_HOURS", "6"))
DELAY_MIN = float(os.environ.get("REPLY_DELAY_MIN", "1"))   # пауза перед ответом, сек
DELAY_MAX = float(os.environ.get("REPLY_DELAY_MAX", "3"))
HANDOFF_MINUTES = float(os.environ.get("HANDOFF_MINUTES", "30"))  # молчать в чате после вашего ответа
IDLE_MINUTES = float(os.environ.get("IDLE_MINUTES", "0"))   # >0: «не в сети», если вы молчите дольше
OWNER_ID = _int_env("OWNER_ID")                    # необязательно: ваш Telegram id для команд боту
MODE = os.environ.get("MODE", "polling").lower()   # polling | webhook
WEBHOOK_PATH = "/tg"
STATE_TTL_DAYS = 14
MAX_BODY = 1_000_000

ALLOWED_UPDATES = [
    "message", "business_connection", "business_message",
    "edited_business_message", "deleted_business_messages",
]

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("secretary")

HELP = (
    "Команды (писать мне в личку):\n"
    "/on, /off — включить / выключить «не в сети» вручную\n"
    "/status — состояние\n"
    "/triggers on|off — триггеры по ключевым словам\n"
    "/notify on|off — пересылать мне сообщения, пока меня нет\n"
    "/text ru|uz <текст> — свой текст автоответа (reset — вернуть из config.json)\n"
    "/ignore <id> и /unignore <id> — не отвечать чату (id указан в уведомлении)\n"
    "/reset — сбросить паузы между ответами\n"
    "/reload — перечитать config.json\n"
    "Можно и так: /away on, /away status и т.д."
)


def parse_hhmm(value: str) -> dtime | None:
    if not value:
        return None
    try:
        hours, minutes = value.split(":")
        return dtime(int(hours), int(minutes))
    except ValueError:
        raise SystemExit(f"Неверное время «{value}», нужен формат ЧЧ:ММ (например 23:00)")


SCHEDULE = (parse_hhmm(AWAY_FROM), parse_hhmm(AWAY_TO))
tz = ZoneInfo(TIMEZONE)


# ------------------------------------------------------------------ состояние

def default_state() -> dict:
    return {
        "away": False,            # ручной режим «не в сети»
        "triggers": True,         # триггеры включены
        "notify": True,           # пересылать владельцу сообщения, пока его нет
        "texts": {},              # свои тексты автоответа {lang: text}
        "ignore": [],             # id чатов, где молчим
        "lang": {},               # {chat_id: [lang, время]}
        "away_replied": {},       # {chat_id: время}
        "trigger_replied": {},    # {"chat_id:триггер": время}
        "owner_active": {},       # {chat_id: время} — когда владелец сам писал в этом чате
        "owner_seen": 0.0,        # когда владелец последний раз писал где-либо
        "connections": {},        # {id подключения: {owner_id, chat_id, enabled, can_reply}}
        "offset": 0,              # позиция в очереди обновлений (polling)
    }


def load_state() -> dict:
    state = default_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return state
    except (OSError, ValueError) as e:
        log.warning("Не удалось прочитать %s (%s) — начинаю с пустого состояния", STATE_FILE, e)
        return state
    state.update({k: v for k, v in data.items() if k in state})
    return state


def save_state() -> None:
    cutoff = time.time() - STATE_TTL_DAYS * 86400
    for key in ("away_replied", "trigger_replied", "owner_active"):
        state[key] = {k: v for k, v in state[key].items() if v >= cutoff}
    state["lang"] = {k: v for k, v in state["lang"].items() if v[1] >= cutoff}
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_FILE)  # атомарно
    except OSError as e:
        log.warning("Не удалось сохранить состояние: %s", e)


def load_config() -> Config:
    try:
        return Config.load(CONFIG_PATH)
    except FileNotFoundError:
        log.warning("%s не найден — работаю только со встроенными текстами", CONFIG_PATH)
        return Config.from_dict({})


state: dict = default_state()
cfg: Config = Config.from_dict({})
api: BotAPI | None = None
webhook_secret: str | None = None
_tasks: set[asyncio.Task] = set()


def background(coro) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ------------------------------------------------------------------ «не в сети»

def in_schedule() -> bool:
    start, end = SCHEDULE
    if not start or not end:
        return False
    now = datetime.now(tz).time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # интервал через полночь


def is_away(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    idle = IDLE_MINUTES > 0 and now - state["owner_seen"] > IDLE_MINUTES * 60
    return bool(state["away"] or in_schedule() or idle)


def away_text_for(lang: str) -> str:
    return state["texts"].get(lang) or cfg.away_text(lang)


def owner_ids() -> set[int]:
    ids = {c["owner_id"] for c in state["connections"].values() if c.get("owner_id")}
    if OWNER_ID:
        ids.add(OWNER_ID)
    return ids


# ------------------------------------------------------------------ подключения Business

def remember_connection(raw: dict) -> dict:
    """Разобрать BusinessConnection (поддерживает и can_reply, и rights.can_reply)."""
    rights = raw.get("rights") or {}
    can_reply = raw["can_reply"] if "can_reply" in raw else rights.get("can_reply", True)
    conn = {
        "owner_id": raw["user"]["id"],
        "chat_id": raw.get("user_chat_id"),
        "enabled": bool(raw.get("is_enabled", True)),
        "can_reply": bool(can_reply),
    }
    state["connections"][raw["id"]] = conn
    return conn


async def get_connection(conn_id: str) -> dict | None:
    conn = state["connections"].get(conn_id)
    if conn is None:  # например, после перезапуска с пустым состоянием
        try:
            raw = await api.call("getBusinessConnection", {"business_connection_id": conn_id})
            conn = remember_connection(raw)
        except (BotAPIError, KeyError) as e:
            log.warning("Не удалось получить подключение: %s", e)
            return None
    return conn


async def send_to_owner(conn: dict | None, text: str, html_mode: bool = False) -> None:
    targets = []
    if conn:
        targets = [conn.get("chat_id"), conn.get("owner_id")]
    elif OWNER_ID:
        targets = [OWNER_ID]
    params = {"text": text[:4000]}
    if html_mode:
        params["parse_mode"] = "HTML"
    for chat_id in filter(None, targets):
        try:
            await api.call("sendMessage", {**params, "chat_id": chat_id})
            return
        except BotAPIError as e:
            log.warning("Не удалось написать владельцу: %s (откройте бота и нажмите Start)", e)


async def on_connection(raw: dict) -> None:
    conn = remember_connection(raw)
    save_state()
    log.info("Business-подключение: enabled=%s can_reply=%s", conn["enabled"], conn["can_reply"])
    if not conn["enabled"]:
        await send_to_owner(conn, "⚠️ Секретарь отключён от вашего аккаунта.")
    elif not conn["can_reply"]:
        await send_to_owner(conn, "⚠️ Секретарь подключён, но без права отвечать. Включите «Отвечать на сообщения» "
                                  "в Настройки → Telegram Business → Чат-боты.")
    else:
        await send_to_owner(conn, "✅ Секретарь подключён. Команды — /help, состояние — /status.")


# ------------------------------------------------------------------ ответы

def plan_reply(cid: str, text: str, lang: str, away: bool, now: float):
    """Решить, что ответить: (текст, раздел_состояния, ключ) или None.

    Время ответа записывается сразу (до отправки), чтобы два быстрых сообщения подряд
    не привели к двум ответам.
    """
    if state["triggers"] and text:
        match = cfg.find_trigger(text, lang)
        if match and (away or not match.trigger.only_when_away):
            key = f"{cid}:{match.trigger.name}"
            if not cooldown_passed(state["trigger_replied"], key, now, match.trigger.cooldown_minutes):
                return None
            state["trigger_replied"][key] = now
            return pick_text(match.trigger.replies, match.lang, cfg.default_lang), "trigger_replied", key
    if away:
        if not cooldown_passed(state["away_replied"], cid, now, COOLDOWN_HOURS * 60):
            return None
        state["away_replied"][cid] = now
        return away_text_for(lang), "away_replied", cid
    return None


async def deliver(chat_id: int, conn_id: str, text: str, bucket: str, key: str) -> bool:
    try:
        if DELAY_MAX > 0:  # «печатает...» и небольшая пауза — выглядит естественнее
            try:
                await api.call("sendChatAction", {"chat_id": chat_id, "action": "typing",
                                                  "business_connection_id": conn_id})
            except BotAPIError:
                pass
            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        await api.call("sendMessage", {"chat_id": chat_id, "text": text, "business_connection_id": conn_id})
    except BotAPIError as e:
        state[bucket].pop(key, None)  # не получилось — не считаем, что ответили
        log.warning("Не удалось отправить ответ: %s", e)
        return False
    log.info("Ответ отправлен (%s)", bucket)
    save_state()
    return True


def describe(bucket: str, key: str) -> str:
    if bucket == "trigger_replied":
        return f"Ответил по триггеру «{key.split(':', 1)[1]}»"
    return "Отправил автоответ"


def media_label(msg: dict) -> str:
    for kind in ("photo", "video", "voice", "audio", "document", "sticker", "video_note", "animation"):
        if kind in msg:
            return f"[{kind}]"
    return "[сообщение без текста]"


async def notify_owner(conn: dict, sender: dict, chat_id: int, msg: dict, text: str, outcome: str) -> None:
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or "Без имени"
    username = f" @{sender['username']}" if sender.get("username") else ""
    body = text or media_label(msg)
    if len(body) > 700:
        body = body[:700] + "…"
    esc = html.escape
    await send_to_owner(conn, (
        f"📩 <b>{esc(name)}</b>{esc(username)} · id <code>{chat_id}</code>\n"
        f"{esc(body)}\n\n<i>{esc(outcome)}</i>"
    ), html_mode=True)


async def on_business_message(msg: dict) -> None:
    conn_id = msg.get("business_connection_id")
    sender = msg.get("from") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not conn_id or chat_id is None:
        return
    conn = await get_connection(conn_id)
    if not conn or not conn["enabled"]:
        return

    now = time.time()
    cid = str(chat_id)
    if sender.get("id") == conn["owner_id"]:  # написал сам владелец
        state["owner_active"][cid] = now
        state["owner_seen"] = now
        return
    if sender.get("is_bot") or cid in state["ignore"]:
        return
    if now - state["owner_active"].get(cid, 0.0) < HANDOFF_MINUTES * 60:
        return  # владелец сам ведёт этот разговор — не перебиваем

    away = is_away(now)
    if not away and not state["triggers"]:
        return

    text = msg.get("text") or msg.get("caption") or ""
    detected = detect_lang(text, cfg.latin_lang)
    if detected:
        state["lang"][cid] = [detected, now]
    lang = detected or (state["lang"].get(cid) or [cfg.default_lang])[0]

    plan = plan_reply(cid, text, lang, away, now) if conn["can_reply"] else None
    if plan:
        reply, bucket, key = plan
        sent = await deliver(chat_id, conn_id, render(reply, sender.get("first_name")), bucket, key)
        outcome = describe(bucket, key) if sent else "Не удалось отправить ответ"
    else:
        outcome = "Ответ не требовался (недавно уже отвечал)" if conn["can_reply"] else "У бота нет права отвечать"

    if away and state["notify"]:
        await notify_owner(conn, sender, chat_id, msg, text, outcome)


# ------------------------------------------------------------------ команды владельцу

def status_text() -> str:
    start, end = SCHEDULE
    schedule = f"{AWAY_FROM}–{AWAY_TO} ({TIMEZONE})" if start and end else "нет"
    idle = f"{IDLE_MINUTES:g} мин молчания" if IDLE_MINUTES > 0 else "выключено"
    return (
        f"Вручную: {'включён' if state['away'] else 'выключен'}\n"
        f"Расписание: {schedule}\n"
        f"По молчанию: {idle}\n"
        f"Сейчас «не в сети»: {'да' if is_away() else 'нет'}\n"
        f"Триггеры: {'включены' if state['triggers'] else 'выключены'} (всего {len(cfg.triggers)})\n"
        f"Пересылка сообщений мне: {'да' if state['notify'] else 'нет'}\n"
        f"Не перебиваю после моего ответа: {HANDOFF_MINUTES:g} мин\n"
        f"Игнорируемых чатов: {len(state['ignore'])}\n"
        f"Подключений Business: {sum(1 for c in state['connections'].values() if c['enabled'])}\n\n"
        f"Текст ru: {away_text_for('ru')}\nТекст uz: {away_text_for('uz')}"
    )


def _switch(value: str) -> bool | None:
    return {"on": True, "off": False}.get(value.lower())


async def run_command(args: list[str]) -> str:
    cmd = args[0].lower() if args else "status"
    arg = args[1] if len(args) > 1 else ""

    if cmd == "on":
        state["away"] = True
        return "Секретарь отвечает вместо вас."
    if cmd == "off":
        state["away"] = False
        return "Ручной режим выключен (расписание продолжает работать)."
    if cmd == "status":
        return status_text()
    if cmd in ("triggers", "notify") and _switch(arg) is not None:
        state[cmd] = _switch(arg)
        title = "Триггеры" if cmd == "triggers" else "Пересылка сообщений"
        return f"{title} {'включены' if state[cmd] else 'выключены'}."
    if cmd == "text":
        lang = arg.lower()
        value = args[2].strip() if len(args) > 2 else ""
        if lang not in SUPPORTED_LANGS or not value:
            return "Формат: /text ru|uz <текст>  (или: /text ru reset)"
        if value.lower() == "reset":
            state["texts"].pop(lang, None)
            return f"Текст ({lang}) возвращён к значению из config.json."
        state["texts"][lang] = value
        return f"Текст ({lang}) обновлён."
    if cmd in ("ignore", "unignore"):
        if not arg.lstrip("-").isdigit():
            return f"Формат: /{cmd} <id чата из уведомления>"
        if cmd == "ignore":
            if arg not in state["ignore"]:
                state["ignore"].append(arg)
            return f"Молчу в чате {arg}."
        if arg in state["ignore"]:
            state["ignore"].remove(arg)
        return f"Снова отвечаю в чате {arg}."
    if cmd == "reset":
        state["away_replied"].clear()
        state["trigger_replied"].clear()
        state["owner_active"].clear()
        return "Паузы между ответами сброшены."
    if cmd == "reload":
        global cfg
        try:
            cfg = Config.load(CONFIG_PATH)
        except (ConfigError, OSError) as e:
            return f"Ошибка в config.json, оставил прежние настройки:\n{e}"
        return f"config.json перечитан (триггеров: {len(cfg.triggers)})."
    return HELP


async def on_message(msg: dict) -> None:
    """Обычное сообщение боту в личку: команды владельца."""
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    text = (msg.get("text") or "").strip()
    if chat.get("type") != "private" or not text.startswith("/"):
        return
    if sender.get("id") not in owner_ids():
        if text.lower().startswith("/start"):
            await api.call("sendMessage", {"chat_id": chat["id"],
                                           "text": "Здравствуйте! Это бот-секретарь. Напишите владельцу напрямую."})
        return

    parts = text.split(maxsplit=3)
    head = parts[0][1:].split("@")[0].lower()
    if head == "away":
        args = parts[1:]
    else:
        args = [head] + text.split(maxsplit=2)[1:]
    if head == "start":
        args = ["help"]
    reply = await run_command(args)
    if head == "start":
        reply = HELP + "\n\n" + status_text()
    save_state()
    await api.call("sendMessage", {"chat_id": chat["id"], "text": reply[:4000]})


# ------------------------------------------------------------------ обработка обновлений

async def process_update(update: dict) -> None:
    try:
        if "business_connection" in update:
            await on_connection(update["business_connection"])
        elif "business_message" in update:
            await on_business_message(update["business_message"])
        elif "message" in update:
            await on_message(update["message"])
        # edited_business_message / deleted_business_messages намеренно игнорируем
    except Exception:
        log.exception("Ошибка при обработке обновления %s", update.get("update_id"))


async def poll_loop() -> None:
    """Режим polling: бот сам опрашивает Telegram (для Worker и запуска на своём компьютере)."""
    try:
        await api.call("deleteWebhook")  # polling не работает, пока установлен webhook
    except BotAPIError as e:
        log.warning("deleteWebhook: %s", e)
    offset = state["offset"]
    pause = 1
    while True:
        try:
            updates = await api.call(
                "getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ALLOWED_UPDATES}, timeout=45
            )
            pause = 1
        except BotAPIError as e:
            wait = e.retry_after or (5 if e.code == 409 else pause)
            log.warning("getUpdates: %s — повтор через %s с", e, wait)
            await asyncio.sleep(wait)
            pause = min(pause * 2, 60)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            background(process_update(update))  # параллельно: задержка «печатает…» не тормозит других
        if updates:
            state["offset"] = offset
            save_state()


# ------------------------------------------------------------------ HTTP (health-check и webhook)

def make_webhook_secret(token: str) -> str:
    return hashlib.sha256(("tg-webhook:" + token).encode()).hexdigest()


async def http_handle(reader, writer) -> None:
    """Крошечный HTTP-сервер: GET → «ok» (для Render и пингов), POST /tg → обновление от Telegram."""
    status, body = 400, b"bad request"
    method = "GET"
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        request_line, *header_lines = head.decode("latin-1").split("\r\n")
        method, target = request_line.split(" ")[:2]
        headers = {}
        for line in header_lines:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        path = target.split("?", 1)[0]

        if method in ("GET", "HEAD"):
            status, body = 200, b"ok"
        elif method == "POST" and path == WEBHOOK_PATH and webhook_secret:
            given = headers.get("x-telegram-bot-api-secret-token", "").encode()
            length = int(headers.get("content-length", "0"))
            if not hmac.compare_digest(given, webhook_secret.encode()):
                status, body = 403, b"forbidden"
            elif not 0 < length <= MAX_BODY:
                status, body = 413, b"bad length"
            else:
                raw = await asyncio.wait_for(reader.readexactly(length), 10)
                background(process_update(json.loads(raw)))
                status, body = 200, b"ok"
        else:
            status, body = 404, b"not found"
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            ValueError, ConnectionError):
        pass
    try:
        reasons = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 413: "Payload Too Large"}
        writer.write(f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\nContent-Type: text/plain\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                     + (b"" if method == "HEAD" else body))
        await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def start_http(port: str | None):
    if not port:
        return None
    server = await asyncio.start_server(http_handle, "0.0.0.0", int(port))
    log.info("HTTP слушает порт %s", port)
    return server


# ------------------------------------------------------------------ запуск

def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name} (см. README.md)")
    return value


async def main() -> None:
    global api, state, cfg, webhook_secret
    token = require_env("BOT_TOKEN")
    api = BotAPI(token)
    try:
        cfg = load_config()
    except ConfigError as e:
        raise SystemExit(f"Ошибка в config.json: {e}")
    state = load_state()
    state["owner_seen"] = state["owner_seen"] or time.time()  # при старте считаем, что владелец на связи

    try:
        me = await api.call("getMe")
    except BotAPIError as e:
        hint = "Проверьте интернет." if e.code == 0 else "Проверьте BOT_TOKEN (новый: @BotFather → /token)."
        raise SystemExit(f"Не удалось войти как бот ({e}). {hint}")
    log.info("Секретарь запущен: @%s, режим %s, триггеров: %d, состояние: %s",
             me.get("username"), MODE, len(cfg.triggers), STATE_FILE)
    if me.get("can_connect_to_business") is False:
        log.warning("У бота выключен Business Mode: @BotFather → ваш бот → Bot Settings → Business Mode")

    server = None
    port = os.environ.get("PORT")
    if MODE == "webhook":
        base = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
        if not base or not port:
            raise SystemExit("Для MODE=webhook нужны PORT и WEBHOOK_URL (адрес сервиса, https://...onrender.com)")
        webhook_secret = make_webhook_secret(token)
        server = await start_http(port)  # сначала порт — Render ждёт его
        await api.call("setWebhook", {"url": base + WEBHOOK_PATH, "secret_token": webhook_secret,
                                      "allowed_updates": ALLOWED_UPDATES})
        log.info("Webhook установлен: %s%s", base, WEBHOOK_PATH)
    else:
        server = await start_http(port)  # health-check, если запущено как Web Service

    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    except (NotImplementedError, RuntimeError):
        pass  # Windows
    try:
        if MODE == "webhook":
            await asyncio.Event().wait()  # работаем, пока не остановят
        else:
            await poll_loop()
    finally:
        save_state()
        if server:
            server.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
