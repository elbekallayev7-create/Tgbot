"""Триггеры и определение языка (русский / узбекский).

Модуль не зависит от Telegram, поэтому его легко тестировать.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_LANGS = ("ru", "uz")

DEFAULT_AWAY_TEXTS = {
    "ru": "Здравствуйте, {name}! Сейчас я не в сети. Увижу ваше сообщение и отвечу, как только смогу.",
    "uz": "Assalomu alaykum, {name}! Hozir aloqada emasman. Xabaringizni ko'rishim bilan javob beraman.",
}


class ConfigError(ValueError):
    """Ошибка в config.json."""


# ---------------------------------------------------------------- нормализация

# Все виды узбекских апострофов (oʻ, o‘, o’, o`) приводим к обычному '
_APOSTROPHES = str.maketrans({c: "'" for c in "ʻʼ’‘`´′"})


def normalize(text: str) -> str:
    text = text.lower().translate(_APOSTROPHES).replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- язык

_UZ_CYR_LETTERS = frozenset("ўқғҳ")  # буквы, которых нет в русском
_UZ_CYR_WORDS = re.compile(
    r"(?<!\w)(?:салом|ассалому|рахмат|нарх|канча|неча|яхши|керак|илтимос|хайр)"
)
_UZ_LAT_HINTS = re.compile(
    r"o'|g'|(?<![\w'])(?:salom|assalom|rahmat|qanday|qancha|necha|yaxshi|bormi|"
    r"kerak|mumkin|iltimos|xayr|narx|manzil)"
)
_RU_TRANSLIT = re.compile(
    r"(?<![\w'])(?:privet|zdravstvu|spasibo|skolko|pozhaluysta|pozhalujsta|dobryy|dobriy)"
)


def detect_lang(text: str, latin_lang: str = "uz") -> str | None:
    """Вернуть 'ru' или 'uz'; None, если в тексте нет букв (эмодзи, стикер и т.п.).

    Кириллица — русский (если нет узбекских букв/слов). Латиница — узбекский
    (в Узбекистане так пишут чаще всего), если не похоже на транслит русского;
    это поведение меняется параметром latin_lang.
    """
    t = normalize(text)
    if _UZ_CYR_LETTERS & set(t):
        return "uz"
    cyr = len(re.findall(r"[а-я]", t))
    lat = len(re.findall(r"[a-z]", t))
    if not cyr and not lat:
        return None
    if cyr >= lat:
        return "uz" if _UZ_CYR_WORDS.search(t) else "ru"
    if _UZ_LAT_HINTS.search(t):
        return "uz"
    if _RU_TRANSLIT.search(t):
        return "ru"
    return latin_lang


# ---------------------------------------------------------------- триггеры

def _compile(keywords: list[str]) -> re.Pattern | None:
    """Ключевое слово совпадает с НАЧАЛА слова: «narx» найдёт «narxi», «narxlar».

    Если слово заканчивается на «$», оно должно совпасть целиком («цена$»).
    Фраза из нескольких слов тоже работает («сколько стоит»).
    """
    parts = []
    for raw in keywords:
        kw = normalize(raw)
        whole = kw.endswith("$")
        kw = kw.rstrip("$").strip()
        if not kw:
            continue
        parts.append(r"(?<![\w'])" + re.escape(kw) + (r"(?![\w'])" if whole else ""))
    return re.compile("|".join(parts)) if parts else None


@dataclass
class Trigger:
    name: str
    replies: dict[str, str]
    patterns: dict[str, re.Pattern]
    only_when_away: bool = True
    cooldown_minutes: float = 30.0


@dataclass
class Match:
    trigger: Trigger
    lang: str


def _check_langs(mapping: dict, where: str) -> None:
    bad = sorted(set(mapping) - set(SUPPORTED_LANGS))
    if bad:
        raise ConfigError(f"{where}: неизвестные языки {bad}, допустимо {list(SUPPORTED_LANGS)}")


def pick_text(texts: dict[str, str], lang: str, default_lang: str = "ru") -> str:
    return texts.get(lang) or texts.get(default_lang) or next(iter(texts.values()))


class Config:
    def __init__(self, default_lang: str, latin_lang: str,
                 away_texts: dict[str, str], triggers: list[Trigger]):
        self.default_lang = default_lang
        self.latin_lang = latin_lang
        self.away_texts = away_texts
        self.triggers = triggers

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError(f"{path}: неверный JSON ({e})") from e
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        default_lang = data.get("default_lang", "ru")
        latin_lang = data.get("latin_lang", "uz")
        for key, value in (("default_lang", default_lang), ("latin_lang", latin_lang)):
            if value not in SUPPORTED_LANGS:
                raise ConfigError(f"{key}: «{value}» не поддерживается, допустимо {list(SUPPORTED_LANGS)}")

        away_texts = {**DEFAULT_AWAY_TEXTS, **data.get("away_texts", {})}
        _check_langs(away_texts, "away_texts")

        triggers: list[Trigger] = []
        seen: set[str] = set()
        for i, raw in enumerate(data.get("triggers", [])):
            name = raw.get("name")
            if not name:
                raise ConfigError(f"triggers[{i}]: не указано поле name")
            if name in seen:
                raise ConfigError(f"triggers[{i}]: имя «{name}» повторяется")
            seen.add(name)

            keywords, replies = raw.get("keywords", {}), raw.get("replies", {})
            _check_langs(keywords, f"«{name}».keywords")
            _check_langs(replies, f"«{name}».replies")
            patterns = {lang: p for lang, kws in keywords.items() if (p := _compile(kws))}
            if not patterns:
                raise ConfigError(f"«{name}»: нет ключевых слов")
            if not replies:
                raise ConfigError(f"«{name}»: нет текстов ответа (replies)")

            triggers.append(Trigger(
                name=name,
                replies=replies,
                patterns=patterns,
                only_when_away=bool(raw.get("only_when_away", True)),
                cooldown_minutes=float(raw.get("cooldown_minutes", 30)),
            ))
        return cls(default_lang, latin_lang, away_texts, triggers)

    def away_text(self, lang: str) -> str:
        return pick_text(self.away_texts, lang, self.default_lang)

    def find_trigger(self, text: str, preferred_lang: str) -> Match | None:
        """Первый подходящий триггер (порядок в config.json = приоритет)."""
        t = normalize(text)
        if not t:
            return None
        for trigger in self.triggers:
            matched = [lang for lang, pat in trigger.patterns.items() if pat.search(t)]
            if matched:
                lang = preferred_lang if preferred_lang in matched else matched[0]
                return Match(trigger, lang)
        return None


# ---------------------------------------------------------------- вспомогательное

def render(text: str, name: str | None) -> str:
    """Подставить {name}; без имени убрать его вместе с запятой."""
    name = (name or "").strip()
    if name:
        return text.replace("{name}", name)
    return re.sub(r"[,\s]*\{name\}", "", text).lstrip(" ,")


def cooldown_passed(history: dict[str, float], key: str, now: float, minutes: float) -> bool:
    return now - history.get(key, 0.0) >= minutes * 60
