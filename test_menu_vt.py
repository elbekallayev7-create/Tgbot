"""Тесты меню из кнопок, проверки файлов (VirusTotal) и прямого режима: python -m pytest -q"""

import asyncio
import time

import pytest

import menu
import secretary as ar
from botapi import BotAPI, BotAPIError
from conftest import CLEAN, EVIL, biz, callback, dm, doc_update, enable_vt, run
from virustotal import VirusTotal, VTError, _multipart, report_url, summarize


def buttons(markup):
    return [b for row in markup["inline_keyboard"] for b in row]


def callbacks(markup):
    return [b["callback_data"] for b in buttons(markup)]


# ------------------------------------------------------------------ меню

def test_main_menu_shows_state_and_all_actions():
    text, markup = menu.main_menu(ar.default_state(), {"away_now": False, "vt": True, "connections": 1})
    assert "Меню секретаря" in text
    assert set(callbacks(markup)) == {"m:away", "m:trig", "m:notify", "m:scan", "m:texts", "m:ignore",
                                       "m:status", "m:reset", "m:reload", "m:help"}
    assert all(len(b["callback_data"]) <= 64 for b in buttons(markup))   # лимит Telegram
    state = ar.default_state()
    state["away"] = True
    labels = [b["text"] for b in buttons(menu.main_menu(state, {"away_now": True, "vt": False, "connections": 0})[1])]
    assert any("ВКЛ" in label for label in labels) and any("нужен ключ" in label for label in labels)


def test_menu_command_sends_keyboard(tg):
    run(dm("/menu"))
    sent = tg.sent(1)[-1]
    assert "Меню секретаря" in sent["text"] and "m:away" in callbacks(sent["reply_markup"])


def test_toggle_buttons_edit_the_same_message(tg):
    run(callback("m:away"))
    assert ar.state["away"] is True
    edit = tg.edits()[-1]
    assert edit["chat_id"] == 1 and edit["message_id"] == 50 and "ВКЛ" in buttons(edit["reply_markup"])[0]["text"]
    run(callback("m:trig"))
    run(callback("m:notify"))
    assert ar.state["triggers"] is False and ar.state["notify"] is False
    assert len(tg.answers()) == 3                      # на каждое нажатие — ответ, чтобы пропал «часик»


def test_buttons_work_only_for_owner(tg):
    run(callback("m:away", sender_id=777))
    assert ar.state["away"] is False and tg.edits() == []
    assert tg.answers()[-1]["text"] == "Нет доступа" and tg.answers()[-1]["show_alert"] is True


def test_status_help_reset_reload_pages(tg):
    ar.state["away_replied"]["1"] = time.time()
    run(callback("m:status"))
    assert "Вручную" in tg.edits()[-1]["text"] and callbacks(tg.edits()[-1]["reply_markup"]) == ["m:main"]
    run(callback("m:help"))
    assert "Команды" in tg.edits()[-1]["text"]
    run(callback("m:reset"))
    assert ar.state["away_replied"] == {} and "Меню секретаря" in tg.edits()[-1]["text"]
    run(callback("m:reload"))
    assert "перечитан" in tg.answers()[-1]["text"]


def test_edit_texts_through_buttons(tg):
    run(callback("m:texts"))
    assert "RU" in tg.edits()[-1]["text"] and "UZ" in tg.edits()[-1]["text"]
    run(callback("t:uz"))
    assert ar.state["awaiting"] == "text:uz" and "x:cancel" in callbacks(tg.edits()[-1]["reply_markup"])
    run(dm("Hozir yo'qman, {name}"))                   # следующее сообщение владельца — новый текст
    assert ar.state["texts"]["uz"] == "Hozir yo'qman, {name}" and ar.state["awaiting"] is None
    assert any("обновлён" in p["text"] for p in tg.sent(1))
    run(callback("tr:uz"))
    assert "uz" not in ar.state["texts"]


