from i18n.en import TRANSLATIONS as EN
from i18n.he import TRANSLATIONS as HE
from user_prefs import get_language

_TRANSLATIONS: dict[str, dict[str, str]] = {"en": EN, "he": HE}


def t(key: str, locale: str | None = None, **kwargs: object) -> str:
    # No explicit locale: render in the user's configured language, not a
    # hard-coded English default. get_language floors to "en" on its own.
    if not locale:
        locale = get_language()
    translations = _TRANSLATIONS.get(locale, EN)
    text = translations.get(key, EN.get(key, key))
    if kwargs:
        for k, v in kwargs.items():
            text = text.replace(f"{{{k}}}", str(v))
    return text
