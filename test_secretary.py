"""Тесты секретаря против локального имитатора Telegram Bot API: python -m pytest -q"""

import asyncio
import json
import time

import pytest

import secretary as ar
from botapi import BotAPI, BotAPIError
from conftest import CONN, OWNER, biz, dm, run
from triggers import Config


# ------------------------------------------------------------------ ответы клиентам

def test_silent_when_online(tg):
    run(biz("Привет"))
    assert tg.sent() == []


def test_away_reply_ru_and_notification(tg):
    ar.state["away"] = True
    run(biz("Привет", username="ali_k"))
    reply = tg.sent(100)[0]
    assert reply["text"].startswith("Здравствуйте, Ali!") and reply["business_connection_id"] == "c1"
    note = tg.sent(1)[0]                       # уведомление владельцу
    assert note["parse_mode"] == "HTML" and "Ali" in note["text"] and "@ali_k" in note["text"]
    assert "Привет" in note["text"] and "<code>100</code>" in note["text"] and "Отправил автоответ" in note["text"]


def test_away_reply_uz(tg):
    ar.state["away"] = True
    run(biz("Salom", uid=101, name="Vali"))
    assert tg.sent(101)[0]["text"].startswith("Assalomu alaykum, Vali!")


def test_cooldown_but_owner_still_notified(tg):
    ar.state["away"] = True
    run(biz("Привет"))
    run(biz("Ты тут?"))
    assert len(tg.sent(100)) == 1             # второй раз клиенту не отвечаем
    assert len(tg.sent(1)) == 2               # но владельцу передаём оба сообщения
    assert "не требовался" in tg.sent(1)[1]["text"]


def test_trigger_in_both_languages(tg):
    ar.state["away"] = True
    run(biz("Сколько стоит?"))
    run(biz("Narxi qancha?", uid=101))
    assert "цены" in tg.sent(100)[0]["text"] and "Narx" in tg.sent(101)[0]["text"]
    assert "price" in tg.sent(1)[0]["text"]


def test_only_when_away_false_trigger_works_online_without_notification(tg):
    ar.cfg = Config.from_dict({"triggers": [{"name": "faq", "only_when_away": False,
                                             "keywords": {"ru": ["faq"]}, "replies": {"ru": "Из FAQ"}}]})
    run(biz("faq"))
    assert [p["text"] for p in tg.sent()] == ["Из FAQ"]


def test_triggers_off(tg):
    ar.state["away"] = True
    ar.state["triggers"] = False
    run(biz("Сколько стоит?"))
    assert tg.sent(100)[0]["text"].startswith("Здравствуйте, Ali!")


def test_media_message_uses_label_and_remembered_language(tg):
    ar.state["away"] = True
    run(biz("Salom"))
    ar.state["away_replied"].clear()
    update = biz("", uid=100)
    update["business_message"].pop("text")
    update["business_message"]["photo"] = [{}]
    run(update)
    assert tg.sent(100)[1]["text"].startswith("Assalomu")
    assert "[photo]" in tg.sent(1)[1]["text"]


def test_notify_off(tg):
    ar.state["away"] = True
    ar.state["notify"] = False
    run(biz("Привет"))
    assert tg.sent(1) == [] and len(tg.sent(100)) == 1


# ------------------------------------------------------------------ не перебиваем, игнор, права

def test_owner_message_pauses_secretary_in_that_chat(tg):
    ar.state["away"] = True
    run(biz("Да, сейчас отвечу", uid=100, name="Owner", sender_id=1))   # написал сам владелец
    run(biz("Привет", uid=100))
    run(biz("Привет", uid=101))
    assert tg.sent(100) == [] and len(tg.sent(101)) == 1


def test_handoff_expires(tg):
    ar.state["away"] = True
    ar.state["owner_active"]["100"] = time.time() - 31 * 60
    run(biz("Привет"))
    assert len(tg.sent(100)) == 1


def test_ignore_bots_and_ignored_chats(tg):
    ar.state["away"] = True
    ar.state["ignore"] = ["100"]
    run(biz("Привет"))
    bot = biz("Привет", uid=102)
    bot["business_message"]["from"]["is_bot"] = True
    run(bot)
    assert tg.sent() == []


def test_no_reply_right_still_notifies_owner(tg):
    ar.state["away"] = True
    ar.remember_connection({**CONN, "can_reply": False})
    run(biz("Привет"))
    assert tg.sent(100) == [] and "нет права" in tg.sent(1)[0]["text"]


def test_disabled_connection_is_ignored(tg):
    ar.state["away"] = True
    ar.remember_connection({**CONN, "is_enabled": False})
    run(biz("Привет"))
    assert tg.sent() == []