def test_cancel_text_input(tg):
    run(callback("t:ru"))
    run(callback("x:cancel"))
    assert ar.state["awaiting"] is None
    run(dm("просто сообщение"))
    assert ar.state["texts"] == {}


def test_command_cancels_text_input(tg):
    run(callback("t:ru"))
    run(dm("/status"))
    assert ar.state["awaiting"] is None


def test_ignore_list_buttons(tg):
    ar.state["ignore"] = ["111", "222"]
    run(callback("m:ignore"))
    assert callbacks(tg.edits()[-1]["reply_markup"]) == ["ui:111", "ui:222", "m:main"]
    run(callback("ui:111"))
    assert ar.state["ignore"] == ["222"]
    run(callback("ui:222"))
    assert "пуст" in tg.edits()[-1]["text"]


def test_ignore_button_under_notification(tg):
    ar.state["away"] = True
    run(biz("Привет"))
    note = tg.sent(1)[0]
    assert "ig:100" in callbacks(note["reply_markup"])
    run(callback("ig:100"))
    assert ar.state["ignore"] == ["100"]
    run(biz("Ещё раз", uid=100))
    assert len(tg.sent(100)) == 1                      # после игнора клиент больше ничего не получил


def test_scan_toggle_needs_key(tg):
    run(callback("m:scan"))
    assert "VT_API_KEY" in tg.answers()[-1]["text"] and tg.answers()[-1]["show_alert"] is True
    enable_vt(tg)
    run(callback("m:scan"))
    assert ar.state["scan"] is False


def test_unchanged_menu_is_not_an_error(tg):
    tg.fail[("editMessageText", None)] = {"error_code": 400, "description": "Bad Request: message is not modified"}
    run(callback("m:main"))                            # не должно падать и ругаться
    assert len(tg.answers()) == 1


# ------------------------------------------------------------------ VirusTotal: клиент

def test_summarize_levels():
    assert summarize(CLEAN)[0] == "ok" and "0 из 70" in summarize(CLEAN)[1]
    assert summarize(EVIL)[0] == "bad" and "12 из 70" in summarize(EVIL)[1]
    assert summarize({"malicious": 1, "undetected": 60})[0] == "warn"
    assert summarize({"suspicious": 2, "undetected": 60})[0] == "warn"
    assert summarize({})[0] == "unknown"


def test_multipart_is_well_formed():
    body, content_type = _multipart('evil".exe\r\n', b"DATA")
    boundary = content_type.split("boundary=")[1]
    assert body.startswith(f"--{boundary}\r\n".encode()) and body.endswith(f"\r\n--{boundary}--\r\n".encode())
    assert b"DATA" in body and b'filename="evil\'.exe  "' in body
    assert b"?" in _multipart("файл.pdf", b"x")[0]     # кириллица в имени заменяется (важен хэш)


def test_vt_client_lookup_upload_wait(tg):
    sha = tg.add_file("f", b"abc")
    tg.vt_known[sha] = CLEAN
    client = VirusTotal("VTKEY", base_url=tg.url + "/api/v3", min_interval=0)

    async def scenario():
        assert (await client.lookup(sha))["stats"] == CLEAN
        assert await client.lookup("0" * 64) is None
        analysis_id = await client.upload("a.bin", b"payload")
        tg.vt_polls_before_done = 2
        return analysis_id, await client.wait_analysis(analysis_id, interval=0)

    analysis_id, stats = asyncio.run(scenario())
    assert analysis_id == "an1" and stats == CLEAN and b"payload" in tg.vt_uploads[0]
    assert tg.vt_key_seen == {"VTKEY"}


def test_vt_client_wait_timeout_and_errors(tg):
    client = VirusTotal("VTKEY", base_url=tg.url + "/api/v3", min_interval=0)
    tg.vt_polls_before_done = 99
    assert asyncio.run(client.wait_analysis("an1", interval=0, attempts=3)) is None
    tg.vt_fail["lookup"] = (429, "Quota exceeded")
    with pytest.raises(VTError) as e:
        asyncio.run(client.lookup("0" * 64))
    assert e.value.code == 429 and "VTKEY" not in str(e.value)
    dead = VirusTotal("VTKEY", base_url="http://127.0.0.1:9/api/v3", min_interval=0)
    with pytest.raises(VTError) as e:
        asyncio.run(dead.lookup("0" * 64))
    assert e.value.code == 0 and "VTKEY" not in str(e.value)


