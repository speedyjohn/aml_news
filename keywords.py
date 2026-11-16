"""Поиск ключевых слов нарушений в нормализованном тексте."""
import re

import config
from normalize import normalize, stem


def _kw_pattern(phrase: str) -> str:
    toks = normalize(phrase).split()
    if not toks:
        return ""
    toks[-1] = stem(toks[-1])     # последнее слово — как основа
    return " " + " ".join(toks)


_ENTRIES = []   # (pattern, label, category)
for cat, words in config.KEYWORDS.items():
    for w in words:
        p = _kw_pattern(w)
        if len(p.strip()) >= 3:
            _ENTRIES.append((p, w, cat))

_LOOKUP = {}
for p, w, cat in _ENTRIES:
    _LOOKUP.setdefault(p, (w, cat))

_RE = re.compile("|".join(re.escape(p) for p in sorted(_LOOKUP, key=len, reverse=True)))


def find(norm: str):
    """Список (позиция, слово, категория) в нормализованном тексте."""
    return [(m.start(), *_LOOKUP[m.group(0)]) for m in _RE.finditer(norm)]


def has_any(text: str) -> bool:
    return _RE.search(normalize(text)) is not None