def test_unknown_connection_is_fetched_from_api(tg):
    ar.state["away"] = True
    tg.connections["c9"] = {**CONN, "id": "c9"}
    run(biz("Привет", conn="c9"))
    assert tg.sent(100)[0]["business_connection_id"] == "c9"
    assert "c9" in ar.state["connections"]


def test_unknown_connection_unavailable(tg):
    ar.state["away"] = True
    run(biz("Привет", conn="nope"))
    assert tg.sent() == []


def test_failed_send_is_rolled_back(tg):
    ar.state["away"] = True
    tg.fail[("sendMessage", 100)] = {"error_code": 400, "description": "Bad Request: chat not found"}
    run(biz("Привет"))
    assert ar.state["away_replied"] == {}
    assert "Не удалось" in tg.sent(1)[0]["text"]
    tg.fail.clear()
    run(biz("Привет снова"))
    assert len(tg.sent(100)) == 2             # неудачная попытка + успешный ответ
    assert "100" in ar.state["away_replied"]  # теперь ответ засчитан


def test_connection_formats():
    assert ar.remember_connection({**CONN, "id": "a"})["can_reply"] is True
    new_format = {"id": "b", "user": OWNER, "rights": {"can_reply": False}, "is_enabled": True}
    assert ar.remember_connection(new_format)["can_reply"] is False
    without_rights = {"id": "d", "user": OWNER, "is_enabled": True}
    assert ar.remember_connection(without_rights)["can_reply"] is True


def test_connection_update_greets_owner(tg):
    run({"update_id": 3, "business_connection": {**CONN, "id": "new"}})
    assert "new" in ar.state["connections"] and "подключён" in tg.sent(1)[0]["text"]


# ------------------------------------------------------------------ «не в сети»

def test_idle_away(tg):
    ar.IDLE_MINUTES = 10
    ar.state["owner_seen"] = time.time() - 5 * 60
    assert not ar.is_away()
    ar.state["owner_seen"] = time.time() - 11 * 60
    assert ar.is_away()
    run(biz("Привет"))
    assert len(tg.sent(100)) == 1


def test_owner_activity_updates_seen(tg):
    ar.state["owner_seen"] = 1.0
    run(biz("ок", uid=100, sender_id=1))
    assert ar.state["owner_seen"] > 1.0


# ------------------------------------------------------------------ команды владельцу

def command(tg, text, sender_id=1):
    before = len(tg.sent())
    run(dm(text, sender_id))
    new = tg.sent()[before:]
    return new[0]["text"] if new else None


def test_owner_commands(tg):
    assert "вместо вас" in command(tg, "/on") and ar.state["away"] is True
    assert "выключен" in command(tg, "/away off") and ar.state["away"] is False
    assert "обновлён" in command(tg, "/text uz Hozir yo'qman")
    assert ar.state["texts"]["uz"] == "Hozir yo'qman"
    assert "config.json" in command(tg, "/text uz reset") and "uz" not in ar.state["texts"]
    assert "Формат" in command(tg, "/text en x")
    assert "Триггеры: выключено" in command(tg, "/triggers off") and ar.state["triggers"] is False
    assert "Пересылка" in command(tg, "/notify off") and ar.state["notify"] is False
    assert "не настроена" in command(tg, "/scan on")             # ключа VirusTotal нет
    assert "Молчу" in command(tg, "/ignore 555") and ar.state["ignore"] == ["555"]
    assert "Формат" in command(tg, "/ignore abc")
    assert "Снова" in command(tg, "/unignore 555") and ar.state["ignore"] == []
    assert "сброшены" in command(tg, "/reset")
    assert "перечитан" in command(tg, "/reload")
    assert "Вручную" in command(tg, "/status")
    assert "Команды" in command(tg, "/help")
    assert "Меню секретаря" in command(tg, "/start") and "Меню секретаря" in command(tg, "/menu")
    assert "Меню секретаря" in command(tg, "привет")             # любое сообщение открывает меню


def test_commands_from_strangers_are_not_executed(tg):
    # посторонний — это клиент прямого режима: команды не выполняются, бот отвечает как секретарь
    assert command(tg, "/on", sender_id=777).startswith("Assalomu alaykum, X!")  # латиница → узбекский
    assert ar.state["away"] is False


def test_owner_id_env_allows_commands_before_connection(tg):
    ar.state["connections"].clear()
    ar.OWNER_ID = 42
    assert "вместо вас" in command(tg, "/on", sender_id=42)


def test_command_with_bot_suffix(tg):
    assert "вместо вас" in command(tg, "/on@sekretar_bot")


# ------------------------------------------------------------------ Bot API, polling, webhook