def test_vt_client_respects_min_interval(tg):
    client = VirusTotal("VTKEY", base_url=tg.url + "/api/v3", min_interval=0.3)

    async def scenario():
        started = time.monotonic()
        await client.lookup("0" * 64)
        await client.lookup("1" * 64)
        return time.monotonic() - started

    assert asyncio.run(scenario()) >= 0.3


def test_report_url():
    assert report_url("ab") == "https://www.virustotal.com/gui/file/ab"


def test_botapi_download_limits(tg):
    tg.add_file("big", b"x" * 100)
    with pytest.raises(BotAPIError):
        asyncio.run(ar.api.download("docs/big", 10))
    with pytest.raises(BotAPIError) as e:
        asyncio.run(ar.api.download("docs/missing", 10))
    assert e.value.code == 404 and "TOKEN" not in str(e.value)


# ------------------------------------------------------------------ VirusTotal: сценарии бота

def test_known_clean_file_from_customer_reported_to_owner_even_when_online(tg):
    enable_vt(tg)
    tg.vt_known[tg.add_file("f1", b"clean file")] = CLEAN
    run(doc_update("f1", "report.pdf"))                 # владелец «онлайн», автоответа нет — но файл проверен
    assert tg.sent(100) == [] and tg.vt_uploads == []   # загружать ничего не пришлось
    verdict = tg.sent(1)[0]
    assert "report.pdf" in verdict["text"] and "✅" in verdict["text"] and "Ali" in verdict["text"]
    assert "virustotal.com/gui/file/" in verdict["text"] and verdict["parse_mode"] == "HTML"


def test_known_malicious_file_is_flagged(tg):
    enable_vt(tg)
    tg.vt_known[tg.add_file("f1", b"evil")] = EVIL
    run(doc_update("f1", "invoice.exe"))
    assert "🚫" in tg.sent(1)[0]["text"] and "НЕ открывайте" in tg.sent(1)[0]["text"]


def test_unknown_file_asks_before_upload_then_uploads_on_button(tg):
    enable_vt(tg)
    tg.add_file("f1", b"never seen before")
    run(doc_update("f1", "new.zip"))
    ask = tg.sent(1)[0]
    assert "ещё не видел" in ask["text"] and tg.vt_uploads == []        # сам файл никуда не ушёл
    up = [c for c in callbacks(ask["reply_markup"]) if c.startswith("vt:up:")][0]
    token = up.split(":")[2]
    assert token in ar.state["pending"] and len(up) <= 64

    tg.vt_polls_before_done = 1
    run(callback(up, message_id=77))
    assert b"never seen before" in tg.vt_uploads[0] and token not in ar.state["pending"]
    texts = [e["text"] for e in tg.edits() if e["message_id"] == 77]
    assert "Загружаю" in texts[0] and "✅" in texts[-1]


def test_skip_button_does_not_upload(tg):
    enable_vt(tg)
    tg.add_file("f1", b"data")
    run(doc_update("f1"))
    token = [c for c in callbacks(tg.sent(1)[0]["reply_markup"]) if c.startswith("vt:no:")][0].split(":")[2]
    run(callback(f"vt:no:{token}", message_id=5))
    assert tg.vt_uploads == [] and token not in ar.state["pending"] and "не загружался" in tg.edits()[-1]["text"]


def test_stale_pending_token(tg):
    enable_vt(tg)
    run(callback("vt:up:deadbeef", message_id=5))
    assert "устарел" in tg.edits()[-1]["text"] and tg.vt_uploads == []


