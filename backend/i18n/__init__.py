from i18n.en import TRANSLATIONS as EN
from i18n.he import TRANSLATIONS as HE

_TRANSLATIONS: dict[str, dict[str, str]] = {"en": EN, "he": HE}
DEFAULT_LOCALE = "en"


def t(key: str, locale: str | None = None, **kwargs: object) -> str:
    locale = locale or DEFAULT_LOCALE
    translations = _TRANSLATIONS.get(locale, EN)
    text = translations.get(key, EN.get(key, key))
    if kwargs:
        for k, v in kwargs.items():
            text = text.replace(f"{{{k}}}", str(v))
    return text