def test_botapi_errors(tg):
    tg.fail[("sendMessage", None)] = {"error_code": 429, "description": "Too Many Requests",
                                      "parameters": {"retry_after": 7}}
    with pytest.raises(BotAPIError) as e:
        asyncio.run(ar.api.call("sendMessage", {"chat_id": 1, "text": "x"}))
    assert e.value.code == 429 and e.value.retry_after == 7 and "TOKEN" not in str(e.value)
    dead = BotAPI("TOKEN", base_url="http://127.0.0.1:9")
    with pytest.raises(BotAPIError) as e:
        asyncio.run(dead.call("getMe"))
    assert e.value.code == 0 and "TOKEN" not in str(e.value)


def test_poll_loop_end_to_end(tg):
    ar.state["away"] = True
    tg.batches = [[{**biz("Привет"), "update_id": 10}, {**biz("Salom", uid=101), "update_id": 11}]]

    async def scenario():
        try:
            await asyncio.wait_for(ar.poll_loop(), 1.0)
        except asyncio.TimeoutError:
            pass
        await asyncio.gather(*ar._tasks)

    asyncio.run(scenario())
    assert len(tg.sent(100)) == 1 and len(tg.sent(101)) == 1
    assert ar.state["offset"] == 12
    polls = [p for m, p in tg.calls if m == "getUpdates"]
    assert polls[0]["offset"] == 0 and polls[-1]["offset"] == 12
    assert "business_message" in polls[0]["allowed_updates"] and ("deleteWebhook", {}) in tg.calls


async def raw_http(port, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return data


def test_http_server_health_and_webhook(tg):
    ar.state["away"] = True
    ar.webhook_secret = ar.make_webhook_secret("TOKEN")
    payload = json.dumps({**biz("Привет"), "update_id": 20}).encode()

    def post(secret, path="/tg", body=payload):
        return (f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n"
                f"X-Telegram-Bot-Api-Secret-Token: {secret}\r\n\r\n").encode() + body

    async def scenario():
        assert await ar.start_http(None) is None
        server = await ar.start_http("0")
        port = server.sockets[0].getsockname()[1]
        out = {
            "get": await raw_http(port, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"),
            "head": await raw_http(port, b"HEAD / HTTP/1.1\r\nHost: x\r\n\r\n"),
            "bad_secret": await raw_http(port, post("wrong")),
            "no_secret": await raw_http(port, post("")),
            "bad_path": await raw_http(port, post(ar.webhook_secret, path="/other")),
            "bad_json": await raw_http(port, post(ar.webhook_secret, body=b"{oops")),
            "ok": await raw_http(port, post(ar.webhook_secret)),
            "junk": await raw_http(port, b"\x00\x01garbage\r\n\r\n"),
        }
        await asyncio.gather(*ar._tasks)
        server.close()
        return out

    out = asyncio.run(scenario())
    assert out["get"].startswith(b"HTTP/1.1 200") and out["get"].endswith(b"ok")
    assert out["head"].startswith(b"HTTP/1.1 200") and out["head"].endswith(b"\r\n\r\n")
    assert out["bad_secret"].startswith(b"HTTP/1.1 403") and out["no_secret"].startswith(b"HTTP/1.1 403")
    assert out["bad_path"].startswith(b"HTTP/1.1 404")
    assert out["bad_json"].startswith(b"HTTP/1.1 400")
    assert out["ok"].startswith(b"HTTP/1.1 200")
    assert out["junk"].startswith(b"HTTP/1.1 400")
    assert len(tg.sent(100)) == 1               # обновление из webhook обработано ровно один раз


def test_webhook_rejects_posts_when_secret_not_configured(tg):
    ar.webhook_secret = None

    async def scenario():
        server = await ar.start_http("0")
        port = server.sockets[0].getsockname()[1]
        data = await raw_http(port, b"POST /tg HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
        server.close()
        return data

    assert asyncio.run(scenario()).startswith(b"HTTP/1.1 404")


# ------------------------------------------------------------------ состояние

def test_state_roundtrip_and_pruning():
    ar.state["ignore"] = ["1"]
    ar.state["away_replied"] = {"old": 1.0, "new": 9e12}
    ar.state["owner_active"] = {"old": 1.0, "new": 9e12}
    ar.state["offset"] = 77
    ar.save_state()
    loaded = ar.load_state()
    assert loaded["ignore"] == ["1"] and loaded["offset"] == 77
    assert list(loaded["away_replied"]) == ["new"] and list(loaded["owner_active"]) == ["new"]
    assert "c1" in loaded["connections"]


def test_broken_state_file_falls_back_to_defaults():
    ar.STATE_FILE.write_text("{not json", encoding="utf-8")
    assert ar.load_state() == ar.default_state()


def test_save_state_creates_missing_folder(tmp_path, monkeypatch):
    target = tmp_path / "new" / "dir" / "state.json"
    monkeypatch.setattr(ar, "STATE_FILE", target)
    ar.save_state()
    assert target.exists()
