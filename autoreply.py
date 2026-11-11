"""Telegram-автоответчик (работает от вашего аккаунта, на Telethon).

Что умеет
  * Когда вы «не в сети» — отвечает на личные сообщения (на русском или узбекском,
    язык определяется по тексту собеседника).
  * Триггеры: отвечает на ключевые слова («цена», «narx», «manzil»...) — см. config.json.
  * «Не в сети» включается вручную (/away on|off в «Избранном») и/или по расписанию.
  * Каждому собеседнику отвечает не чаще раза в COOLDOWN_HOURS (триггеры — свои паузы).

Запуск: см. README.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.types import User

from triggers import SUPPORTED_LANGS, Config, ConfigError, cooldown_passed, detect_lang, pick_text, render

BASE_DIR = Path(__file__).resolve().parent

# ------------------------------------------------------------------ настройки
# Всё необязательное можно менять переменными окружения.
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", BASE_DIR / "config.json"))
STATE_FILE = Path(os.environ.get("STATE_FILE", BASE_DIR / "state.json"))
TIMEZONE = os.environ.get("TZ_NAME", "Asia/Tashkent")
AWAY_FROM = os.environ.get("AWAY_FROM", "")  # «не в сети» с ЧЧ:ММ, например 23:00
AWAY_TO = os.environ.get("AWAY_TO", "")      # ... до ЧЧ:ММ, например 08:00
COOLDOWN_HOURS = float(os.environ.get("COOLDOWN_HOURS", "6"))
DELAY_MIN = float(os.environ.get("REPLY_DELAY_MIN", "1"))  # пауза перед ответом, сек
DELAY_MAX = float(os.environ.get("REPLY_DELAY_MAX", "3"))
STATE_TTL_DAYS = 14            # через сколько дней забывать собеседников
SERVICE_IDS = {777000}         # служебные уведомления Telegram

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("autoreply")

HELP = (
    "Команды (писать в «Избранное»):\n"
    "/away on | off — включить / выключить вручную\n"
    "/away status — состояние\n"
    "/away triggers on | off — триггеры (ключевые слова)\n"
    "/away text ru|uz <текст> — свой текст автоответа (reset — вернуть из config.json)\n"
    "/away ignore <@username|id> — не отвечать этому человеку\n"
    "/away unignore <@username|id>\n"
    "/away reset — сбросить паузы между ответами\n"
    "/away reload — перечитать config.json"
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
        "away": False,            # ручной режим
        "triggers": True,         # триггеры включены
        "texts": {},              # свои тексты автоответа {lang: text}
        "ignore": [],             # id, которым не отвечаем
        "lang": {},               # {id: [lang, время]} — последний язык собеседника
        "away_replied": {},       # {id: время} — когда последний раз ответили «я не в сети»
        "trigger_replied": {},    # {"id:триггер": время}
    }


def load_state() -> dict:
    state = default_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return state
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Не удалось прочитать %s (%s) — начинаю с пустого состояния", STATE_FILE, e)
        return state
    # совместимость со старой версией (ключ replied -> away_replied)
    if "replied" in data:
        data.setdefault("away_replied", data.pop("replied"))
    data.pop("text", None)
    state.update({k: v for k, v in data.items() if k in state})
    return state


def save_state() -> None:
    cutoff = time.time() - STATE_TTL_DAYS * 86400
    for key in ("away_replied", "trigger_replied"):
        state[key] = {k: v for k, v in state[key].items() if v >= cutoff}
    state["lang"] = {k: v for k, v in state["lang"].items() if v[1] >= cutoff}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)  # атомарно: файл не повредится при сбое


def load_config() -> Config:
    try:
        return Config.load(CONFIG_PATH)
    except FileNotFoundError:
        log.warning("%s не найден — работаю только со встроенными текстами", CONFIG_PATH)
        return Config.from_dict({})


state: dict = default_state()
cfg: Config = Config.from_dict({})
client: TelegramClient | None = None


# ------------------------------------------------------------------ логика

def in_schedule() -> bool:
    start, end = SCHEDULE
    if not start or not end:
        return False
    now = datetime.now(tz).time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # интервал через полночь


def is_away() -> bool:
    return state["away"] or in_schedule()


def away_text_for(lang: str) -> str:
    return state["texts"].get(lang) or cfg.away_text(lang)


def is_real_user(sender) -> bool:
    return (
        isinstance(sender, User)
        and not sender.bot
        and not getattr(sender, "is_self", False)
        and not sender.deleted
        and sender.id not in SERVICE_IDS
    )


def plan_reply(uid: str, text: str, lang: str, away: bool, now: float):
    """Решить, что ответить. Возвращает (текст, раздел_состояния, ключ) или None.

    Время ответа записывается сразу (до отправки), чтобы два быстрых
    сообщения подряд не привели к двум ответам.
    """
    if state["triggers"] and text:
        match = cfg.find_trigger(text, lang)
        if match and (away or not match.trigger.only_when_away):
            key = f"{uid}:{match.trigger.name}"
            if not cooldown_passed(state["trigger_replied"], key, now, match.trigger.cooldown_minutes):
                return None
            state["trigger_replied"][key] = now
            return pick_text(match.trigger.replies, match.lang, cfg.default_lang), "trigger_replied", key
    if away:
        if not cooldown_passed(state["away_replied"], uid, now, COOLDOWN_HOURS * 60):
            return None
        state["away_replied"][uid] = now
        return away_text_for(lang), "away_replied", uid
    return None


async def deliver(event, text: str, bucket: str, key: str) -> None:
    try:
        if DELAY_MAX > 0:  # «печатает...» и небольшая пауза — выглядит естественнее
            async with client.action(event.chat_id, "typing"):
                await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                await event.respond(text)
        else:
            await event.respond(text)
        log.info("Ответ отправлен (%s, %s)", bucket, key)
        save_state()
    except errors.FloodWaitError as e:
        state[bucket].pop(key, None)
        log.warning("Telegram просит подождать %s с — ответ пропущен", getattr(e, "seconds", "?"))
    except Exception:
        state[bucket].pop(key, None)  # не получилось — не считаем, что ответили
        log.exception("Не удалось отправить ответ")


# ------------------------------------------------------------------ обработчики

async def on_incoming(event) -> None:
    """Входящее личное сообщение."""
    away = is_away()
    if not away and not state["triggers"]:
        return
    sender = await event.get_sender()
    if not is_real_user(sender):
        return
    uid = str(sender.id)
    if uid in state["ignore"]:
        return

    now = time.time()
    text = event.raw_text or ""
    detected = detect_lang(text, cfg.latin_lang)
    if detected:
        state["lang"][uid] = [detected, now]
    lang = detected or (state["lang"].get(uid) or [cfg.default_lang])[0]

    plan = plan_reply(uid, text, lang, away, now)
    if plan:
        reply, bucket, key = plan
        await deliver(event, render(reply, sender.first_name), bucket, key)


async def resolve_user(arg: str):
    try:
        entity = await client.get_entity(int(arg) if arg.lstrip("-").isdigit() else arg)
    except (ValueError, errors.RPCError):
        return None
    return entity if isinstance(entity, User) else None


def status_text() -> str:
    start, end = SCHEDULE
    schedule = f"{AWAY_FROM}–{AWAY_TO} ({TIMEZONE})" if start and end else "нет"
    return (
        f"Вручную: {'включён' if state['away'] else 'выключен'}\n"
        f"Расписание: {schedule}\n"
        f"Сейчас отвечаю «не в сети»: {'да' if is_away() else 'нет'}\n"
        f"Триггеры: {'включены' if state['triggers'] else 'выключены'} (всего {len(cfg.triggers)})\n"
        f"Игнорируемых: {len(state['ignore'])}\n\n"
        f"Текст ru: {away_text_for('ru')}\nТекст uz: {away_text_for('uz')}"
    )


async def run_command(parts: list[str]) -> str:
    cmd = parts[1].lower() if len(parts) > 1 else "status"
    arg = parts[2] if len(parts) > 2 else ""

    if cmd == "on":
        state["away"] = True
        return "Автоответчик включён."
    if cmd == "off":
        state["away"] = False
        return "Автоответчик выключен (расписание продолжает работать)."
    if cmd == "status":
        return status_text()
    if cmd == "triggers" and arg.lower() in ("on", "off"):
        state["triggers"] = arg.lower() == "on"
        return f"Триггеры {'включены' if state['triggers'] else 'выключены'}."
    if cmd == "text":
        lang = arg.lower()
        value = parts[3].strip() if len(parts) > 3 else ""
        if lang not in SUPPORTED_LANGS or not value:
            return "Формат: /away text ru|uz <текст>  (или: /away text ru reset)"
        if value.lower() == "reset":
            state["texts"].pop(lang, None)
            return f"Текст ({lang}) возвращён к значению из config.json."
        state["texts"][lang] = value
        return f"Текст ({lang}) обновлён."
    if cmd in ("ignore", "unignore"):
        if not arg:
            return f"Формат: /away {cmd} <@username или id>"
        user = await resolve_user(arg)
        if not user:
            return "Не нашёл такого пользователя (для id он должен быть в ваших чатах)."
        uid = str(user.id)
        title = user.first_name or user.username or uid
        if cmd == "ignore":
            if uid not in state["ignore"]:
                state["ignore"].append(uid)
            return f"Не буду отвечать: {title}."
        if uid in state["ignore"]:
            state["ignore"].remove(uid)
        return f"Снова отвечаю: {title}."
    if cmd == "reset":
        state["away_replied"].clear()
        state["trigger_replied"].clear()
        return "Паузы между ответами сброшены."
    if cmd == "reload":
        global cfg
        try:
            cfg = Config.load(CONFIG_PATH)
        except (ConfigError, OSError) as e:
            return f"Ошибка в config.json, оставил прежние настройки:\n{e}"
        return f"config.json перечитан (триггеров: {len(cfg.triggers)})."
    return HELP


async def on_command(event) -> None:
    """Команды управления: сообщения в «Избранном», начинающиеся с /away."""
    parts = (event.raw_text or "").strip().split(maxsplit=3)
    if not parts or parts[0].lower() != "/away":
        return
    reply = await run_command(parts)
    save_state()
    await event.reply(reply)


# ------------------------------------------------------------------ запуск

def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name} (см. README.md)")
    return value


async def main() -> None:
    global client, state, cfg
    try:
        api_id = int(require_env("TG_API_ID"))
    except ValueError:
        raise SystemExit("TG_API_ID должен быть числом")
    api_hash = require_env("TG_API_HASH")
    session_string = os.environ.get("TG_SESSION_STRING", "").strip()

    try:
        cfg = load_config()
    except ConfigError as e:
        raise SystemExit(f"Ошибка в config.json: {e}")
    state = load_state()

    session = StringSession(session_string) if session_string else os.environ.get("TG_SESSION", "autoreply")
    client = TelegramClient(session, api_id, api_hash)
    client.add_event_handler(on_command, events.NewMessage(outgoing=True, chats="me"))
    client.add_event_handler(on_incoming, events.NewMessage(incoming=True, func=lambda e: e.is_private))

    # Сервер при деплое шлёт SIGTERM: аккуратно отключаемся, чтобы не оставлять «висящее» соединение
    try:
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGTERM, lambda: asyncio.ensure_future(client.disconnect())
        )
    except (NotImplementedError, RuntimeError):
        pass  # Windows не поддерживает

    if session_string:  # на сервере: входить с вводом кода нельзя
        await client.connect()
        if not await client.is_user_authorized():
            raise SystemExit("TG_SESSION_STRING недействителен — создайте новый через make_session.py")
    else:
        await client.start()  # при первом запуске спросит телефон и код

    me = await client.get_me()
    log.info("Автоответчик запущен для %s. Триггеров: %d. Состояние: %s",
             me.username or me.first_name, len(cfg.triggers), STATE_FILE)
    try:
        await client.run_until_disconnected()
    finally:
        save_state()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
