"""
Нормализация текста и имён в общее «скелетное» латинское представление.

Зачем: в списке получателей и в новостях одно и то же имя пишется по-разному —
кириллицей и латиницей, с разной транслитерацией (KH/H, Y/I, ZH/J, Q/K),
с пунктуацией и правовой формой. Поэтому и имена, и тексты новостей
приводятся к одному виду, и сравнение идёт уже в нём.

  "ТОО «Альфа-Трейд»"         -> " too alfa treid "
  "YESHMURATOV ISLAM"         -> " eshmuratov islam "
  "Ешмуратов Ислам"           -> " eshmuratov islam "
  "Юлия" / "ULIA" / "YULIYA"  -> " ula "

Результат не предназначен для показа человеку — только для сравнения.
Для сниппетов хранится карта позиций обратно в исходный текст.
"""
import re
import unicodedata

_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # казахские
    "ә": "a", "ғ": "g", "қ": "k", "ң": "n", "ө": "o", "ұ": "u", "ү": "u",
    "һ": "h", "і": "i",
    # украинские / прочие
    "є": "e", "ї": "i", "ґ": "g", "ў": "u",
    # турецкие/азербайджанские, которых NFKD не раскладывает
    "ı": "i", "ə": "a", "ß": "ss", "ø": "o", "æ": "ae", "đ": "d", "ł": "l",
}

_char_cache: dict[str, str] = {}


def _char_rep(ch: str) -> str:
    rep = _char_cache.get(ch)
    if rep is not None:
        return rep
    c = ch.lower()
    if len(c) != 1:          # редкие символы, у которых lower() даёт 2 знака
        c = c[0]
    if c in _CYR:
        rep = _CYR[c]
    elif c.isascii():
        rep = c if c.isalnum() else " "
    elif c.isalnum():
        d = unicodedata.normalize("NFKD", c)
        rep = "".join(x for x in d if x.isascii() and x.isalnum()) or " "
    else:
        rep = " "
    _char_cache[ch] = rep
    return rep


# Порядок проходов важен: сначала многобуквенные сочетания, затем
# y->i, затем схлопывание удвоенных букв, затем выпадение i перед гласной.
_FOLD1 = re.compile(r"dzh|shch|kh|zh|ts|ph|q|w|y")
_FOLD1_MAP = {"dzh": "j", "shch": "sh", "kh": "h", "zh": "j", "ts": "c",
              "ph": "f", "q": "k", "w": "v", "y": "i"}
_FOLD2 = re.compile(r"([a-z])\1+")
_FOLD3 = re.compile(r"i(?=[aeiou])")


def _sub_with_map(s, idx, pattern, repl):
    out, oidx, pos = [], [], 0
    for m in pattern.finditer(s):
        a, b = m.start(), m.end()
        out.append(s[pos:a])
        oidx.extend(idx[pos:a])
        r = repl(m)
        out.append(r)
        oidx.extend([idx[a]] * len(r))
        pos = b
    out.append(s[pos:])
    oidx.extend(idx[pos:])
    return "".join(out), oidx


def normalize_with_map(text: str):
    """Возвращает (norm, idx): norm начинается и заканчивается пробелом,
    слова разделены одним пробелом; idx[i] — позиция символа norm[i] в text."""
    out = [" "]
    idx = [0]
    prev_space = True
    for i, ch in enumerate(text):
        for r in _char_rep(ch):
            if r == " ":
                if prev_space:
                    continue
                prev_space = True
            else:
                prev_space = False
            out.append(r)
            idx.append(i)
    if not prev_space:
        out.append(" ")
        idx.append(max(len(text) - 1, 0))
    s = "".join(out)
    s, idx = _sub_with_map(s, idx, _FOLD1, lambda m: _FOLD1_MAP[m.group(0)])
    s, idx = _sub_with_map(s, idx, _FOLD2, lambda m: m.group(1))
    s, idx = _sub_with_map(s, idx, _FOLD3, lambda m: "")
    return s, idx


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


_VOWELS = "aeiou"


def stem(token: str) -> str:
    """Грубая основа для падежей: срезаем конечные гласные.
    ivanova -> ivanov (подходит к ivanov, ivanovoi, ivanovu, ivanova)."""
    return token.rstrip(_VOWELS) or token
