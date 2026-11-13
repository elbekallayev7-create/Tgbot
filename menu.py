"""Меню из кнопок (inline-клавиатуры) для владельца. Только построение текста и кнопок."""

from __future__ import annotations


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def kb(*rows) -> dict:
    return {"inline_keyboard": [list(row) for row in rows]}


BACK = btn("⬅️ Назад", "m:main")


def _onoff(value: bool) -> str:
    return "вкл" if value else "выкл"


def main_menu(state: dict, info: dict) -> tuple[str, dict]:
    away = state["away"]
    scan = f"🛡 Проверка файлов: {_onoff(state['scan'])}" if info["vt"] else "🛡 Проверка файлов: нужен ключ"
    text = (
        "🗂 Меню секретаря\n\n"
        f"Сейчас «не в сети»: {'да' if info['away_now'] else 'нет'}\n"
        f"Подключений Business: {info['connections']}"
    )
    return text, kb(
        [btn(f"{'🔴' if away else '⚪'} Не в сети: {'ВКЛ' if away else 'выкл'}", "m:away")],
        [btn(f"⚡ Триггеры: {_onoff(state['triggers'])}", "m:trig"),
         btn(f"📩 Пересылка: {_onoff(state['notify'])}", "m:notify")],
        [btn(scan, "m:scan")],
        [btn("✏️ Тексты", "m:texts"), btn(f"🚫 Игнор ({len(state['ignore'])})", "m:ignore")],
        [btn("📊 Состояние", "m:status"), btn("🔄 Сбросить паузы", "m:reset")],
        [btn("♻️ Перечитать config", "m:reload"), btn("❓ Помощь", "m:help")],
    )


def texts_menu(text_ru: str, text_uz: str) -> tuple[str, dict]:
    def cut(value: str) -> str:
        return value if len(value) <= 300 else value[:300] + "…"

    text = f"✏️ Тексты автоответа\n\n🇷🇺 RU:\n{cut(text_ru)}\n\n🇺🇿 UZ:\n{cut(text_uz)}\n\n{{name}} — имя собеседника."
    return text, kb(
        [btn("✏️ Изменить RU", "t:ru"), btn("✏️ Изменить UZ", "t:uz")],
        [btn("↩️ RU по умолчанию", "tr:ru"), btn("↩️ UZ по умолчанию", "tr:uz")],
        [BACK],
    )


def prompt_page(lang: str) -> tuple[str, dict]:
    return (f"Отправьте новый текст автоответа ({lang}) одним сообщением.\n"
            "Можно использовать {name} — туда подставится имя собеседника."), kb([btn("❌ Отмена", "x:cancel")])


def ignore_menu(ignored: list[str]) -> tuple[str, dict]:
    if not ignored:
        return ("🚫 Игнор-лист пуст.\n\nЧат можно добавить кнопкой «Не отвечать этому чату» под уведомлением "
                "или командой /ignore <id>."), kb([BACK])
    rows = [[btn(f"🔓 Снова отвечать: {chat_id}", f"ui:{chat_id}")] for chat_id in ignored[:20]]
    return "🚫 Чаты, где секретарь молчит:", kb(*rows, [BACK])


def simple_page(text: str) -> tuple[str, dict]:
    return text, kb([BACK])
