"""Тесты обработчиков на поддельных событиях (без подключения к Telegram).

    python -m pytest -q
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ["STATE_FILE"] = str(Path(tempfile.mkdtemp()) / "state.json")
os.environ["REPLY_DELAY_MIN"] = "0"
os.environ["REPLY_DELAY_MAX"] = "0"
os.environ["COOLDOWN_HOURS"] = "6"
os.environ["AWAY_FROM"] = ""
os.environ["AWAY_TO"] = ""

import pytest
from telethon.tl.types import User

import autoreply as ar
from triggers import Config

CONFIG_FILE = Path(__file__).with_name("config.json")


class FakeClient:
    async def get_entity(self, key):
        if str(key).lstrip("@").lower() == "nobody":
            raise ValueError("not found")
        return User(id=555, first_name="Vali")


class FakeEvent:
    def __init__(self, sender, text, fail=False):
        self._sender, self.raw_text, self.chat_id = sender, text, sender.id
        self.sent, self._fail = [], fail

    async def get_sender(self):
        return self._sender

    async def respond(self, text):
        if self._fail:
            raise RuntimeError("send failed")
        self.sent.append(text)


def user(uid=100, name="Ali", **kw):
    return User(id=uid, first_name=name, **kw)


def incoming(text, sender=None, fail=False):
    event = FakeEvent(sender or user(), text, fail)
    asyncio.run(ar.on_incoming(event))
    return event.sent


@pytest.fixture(autouse=True)
def fresh():
    ar.state = ar.default_state()
    ar.cfg = Config.load(CONFIG_FILE)
    ar.client = FakeClient()
    if ar.STATE_FILE.exists():
        ar.STATE_FILE.unlink()


def test_silent_when_online():
    assert incoming("Привет") == []


def test_away_text_ru_and_uz():
    ar.state["away"] = True
    assert incoming("Привет")[0].startswith("Здравствуйте, Ali!")
    assert incoming("Salom", user(101, "Vali"))[0].startswith("Assalomu alaykum, Vali!")


def test_away_cooldown():
    ar.state["away"] = True
    assert len(incoming("Привет")) == 1
    assert incoming("Ты тут?") == []


def test_trigger_replies_in_language():
    ar.state["away"] = True
    assert "цены" in incoming("Сколько стоит?")[0]
    assert "Narx" in incoming("Narxi qancha?", user(101))[0]
    assert "Narx" in incoming("Нарх қанча?", user(102))[0]  # узбекская кириллица -> ответ на узбекском


def test_trigger_cooldown_and_away_text_after():
    ar.state["away"] = True
    assert len(incoming("Сколько стоит?")) == 1
    assert incoming("Сколько это стоит?") == []                  # пауза триггера
    assert incoming("ок")[0].startswith("Здравствуйте, Ali!")    # а обычный автоответ ещё не отправлялся


def test_trigger_only_when_away_flag():
    ar.cfg = Config.from_dict({"triggers": [
        {"name": "faq", "only_when_away": False,
         "keywords": {"ru": ["faq"]}, "replies": {"ru": "Ответ из FAQ"}},
        {"name": "late", "keywords": {"ru": ["позже"]}, "replies": {"ru": "Позже"}},
    ]})
    assert incoming("faq") == ["Ответ из FAQ"]   # работает, даже когда я онлайн
    assert incoming("позже") == []               # только когда не в сети


def test_triggers_switch_off():
    ar.state["away"] = True
    ar.state["triggers"] = False
    assert incoming("Сколько стоит?")[0].startswith("Здравствуйте, Ali!")


def test_ignored_bots_and_service_accounts():
    ar.state["away"] = True
    ar.state["ignore"] = ["100"]
    assert incoming("Привет") == []
    assert incoming("Привет", user(200, bot=True)) == []
    assert incoming("Привет", user(777000)) == []
    assert incoming("Привет", user(300, deleted=True)) == []


def test_language_is_remembered_for_stickers():
    ar.state["away"] = True
    assert incoming("Salom")[0].startswith("Assalomu")
    ar.state["away_replied"].clear()
    assert incoming("")[0].startswith("Assalomu")  # стикер/фото без текста -> язык из прошлого сообщения


def test_failed_send_does_not_block_next_reply():
    ar.state["away"] = True
    assert incoming("Привет", fail=True) == []
    assert ar.state["away_replied"] == {}
    assert len(incoming("Привет")) == 1


def run(*parts):
    return asyncio.run(ar.run_command(["/away", *parts]))


def test_commands():
    assert "включён" in run("on") and ar.state["away"] is True
    assert "выключен" in run("off") and ar.state["away"] is False
    run("triggers", "off")
    assert ar.state["triggers"] is False
    assert "обновлён" in run("text", "uz", "Hozir yo'qman")
    assert ar.state["texts"]["uz"] == "Hozir yo'qman"
    ar.state["away"] = True
    assert incoming("Salom") == ["Hozir yo'qman"]
    assert "Формат" in run("text", "en", "x")
    assert "config.json" in run("text", "uz", "reset") and "uz" not in ar.state["texts"]
    assert "Не буду отвечать: Vali" in run("ignore", "@vali") and ar.state["ignore"] == ["555"]
    assert "Снова отвечаю" in run("unignore", "@vali") and ar.state["ignore"] == []
    assert "Не нашёл" in run("ignore", "@nobody")
    assert "перечитан" in run("reload")
    assert "Команды" in run("wat")
    assert "Триггеры" in run("status")


def test_state_roundtrip_and_old_format_migration():
    ar.state["ignore"] = ["1"]
    ar.state["lang"]["1"] = ["uz", 9e12]
    ar.save_state()
    assert ar.load_state()["ignore"] == ["1"]
    ar.STATE_FILE.write_text(json.dumps({"away": True, "text": "старый", "replied": {"7": 9e12}}), encoding="utf-8")
    loaded = ar.load_state()
    assert loaded["away"] is True and loaded["away_replied"] == {"7": 9e12} and "text" not in loaded


def test_old_entries_are_pruned():
    ar.state["away_replied"] = {"old": 1.0, "new": 9e12}
    ar.save_state()
    assert list(ar.load_state()["away_replied"]) == ["new"]


def test_save_state_creates_missing_folder(tmp_path, monkeypatch):
    target = tmp_path / "new" / "dir" / "state.json"   # как /data/state.json на пустом диске
    monkeypatch.setattr(ar, "STATE_FILE", target)
    ar.save_state()
    assert target.exists()