def test_auto_upload_setting(tg):
    enable_vt(tg)
    ar.VT_AUTO_UPLOAD = True
    tg.vt_upload_stats = EVIL
    tg.add_file("f1", b"fresh malware")
    run(doc_update("f1", "x.apk"))
    assert len(tg.vt_uploads) == 1 and "🚫" in tg.sent(1)[0]["text"]


def test_owner_sent_file_is_checked_and_uploaded_without_asking(tg):
    enable_vt(tg)
    tg.add_file("f1", b"my own file")
    run(doc_update("f1", "mine.docx", as_dm=True, sender_id=1))
    texts = [p["text"] for p in tg.sent(1)]
    assert "Проверяю" in texts[0] and "✅" in texts[1] and len(tg.vt_uploads) == 1


def test_owner_sent_file_without_vt_key(tg):
    run(doc_update("f1", as_dm=True, sender_id=1))
    assert "VT_API_KEY" in tg.sent(1)[0]["text"]


def test_scan_does_nothing_without_key_or_when_switched_off(tg):
    tg.add_file("f1", b"data")
    run(doc_update("f1"))
    assert [c for c in tg.calls if c[0] == "getFile"] == []
    enable_vt(tg)
    ar.state["scan"] = False
    run(doc_update("f1"))
    assert [c for c in tg.calls if c[0] == "getFile"] == []


def test_ignored_chat_files_are_not_scanned(tg):
    enable_vt(tg)
    ar.state["ignore"] = ["100"]
    tg.add_file("f1", b"data")
    run(doc_update("f1"))
    assert [c for c in tg.calls if c[0] == "getFile"] == []


def test_too_big_file_is_not_downloaded(tg):
    enable_vt(tg)
    run(doc_update("f1", "huge.iso", size=ar.VT_MAX_BYTES + 1))
    assert "больше" in tg.sent(1)[0]["text"] and [c for c in tg.calls if c[0] == "getFile"] == []


def test_telegram_refuses_file(tg):
    enable_vt(tg)
    run(doc_update("unknown_id", "x.bin"))             # имитатор: «file is too big»
    assert "Не удалось получить файл" in tg.sent(1)[0]["text"]


def test_vt_rate_limit_and_bad_key_messages(tg):
    enable_vt(tg)
    tg.add_file("f1", b"data")
    tg.vt_fail["lookup"] = (429, "Quota exceeded")
    run(doc_update("f1"))
    assert "лимит запросов" in tg.sent(1)[-1]["text"] and "не проверен" in tg.sent(1)[-1]["text"]
    tg.vt_fail["lookup"] = (401, "Wrong API key")
    run(doc_update("f1", uid=101))
    assert "отклонил ключ" in tg.sent(1)[-1]["text"]


def test_daily_quota(tg):
    enable_vt(tg)
    ar.VT_DAILY_LIMIT = 1
    tg.vt_known[tg.add_file("f1", b"one")] = CLEAN
    run(doc_update("f1"))
    run(doc_update("f1", uid=101))
    assert "✅" in tg.sent(1)[0]["text"] and "Суточный лимит" in tg.sent(1)[1]["text"]
    assert ar.state["vt_day"]["n"] == 1


def test_upload_failure_is_reported_in_place(tg):
    enable_vt(tg)
    ar.state["pending"]["tok"] = {"file_id": "f1", "name": "a.bin", "label": "Ali", "t": time.time()}
    tg.add_file("f1", b"data")
    tg.vt_fail["upload"] = (429, "Quota exceeded")
    run(callback("vt:up:tok", message_id=9))
    assert "лимит запросов" in tg.edits()[-1]["text"]


def test_file_notification_shows_file_name(tg):
    ar.state["away"] = True
    run(doc_update("f1", "contract.pdf"))
    assert "[файл: contract.pdf]" in tg.sent(1)[0]["text"]


# ------------------------------------------------------------------ прямой режим (без Business и Premium)

def stranger(text, uid=777, name="Vali", username=None):
    return dm(text, sender_id=uid, name=name, username=username)


