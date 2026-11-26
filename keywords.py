"""Поиск ключевых слов в нормализованном тексте."""
import re

import config
from normalize import normalize, stem


# ---------------------------------------------------------------------------
# Ключевые слова нарушений (ОД/ФТ) для матчинга по списку получателей
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Универсальный словарь «группа -> слова» (негативные новости, регионы)
# ---------------------------------------------------------------------------
_FLEX = "[a-z0-9]{0,5}"   # допустимое окончание у слов внутри фразы


def phrase_regex(phrase: str, use_stem: bool = True) -> str | None:
    """Регулярка для фразы в нормализованном тексте (он начинается и
    заканчивается пробелом, слова разделены одним пробелом).

      «удорожан»   -> начало слова
      «срыв срок»  -> у каждого слова, кроме последнего, допустимо окончание
      «семей$»     -> слово целиком
    use_stem=False — не срезать конечные гласные (для названий мест:
    «бейнеу» иначе превратилось бы в «bein» и ловило бы английское being).
    """
    exact = phrase.rstrip().endswith("$")
    toks = normalize(phrase.rstrip().rstrip("$")).split()
    if not toks:
        return None
    parts = []
    for i, t in enumerate(toks):
        last = i == len(toks) - 1
        # основа короче 4 букв ловит слишком много («ақау» -> «ak»): берём слово целиком
        base = stem(t) if use_stem else t
        if len(base) < 4:
            base = t
        if last:
            parts.append(re.escape(t) + "(?= )" if exact else re.escape(base))
        else:
            parts.append(re.escape(base) + (_FLEX if len(t) >= 4 else ""))
    rx = " " + " ".join(parts)
    # слишком короткие основы дают шум
    if len("".join(toks)) < 3:
        return None
    return rx


class KeywordIndex:
    """Индекс «группа -> список слов/фраз». Ищет все вхождения за один проход."""

    def __init__(self, groups: dict[str, list[str]], use_stem: bool = True):
        self.labels: list[tuple[str, str]] = []      # (группа, исходное слово)
        alts = []
        seen = set()
        for group, words in groups.items():
            for w in words:
                rx = phrase_regex(w, use_stem)
                if not rx or rx in seen:
                    continue
                seen.add(rx)
                alts.append((rx, len(self.labels)))
                self.labels.append((group, w))
        # длинные шаблоны первыми — «алматинск обл» раньше «алмат»
        alts.sort(key=lambda x: -len(x[0]))
        self._re = re.compile("|".join(f"(?P<k{i}>{rx})" for rx, i in alts)) if alts else None

    def find(self, norm: str) -> list[tuple[int, str, str]]:
        """[(позиция, группа, слово)] в нормализованном тексте."""
        return [(a, g, w) for a, _, g, w in self.find_spans(norm)]

    def find_spans(self, norm: str) -> list[tuple[int, int, str, str]]:
        """[(начало, конец, группа, слово из словаря)] в нормализованном тексте."""
        if self._re is None:
            return []
        out = []
        for m in self._re.finditer(norm):
            group, word = self.labels[int(m.lastgroup[1:])]
            out.append((m.start(), m.end(), group, word))
        return out

    def search(self, norm: str) -> bool:
        return self._re is not None and self._re.search(norm) is not None
