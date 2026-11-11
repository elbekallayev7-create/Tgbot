"""Одноразовый вход в Telegram: печатает строку сессии (TG_SESSION_STRING).

Нужна для запуска на сервере (Render и т.п.), где нельзя ввести код из Telegram.
Строка даёт полный доступ к вашему аккаунту — храните её как пароль.

    python make_session.py
"""

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API ID: ").strip())
api_hash = input("API HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:  # спросит телефон и код
    print("\nВаша строка сессии (вставьте в переменную TG_SESSION_STRING):\n")
    print(client.session.save())
