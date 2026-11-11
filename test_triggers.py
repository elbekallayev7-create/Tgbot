"""Тесты логики триггеров и языка: python -m pytest -q"""

from pathlib import Path

import pytest

from triggers import Config, ConfigError, cooldown_passed, detect_lang, normalize, render

CONFIG = Config.load(Path(__file__).with_name("config.json"))


def test_normalize():
    assert normalize("O‘zbek  ТИЛИ") == "o'zbek тили"
    assert normalize("Ёлка") == "елка"
    assert normalize("Oʻzbek, g`oya") == "o'zbek, g'oya"


@pytest.mark.parametrize("text,expected", [
    ("Привет, как дела?", "ru"),
    ("Здравствуйте", "ru"),
    ("Assalomu alaykum", "uz"),
    ("salom", "uz"),
    ("Салом, нархи қанча?", "uz"),   # узбекская кириллица (есть қ)
    ("рахмат", "uz"),                # узбекское слово кириллицей
    ("O‘zbekcha yozaman", "uz"),
    ("privet", "ru"),                # русский транслитом
    ("👍", None),
    ("", None),
])
def test_detect_lang(text, expected):
    assert detect_lang(text) == expected


def test_latin_default_is_configurable():
    assert detect_lang("hello", "uz") == "uz"
    assert detect_lang("hello", "ru") == "ru"


@pytest.mark.parametrize("text,trigger,lang", [
    ("Сколько стоит?", "price", "ru"),
    ("Сколько это будет стоить?", "price", "ru"),
    ("Какая цена?", "price", "ru"),
    ("Narxi qancha?", "price", "uz"),
    ("Qancha turadi", "price", "uz"),
    ("неча сўм", "price", "uz"),
    ("Срочно нужна помощь", "urgent", "ru"),
    ("Shoshilinch savol bor", "urgent", "uz"),
    ("Где вы находитесь?", "address", "ru"),
    ("Manzilingiz qayerda?", "address", "uz"),
    ("Qo‘ng‘iroq qilsam bo'ladimi", "contact", "uz"),
    ("Свяжитесь со мной", "contact", "ru"),
    ("до скольки работаете", "hours", "ru"),
    ("Ish vaqti qanday?", "hours", "uz"),
])
def test_find_trigger(text, trigger, lang):
    match = CONFIG.find_trigger(text, detect_lang(text) or "ru")
    assert match is not None
    assert (match.trigger.name, match.lang) == (trigger, lang)


@pytest.mark.parametrize("text", [
    "Привет, как дела?",
    "Центр города",         # «цена$» — только целое слово, «центр» не должен срабатывать
    "Qachon ko'rishamiz?",
    "ok",
])
def test_no_trigger(text):
    assert CONFIG.find_trigger(text, "ru") is None


def test_priority_follows_config_order():
    # «срочно» стоит в конфиге раньше «цены»
    assert CONFIG.find_trigger("Срочно, какая цена?", "ru").trigger.name == "urgent"


def test_shared_keyword_uses_preferred_language():
    assert CONFIG.find_trigger("urgent", "uz").lang == "uz"
    assert CONFIG.find_trigger("urgent", "ru").lang == "ru"


def test_render():
    assert render("Привет, {name}! Как дела?", "Али") == "Привет, Али! Как дела?"
    assert render("Привет, {name}! Как дела?", None) == "Привет! Как дела?"
    assert render("{name}, salom", "") == "salom"


def test_cooldown():
    history = {"a": 1000.0}
    assert not cooldown_passed(history, "a", 1000.0 + 59 * 60, 60)
    assert cooldown_passed(history, "a", 1000.0 + 60 * 60, 60)
    assert cooldown_passed(history, "unknown", 1_700_000_000.0, 60)


def test_config_errors():
    with pytest.raises(ConfigError):
        Config.from_dict({"triggers": [{"keywords": {"ru": ["x"]}, "replies": {"ru": "y"}}]})  # нет name
    with pytest.raises(ConfigError):
        Config.from_dict({"triggers": [{"name": "a", "keywords": {"en": ["x"]}, "replies": {"ru": "y"}}]})
    with pytest.raises(ConfigError):
        Config.from_dict({"triggers": [{"name": "a", "keywords": {"ru": ["x"]}, "replies": {}}]})
    with pytest.raises(ConfigError):
        Config.from_dict({"default_lang": "en"})


def test_default_texts_without_config():
    cfg = Config.from_dict({})
    assert "Assalomu" in cfg.away_text("uz")
    assert "Здравствуйте" in cfg.away_text("ru")
