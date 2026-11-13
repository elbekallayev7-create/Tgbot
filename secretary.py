"""Telegram-секретарь: бот, который отвечает от вашего имени. Telegram Premium боту не нужен.

Два способа общения с людьми
  1. Business-подключение: бот подключён к вашему аккаунту (Настройки → Telegram Business → Чат-боты)
     и отвечает прямо в ваших личных чатах от вашего имени.
  2. Прямой режим: люди пишут самому боту; он отвечает, пересылает вам сообщения, а вы отвечаете
     через Reply на уведомление. Работает без подключения Business.

Что умеет
  * автоответы на русском и узбекском (язык по тексту собеседника);
  * триггеры по ключевым словам (config.json);
  * передаёт вам сообщения, пока вас нет; не перебивает, если вы сами пишете в чате;
  * проверяет присланные файлы через VirusTotal (если задан VT_API_KEY);
  * меню из кнопок: /menu.

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
import secrets
import signal
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import menu
from botapi import BotAPI, BotAPIError
from triggers import SUPPORTED_LANGS, Config, ConfigError, cooldown_passed, detect_lang, pick_text, render
from virustotal import VirusTotal, VTError, report_url, sum_engines, summarize

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
OWNER_ID = _int_env("OWNER_ID")                    # ваш Telegram id (необязательно)
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "").strip().lstrip("@").lower()  # или ваш @username
MODE = os.environ.get("MODE", "polling").lower()   # polling | webhook
VT_API_KEY = os.environ.get("VT_API_KEY", "").strip()
VT_AUTO_UPLOAD = os.environ.get("VT_AUTO_UPLOAD", "0").strip().lower() in ("1", "true", "yes")
VT_MAX_BYTES = int(min(float(os.environ.get("VT_MAX_MB", "20")), 20) * 1024 * 1024)  # лимит Bot API — 20 МБ
VT_DAILY_LIMIT = _int_env("VT_DAILY_LIMIT", 100)   # проверок в сутки (бесплатный ключ: 500 запросов)
VT_POLL_INTERVAL = float(os.environ.get("VT_POLL_INTERVAL", "20"))
WEBHOOK_PATH = "/tg"
STATE_TTL_DAYS = 14
MAX_BODY = 1_000_000

ALLOWED_UPDATES = [
    "message", "callback_query", "business_connection", "business_message",
    "edited_business_message", "deleted_business_messages",
]

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("secretary")

HELP = (
    "Всё управление — в меню: /menu\n\n"
    "Команды:\n"
    "/on, /off — «не в сети» вручную\n"
    "/status — состояние\n"
    "/triggers on|off — триггеры\n"
    "/notify on|off — пересылать мне сообщения\n"
    "/scan on|off — проверка файлов (VirusTotal)\n"
    "/text ru|uz <текст> — свой текст автоответа (reset — вернуть из config.json)\n"
    "/ignore <id>, /unignore <id> — не отвечать чату\n"
    "/reset — сбросить паузы между ответами\n"
    "/reload — перечитать config.json\n\n"
    "Файл, отправленный мне в личку, проверится в VirusTotal.\n"
    "Клиенту в прямом режиме можно ответить через Reply на его уведомление."
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
        "scan": True,             # проверять файлы (если есть ключ VirusTotal)
        "awaiting": None,         # "text:ru" — ждём от владельца новый текст
        "texts": {},              # свои тексты автоответа {lang: text}
        "ignore": [],             # id чатов, где молчим
        "lang": {},               # {chat_id: [lang, время]}
        "away_replied": {},       # {chat_id: время}
        "trigger_replied": {},    # {"chat_id:триггер": время}
        "owner_active": {},       # {chat_id: время} — когда владелец сам писал в этом чате
        "owner_seen": 0.0,        # когда владелец последний раз писал где-либо
        "owner_ids": [],          # id владельца, узнанные по OWNER_USERNAME
        "connections": {},        # {id подключения: {owner_id, chat_id, enabled, can_reply}}
        "pending": {},            # {токен: файл, ожидающий решения о загрузке в VirusTotal}
        "relay": {},              # {id уведомления: [chat_id клиента, время]} — для ответов через Reply
        "vt_day": {"date": "", "n": 0},
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
    now = time.time()
    cutoff = now - STATE_TTL_DAYS * 86400
    for key in ("away_replied", "trigger_replied", "owner_active"):
        state[key] = {k: v for k, v in state[key].items() if v >= cutoff}
    state["lang"] = {k: v for k, v in state["lang"].items() if v[1] >= cutoff}
    state["pending"] = {k: v for k, v in state["pending"].items() if v["t"] >= now - 86400}
    relay = {k: v for k, v in state["relay"].items() if v[1] >= cutoff}
    if len(relay) > 300:
        relay = dict(sorted(relay.items(), key=lambda kv: kv[1][1])[-300:])
    state["relay"] = relay
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


def reload_config() -> str:
    global cfg
    try:
        cfg = Config.load(CONFIG_PATH)
    except (ConfigError, OSError) as e:
        return f"Ошибка в config.json, оставил прежние настройки:\n{e}"
    return f"config.json перечитан (триггеров: {len(cfg.triggers)})."


state: dict = default_state()
cfg: Config = Config.from_dict({})
api: BotAPI | None = None
vt: VirusTotal | None = None
webhook_secret: str | None = None
_tasks: set[asyncio.Task] = set()


def background(coro) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ------------------------------------------------------------------ «не в сети» и владелец

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
    ids.update(state["owner_ids"])
    if OWNER_ID:
        ids.add(OWNER_ID)
    return ids


def is_owner(user: dict) -> bool:
    uid = user.get("id")
    if uid is None:
        return False
    if uid in owner_ids():
        return True
    if OWNER_USERNAME and (user.get("username") or "").lower() == OWNER_USERNAME:
        state["owner_ids"].append(uid)  # запоминаем id, чтобы писать владельцу первым
        save_state()
        return True
    return False


def who(sender: dict) -> str:
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or "Без имени"
    return name + (f" (@{sender['username']})" if sender.get("username") else "")


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


def owner_targets(conn: dict | None) -> list[int]:
    targets = [conn.get("chat_id"), conn.get("owner_id")] if conn else []
    for known in state["connections"].values():  # прямой режим: владелец известен по подключению
        targets += [known.get("chat_id"), known.get("owner_id")]
    targets += [OWNER_ID, *state["owner_ids"]]
    unique = []
    for chat_id in targets:
        if chat_id and chat_id not in unique:
            unique.append(chat_id)
    return unique


async def send_to_owner(conn: dict | None, text: str, html_mode: bool = False, markup: dict | None = None):
    """Написать владельцу. Возвращает id отправленного сообщения (или None)."""
    params = {"text": text[:4000]}
    if html_mode:
        params["parse_mode"] = "HTML"
    if markup:
        params["reply_markup"] = markup
    targets = owner_targets(conn)
    if not targets:
        log.warning("Некому писать: задайте OWNER_ID или OWNER_USERNAME и отправьте боту /start")
    for chat_id in targets:
        try:
            result = await api.call("sendMessage", {**params, "chat_id": chat_id})
            return (result or {}).get("message_id")
        except BotAPIError as e:
            log.warning("Не удалось написать владельцу: %s (откройте бота и нажмите Start)", e)
    return None


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
        await send_to_owner(conn, "✅ Секретарь подключён. Меню — /menu.")


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


async def deliver(chat_id: int, conn_id: str | None, text: str, bucket: str, key: str) -> bool:
    """Отправить ответ клиенту (conn_id=None — прямой чат с ботом)."""
    extra = {"business_connection_id": conn_id} if conn_id else {}
    try:
        if DELAY_MAX > 0:  # «печатает...» и небольшая пауза — выглядит естественнее
            try:
                await api.call("sendChatAction", {"chat_id": chat_id, "action": "typing", **extra})
            except BotAPIError:
                pass
            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        await api.call("sendMessage", {"chat_id": chat_id, "text": text, **extra})
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
    if "document" in msg:
        return f"[файл: {msg['document'].get('file_name') or 'без имени'}]"
    for kind in ("photo", "video", "voice", "audio", "sticker", "video_note", "animation"):
        if kind in msg:
            return f"[{kind}]"
    return "[сообщение без текста]"


async def notify_owner(conn: dict | None, sender: dict, chat_id: int, msg: dict, text: str,
                       outcome: str, direct: bool = False) -> None:
    username = f" @{sender['username']}" if sender.get("username") else ""
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or "Без имени"
    body = text or media_label(msg)
    if len(body) > 700:
        body = body[:700] + "…"
    esc = html.escape
    lines = [f"📩 <b>{esc(name)}</b>{esc(username)} · id <code>{chat_id}</code>", esc(body), "", f"<i>{esc(outcome)}</i>"]
    if direct:
        lines.append("↩️ Ответьте (Reply) на это сообщение, чтобы написать клиенту.")
    markup = menu.kb([menu.btn("🚫 Не отвечать этому чату", f"ig:{chat_id}")])
    message_id = await send_to_owner(conn, "\n".join(lines), html_mode=True, markup=markup)
    if direct and message_id:
        state["relay"][str(message_id)] = [chat_id, time.time()]
        save_state()


async def handle_customer(msg: dict, conn: dict | None, conn_id: str | None = None) -> None:
    """Сообщение от клиента: через Business-подключение (conn) или напрямую боту (conn=None)."""
    sender = msg.get("from") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if chat_id is None or sender.get("is_bot"):
        return
    cid = str(chat_id)
    if cid in state["ignore"]:
        return
    direct = conn is None
    now = time.time()
    text = msg.get("text") or msg.get("caption") or ""

    if "document" in msg and scan_enabled():  # файлы проверяем всегда, даже если вы онлайн
        origin = {"label": who(sender), "conn": conn, "chat_id": None, "owner_initiated": False}
        background(scan_document(msg["document"], origin))

    if not direct and now - state["owner_active"].get(cid, 0.0) < HANDOFF_MINUTES * 60:
        return  # владелец сам ведёт этот разговор — не перебиваем

    away = True if direct else is_away(now)  # в прямом режиме бот — «приёмная»: отвечает всегда
    if not away and not state["triggers"]:
        return

    detected = detect_lang(text, cfg.latin_lang)
    if detected:
        state["lang"][cid] = [detected, now]
    lang = detected or (state["lang"].get(cid) or [cfg.default_lang])[0]

    can_reply = True if direct else conn["can_reply"]
    plan = plan_reply(cid, text, lang, away, now) if can_reply else None
    if plan:
        reply, bucket, key = plan
        sent = await deliver(chat_id, conn_id, render(reply, sender.get("first_name")), bucket, key)
        outcome = describe(bucket, key) if sent else "Не удалось отправить ответ"
    else:
        outcome = "Ответ не требовался (недавно уже отвечал)" if can_reply else "У бота нет права отвечать"

    if away and state["notify"] and not (direct and text.startswith("/start")):
        await notify_owner(conn, sender, chat_id, msg, text, outcome, direct)


async def on_business_message(msg: dict) -> None:
    conn_id = msg.get("business_connection_id")
    sender = msg.get("from") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not conn_id or chat_id is None:
        return
    conn = await get_connection(conn_id)
    if not conn or not conn["enabled"]:
        return
    if sender.get("id") == conn["owner_id"]:  # написал сам владелец
        now = time.time()
        state["owner_active"][str(chat_id)] = now
        state["owner_seen"] = now
        return
    await handle_customer(msg, conn, conn_id)


# ------------------------------------------------------------------ проверка файлов (VirusTotal)

def scan_enabled() -> bool:
    return vt is not None and state["scan"]


def vt_quota_take() -> bool:
    today = datetime.now(tz).date().isoformat()
    if state["vt_day"].get("date") != today:
        state["vt_day"] = {"date": today, "n": 0}
    if state["vt_day"]["n"] >= VT_DAILY_LIMIT:
        return False
    state["vt_day"]["n"] += 1
    return True


async def tell(origin: dict, text: str, markup: dict | None = None) -> None:
    """Сообщить результат: в чат владельца с ботом (если он сам прислал файл) или как уведомление."""
    if origin.get("chat_id"):
        params = {"chat_id": origin["chat_id"], "text": text[:4000], "parse_mode": "HTML"}
        if markup:
            params["reply_markup"] = markup
        try:
            await api.call("sendMessage", params)
        except BotAPIError as e:
            log.warning("Не удалось отправить результат проверки: %s", e)
    else:
        await send_to_owner(origin.get("conn"), text, html_mode=True, markup=markup)


async def edit(chat_id: int, message_id: int, text: str, markup: dict | None = None, html_mode: bool = False) -> None:
    params = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
    if markup:
        params["reply_markup"] = markup
    if html_mode:
        params["parse_mode"] = "HTML"
    try:
        await api.call("editMessageText", params)
    except BotAPIError as e:
        if "not modified" not in e.description:
            log.warning("Не удалось изменить сообщение: %s", e)


def file_head(name: str, label: str = "") -> str:
    head = f"🛡 <b>{html.escape(name)}</b>"
    return head + (f"\nОт: {html.escape(label)}" if label else "")


def verdict_text(head: str, stats: dict, sha: str) -> str:
    return f"{head}\n{html.escape(summarize(stats)[1])}\n<a href=\"{report_url(sha)}\">Отчёт VirusTotal</a>"


async def download_document(file_id: str) -> bytes:
    info = await api.call("getFile", {"file_id": file_id})
    return await api.download(info["file_path"], VT_MAX_BYTES)


async def upload_and_wait(name: str, data: bytes, sha: str, head: str) -> str:
    analysis_id = await vt.upload(name, data)
    stats = await vt.wait_analysis(analysis_id, interval=VT_POLL_INTERVAL)
    if stats is None or sum_engines(stats) == 0:
        return f"{head}\n⏳ VirusTotal ещё не закончил проверку.\n<a href=\"{report_url(sha)}\">Результат появится здесь</a>"
    return verdict_text(head, stats, sha)


def scan_error_text(head: str, error: Exception) -> str:
    if isinstance(error, VTError) and error.code == 429:
        reason = "Превышен лимит запросов VirusTotal — попробуйте позже."
    elif isinstance(error, VTError) and error.code in (401, 403):
        reason = "VirusTotal отклонил ключ (VT_API_KEY)."
    elif isinstance(error, BotAPIError) and error.method in ("getFile", "download"):
        reason = "Не удалось получить файл из Telegram (возможно, он больше 20 МБ)."
    else:
        reason = f"Ошибка проверки: {error}"
    return f"{head}\n⚠️ {html.escape(reason)} Файл не проверен — не открывайте, если не доверяете отправителю."


async def scan_document(doc: dict, origin: dict) -> None:
    """Проверить файл: сначала по хэшу (файл не покидает Telegram), загрузка в VirusTotal — по решению владельца."""
    name = doc.get("file_name") or "файл"
    head = file_head(name, origin.get("label", ""))
    try:
        if (doc.get("file_size") or 0) > VT_MAX_BYTES:
            await tell(origin, f"{head}\n⚠️ Файл больше {VT_MAX_BYTES // 2**20} МБ — через бота его не проверить. "
                               "Не открывайте, если не доверяете отправителю.")
            return
        if not vt_quota_take():
            await tell(origin, f"{head}\n⚠️ Суточный лимит проверок исчерпан — файл не проверен.")
            return
        data = await download_document(doc["file_id"])
        sha = hashlib.sha256(data).hexdigest()
        found = await vt.lookup(sha)
        if found and sum_engines(found["stats"]) > 0:
            await tell(origin, verdict_text(head, found["stats"], sha))
        elif origin.get("owner_initiated") or VT_AUTO_UPLOAD:
            await tell(origin, await upload_and_wait(name, data, sha, head))
        else:  # файл VirusTotal не знает: загружать его или нет — решает владелец
            token = secrets.token_hex(4)
            state["pending"][token] = {"file_id": doc["file_id"], "name": name,
                                       "label": origin.get("label", ""), "t": time.time()}
            save_state()
            markup = menu.kb([menu.btn("⬆️ Проверить (загрузить в VirusTotal)", f"vt:up:{token}")],
                             [menu.btn("Пропустить", f"vt:no:{token}")])
            await tell(origin, f"{head}\n❔ VirusTotal этот файл ещё не видел. Загрузить его на проверку? "
                               "Файл станет доступен сервису и, возможно, другим его пользователям.", markup)
    except (VTError, BotAPIError) as e:
        log.warning("Проверка файла: %s", e)
        await tell(origin, scan_error_text(head, e))
    except Exception:
        log.exception("Ошибка при проверке файла")


async def upload_pending(token: str, chat_id: int, message_id: int) -> None:
    item = state["pending"].pop(token, None)
    if not item:
        await edit(chat_id, message_id, "Запрос устарел.")
        return
    head = file_head(item["name"], item.get("label", ""))
    await edit(chat_id, message_id, f"{head}\n⏳ Загружаю в VirusTotal, это займёт до пары минут…", html_mode=True)
    try:
        data = await download_document(item["file_id"])
        sha = hashlib.sha256(data).hexdigest()
        await edit(chat_id, message_id, await upload_and_wait(item["name"], data, sha, head), html_mode=True)
    except (VTError, BotAPIError) as e:
        log.warning("Загрузка в VirusTotal: %s", e)
        await edit(chat_id, message_id, scan_error_text(head, e), html_mode=True)
    except Exception:
        log.exception("Ошибка при загрузке файла в VirusTotal")
    save_state()


# ------------------------------------------------------------------ меню и команды владельца

def menu_info() -> dict:
    return {"away_now": is_away(), "vt": vt is not None,
            "connections": sum(1 for c in state["connections"].values() if c["enabled"])}


def page_main() -> tuple[str, dict]:
    return menu.main_menu(state, menu_info())


def page_texts() -> tuple[str, dict]:
    return menu.texts_menu(away_text_for("ru"), away_text_for("uz"))


def status_text() -> str:
    start, end = SCHEDULE
    schedule = f"{AWAY_FROM}–{AWAY_TO} ({TIMEZONE})" if start and end else "нет"
    idle = f"{IDLE_MINUTES:g} мин молчания" if IDLE_MINUTES > 0 else "выключено"
    if vt is None:
        files = "выключена (нет VT_API_KEY)"
    else:
        files = (f"{'включена' if state['scan'] else 'выключена'} "
                 f"(сегодня проверок: {state['vt_day']['n'] if state['vt_day'].get('date') else 0}/{VT_DAILY_LIMIT})")
    return (
        f"Вручную: {'включён' if state['away'] else 'выключен'}\n"
        f"Расписание: {schedule}\n"
        f"По молчанию: {idle}\n"
        f"Сейчас «не в сети»: {'да' if is_away() else 'нет'}\n"
        f"Триггеры: {'включены' if state['triggers'] else 'выключены'} (всего {len(cfg.triggers)})\n"
        f"Пересылка сообщений мне: {'да' if state['notify'] else 'нет'}\n"
        f"Проверка файлов: {files}\n"
        f"Не перебиваю после моего ответа: {HANDOFF_MINUTES:g} мин\n"
        f"Игнорируемых чатов: {len(state['ignore'])}\n"
        f"Подключений Business: {sum(1 for c in state['connections'].values() if c['enabled'])}\n\n"
        f"Текст ru: {away_text_for('ru')}\nТекст uz: {away_text_for('uz')}"
    )


def reset_pauses() -> None:
    state["away_replied"].clear()
    state["trigger_replied"].clear()
    state["owner_active"].clear()


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
    if cmd in ("triggers", "notify", "scan") and _switch(arg) is not None:
        if cmd == "scan" and vt is None:
            return "Проверка файлов не настроена: добавьте VT_API_KEY в переменные окружения."
        state[cmd] = _switch(arg)
        title = {"triggers": "Триггеры", "notify": "Пересылка сообщений", "scan": "Проверка файлов"}[cmd]
        return f"{title}: {'включено' if state[cmd] else 'выключено'}."
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
        reset_pauses()
        return "Паузы между ответами сброшены."
    if cmd == "reload":
        return reload_config()
    return HELP


async def answer_cb(callback_id: str, text: str | None = None, alert: bool = False) -> None:
    params = {"callback_query_id": callback_id}
    if text:
        params.update({"text": text[:190], "show_alert": alert})
    try:
        await api.call("answerCallbackQuery", params)
    except BotAPIError as e:
        log.warning("answerCallbackQuery: %s", e)


async def on_callback(cq: dict) -> None:
    """Нажатие кнопки."""
    if not is_owner(cq.get("from") or {}):
        await answer_cb(cq["id"], "Нет доступа", True)
        return
    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        await answer_cb(cq["id"])
        return

    kind, _, arg = (cq.get("data") or "").partition(":")
    toast, alert, page = None, False, None

    if kind == "m":
        state["awaiting"] = None
        if arg == "away":
            state["away"] = not state["away"]
            toast, page = ("Секретарь отвечает вместо вас" if state["away"] else "Ручной режим выключен"), page_main()
        elif arg == "trig":
            state["triggers"] = not state["triggers"]
            page = page_main()
        elif arg == "notify":
            state["notify"] = not state["notify"]
            page = page_main()
        elif arg == "scan":
            if vt is None:
                toast, alert = "Добавьте VT_API_KEY в переменные окружения", True
            else:
                state["scan"] = not state["scan"]
                page = page_main()
        elif arg == "texts":
            page = page_texts()
        elif arg == "ignore":
            page = menu.ignore_menu(state["ignore"])
        elif arg == "status":
            page = menu.simple_page(status_text())
        elif arg == "reset":
            reset_pauses()
            toast, page = "Паузы сброшены", page_main()
        elif arg == "reload":
            toast, page = reload_config(), page_main()
        elif arg == "help":
            page = menu.simple_page(HELP)
        else:
            page = page_main()
    elif kind == "t" and arg in SUPPORTED_LANGS:
        state["awaiting"] = f"text:{arg}"
        page = menu.prompt_page(arg)
    elif kind == "tr" and arg in SUPPORTED_LANGS:
        state["texts"].pop(arg, None)
        toast, page = f"Текст ({arg}) возвращён к config.json", page_texts()
    elif kind == "ui":
        if arg in state["ignore"]:
            state["ignore"].remove(arg)
        page = menu.ignore_menu(state["ignore"])
    elif kind == "ig":
        if arg.lstrip("-").isdigit() and arg not in state["ignore"]:
            state["ignore"].append(arg)
        toast = "Этот чат больше не получит ответов"
    elif kind == "x":
        state["awaiting"] = None
        page = page_main()
    elif kind == "vt":
        action, _, token = arg.partition(":")
        if action == "up" and vt is not None:
            background(upload_pending(token, chat_id, message_id))
            toast = "Загружаю в VirusTotal…"
        elif action == "no":
            state["pending"].pop(token, None)
            await edit(chat_id, message_id, "Файл не загружался в VirusTotal.")

    await answer_cb(cq["id"], toast, alert)
    if page:
        await edit(chat_id, message_id, *page)
    save_state()


async def handle_owner_dm(msg: dict) -> None:
    """Личное сообщение боту от владельца: файлы, ответы клиентам, ввод текста, команды."""
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    async def say(reply: str, markup: dict | None = None) -> None:
        params = {"chat_id": chat_id, "text": reply[:4000]}
        if markup:
            params["reply_markup"] = markup
        await api.call("sendMessage", params)

    if "document" in msg:  # файл от владельца: проверяем и загружаем сразу — он сам попросил
        if vt is None:
            await say("Проверка файлов не настроена: добавьте VT_API_KEY в переменные окружения.")
        else:
            await say("⏳ Проверяю файл…")
            background(scan_document(msg["document"], {"label": "", "conn": None, "chat_id": chat_id,
                                                       "owner_initiated": True}))
        return

    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    if text and not text.startswith("/") and reply_to and str(reply_to) in state["relay"]:
        try:  # ответ клиенту прямого режима
            await api.call("sendMessage", {"chat_id": state["relay"][str(reply_to)][0], "text": text})
            await say("✅ Отправлено клиенту.")
        except BotAPIError as e:
            await say(f"Не удалось отправить: {e.description}")
        return

    if state["awaiting"] and text and not text.startswith("/"):
        lang = state["awaiting"].split(":")[1]
        state["texts"][lang] = text
        state["awaiting"] = None
        save_state()
        await say(f"✅ Текст ({lang}) обновлён.")
        await say(*page_texts())
        return

    if not text.startswith("/"):  # любое другое сообщение — показываем меню
        await say(*page_main())
        return

    state["awaiting"] = None
    parts = text.split(maxsplit=3)
    head = parts[0][1:].split("@")[0].lower()
    if head in ("start", "menu"):
        await say(*page_main())
        return
    args = parts[1:] if head == "away" else [head] + text.split(maxsplit=2)[1:]
    reply = await run_command(args)
    save_state()
    await say(reply)


async def on_message(msg: dict) -> None:
    """Обычное сообщение боту в личку: владелец — команды и меню, остальные — клиенты (прямой режим)."""
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    if chat.get("type") != "private" or sender.get("is_bot"):
        return
    if is_owner(sender):
        await handle_owner_dm(msg)
    else:
        await handle_customer(msg, None)


# ------------------------------------------------------------------ обработка обновлений

async def process_update(update: dict) -> None:
    try:
        if "callback_query" in update:
            await on_callback(update["callback_query"])
        elif "business_connection" in update:
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
    global api, vt, state, cfg, webhook_secret
    token = require_env("BOT_TOKEN")
    api = BotAPI(token)
    try:
        cfg = load_config()
    except ConfigError as e:
        raise SystemExit(f"Ошибка в config.json: {e}")
    state = load_state()
    state["owner_seen"] = state["owner_seen"] or time.time()  # при старте считаем, что владелец на связи
    vt = VirusTotal(VT_API_KEY) if VT_API_KEY else None

    try:
        me = await api.call("getMe")
    except BotAPIError as e:
        hint = "Проверьте интернет." if e.code == 0 else "Проверьте BOT_TOKEN (новый: @BotFather → /token)."
        raise SystemExit(f"Не удалось войти как бот ({e}). {hint}")
    log.info("Секретарь запущен: @%s, режим %s, триггеров: %d, проверка файлов: %s, состояние: %s",
             me.get("username"), MODE, len(cfg.triggers), "VirusTotal" if vt else "выключена", STATE_FILE)
    if me.get("can_connect_to_business") is False:
        log.info("Business Mode у бота выключен — работает только прямой режим (люди пишут боту). "
                 "Включить: @BotFather → ваш бот → Bot Settings → Business Mode")
    if not (OWNER_ID or OWNER_USERNAME or state["connections"]):
        log.warning("Владелец не задан: добавьте OWNER_USERNAME (ваш @username) или OWNER_ID, "
                    "иначе меню и пересылка не заработают до подключения Business")
    try:
        await api.call("setMyCommands", {"commands": [
            {"command": "menu", "description": "Меню секретаря"},
            {"command": "status", "description": "Состояние"},
            {"command": "on", "description": "Включить «не в сети»"},
            {"command": "off", "description": "Выключить «не в сети»"},
        ]})
    except BotAPIError as e:
        log.warning("setMyCommands: %s", e)

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