def test_direct_mode_replies_without_business_connection_and_forwards_to_owner(tg):
    ar.state["connections"].clear()
    ar.OWNER_ID = 1
    run(stranger("Привет, Али"))
    reply = tg.sent(777)[0]
    assert reply["text"].startswith("Здравствуйте, Vali!") and "business_connection_id" not in reply
    note = tg.sent(1)[0]
    assert "Vali" in note["text"] and "<code>777</code>" in note["text"] and "Reply" in note["text"]
    (chat_id, _), = ar.state["relay"].values()          # уведомление запомнено для ответа через Reply
    assert chat_id == 777


def test_direct_mode_triggers_work(tg):
    run(stranger("Сколько стоит?"))
    assert "цены" in tg.sent(777)[0]["text"]


def test_direct_mode_owner_replies_via_reply(tg):
    run(stranger("Салом, нарх қанча?", name="Vali"))
    note_id = int(list(ar.state["relay"])[0])
    update = dm("Добрый день! Цена 100 000 сум.", sender_id=1, reply_to_message={"message_id": note_id})
    run(update)
    assert tg.sent(777)[-1]["text"] == "Добрый день! Цена 100 000 сум."
    assert "Отправлено клиенту" in tg.sent(1)[-1]["text"]


def test_reply_to_unknown_message_just_shows_menu(tg):
    run(dm("текст", sender_id=1, reply_to_message={"message_id": 12345}))
    assert "Меню секретаря" in tg.sent(1)[-1]["text"] and tg.sent(777) == []


def test_direct_mode_start_does_not_spam_owner(tg):
    run(stranger("/start"))
    assert len(tg.sent(777)) == 1 and tg.sent(1) == []


def test_direct_mode_ignores_groups_and_bots(tg):
    run(dm("привет", sender_id=-100, chat_type="group"))
    bot = stranger("привет")
    bot["message"]["from"]["is_bot"] = True
    run(bot)
    assert tg.sent() == []


def test_direct_mode_file_from_stranger_is_scanned(tg):
    enable_vt(tg)
    tg.vt_known[tg.add_file("f1", b"stranger file")] = EVIL
    run(doc_update("f1", "foto.scr", as_dm=True, sender_id=777, name="Vali"))
    assert any("🚫" in p["text"] and "Vali" in p["text"] for p in tg.sent(1))


def test_owner_username_is_recognised_and_remembered(tg):
    ar.state["connections"].clear()
    ar.OWNER_USERNAME = "boss"
    run(stranger("/on", uid=42, username="not_boss"))                 # чужой username — не владелец
    assert ar.state["away"] is False
    run(dm("/on", sender_id=42, username="Boss"))                      # регистр не важен
    assert ar.state["away"] is True and ar.state["owner_ids"] == [42]
    run(callback("m:away", sender_id=42, chat_id=42))                  # дальше узнаём по id
    assert ar.state["away"] is False


def test_notifications_go_to_learned_owner(tg):
    ar.state["connections"].clear()
    ar.OWNER_USERNAME = "boss"
    run(dm("/menu", sender_id=42, username="boss"))
    run(stranger("Привет"))
    assert any("Vali" in p["text"] for p in tg.sent(42))


def test_no_owner_known_does_not_crash(tg):
    ar.state["connections"].clear()
    run(stranger("Привет"))
    assert len(tg.sent(777)) == 1


def test_relay_and_pending_are_pruned(tg):
    ar.state["relay"] = {str(i): [i, time.time() - i] for i in range(400)}
    ar.state["relay"]["old"] = [1, 1.0]
    ar.state["pending"] = {"old": {"file_id": "x", "name": "n", "t": 1.0},
                           "new": {"file_id": "y", "name": "m", "t": time.time()}}
    ar.save_state()
    loaded = ar.load_state()
    assert len(loaded["relay"]) == 300 and "old" not in loaded["relay"]
    assert list(loaded["pending"]) == ["new"]
