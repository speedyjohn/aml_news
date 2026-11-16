"""
Разбор имён из списка: чистка, правовая форма, физлицо/юрлицо, шаблон для поиска.
"""
import re
from dataclasses import dataclass, field

import config
from normalize import normalize, stem

_JUNK_RE = re.compile(r"&?quot;?|&amp;|&apos;|&#\d+;", re.I)
_IDNUM_RE = re.compile(r"(инн|кпп|бин|иин|inn|kpp|bin|iin)?\s*\d{6,}", re.I)


def _norm_list(items):
    return {t for x in items for t in normalize(x).split()}


LEGAL_TOKENS = _norm_list(config.LEGAL_TOKENS)
LEGAL_PHRASES = sorted({normalize(p) for p in config.LEGAL_PHRASES}, key=len, reverse=True)
GENERIC = _norm_list(config.GENERIC_WORDS)


@dataclass
class Entity:
    name: str                 # как в исходном списке (первый встреченный вариант)
    kind: str                 # org | person
    core: str                 # нормализованное «ядро» без правовой формы
    pattern: str              # ключ для поиска (org: " ядро ", person: " основа")
    tokens: list = field(default_factory=list)   # для физлиц: основы слов
    skip_reason: str | None = None


def clean_name(raw: str) -> str:
    s = _JUNK_RE.sub(" ", str(raw))
    s = _IDNUM_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip(" .,;:-\"'«»")


def _is_patronymic(tok: str) -> bool:
    return len(tok) > 5 and tok.endswith(config.PATRONYMIC_SUFFIXES)


def parse(raw: str) -> Entity:
    name = clean_name(raw)
    norm = normalize(name)
    had_legal = False
    for ph in LEGAL_PHRASES:
        if ph in norm:
            norm = norm.replace(ph, " ")
            had_legal = True
    toks = norm.split()
    core_toks = [t for t in toks if t not in LEGAL_TOKENS]
    had_legal = had_legal or len(core_toks) != len(toks)
    core = " ".join(core_toks)

    if not core_toks:
        return Entity(name, "org", core, "", skip_reason="пусто после чистки")

    patr = [] if had_legal else [t for t in core_toks if _is_patronymic(t)]
    has_digits = any(ch.isdigit() for ch in core)
    looks_person = bool(patr) or (
        not had_legal and 2 <= len(core_toks) <= 3 and not has_digits
        and not any(t in GENERIC for t in core_toks)
        and all(len(t) >= 2 for t in core_toks)
    )

    if looks_person:
        main = [t for t in core_toks if t not in patr]
        if len(main) < 2:
            main = core_toks[:]           # «Назгуль Абдешкызы» — отчество как фамилия
        main = main[:2]
        stems = [stem(t) if len(t) >= 5 else t for t in main]
        if len(stems) < 2:
            return Entity(name, "person", core, "", skip_reason="одно слово в ФИО")
        # триггер — более длинная из двух основ, вторая проверяется рядом
        order = sorted(range(2), key=lambda i: -len(stems[i]))
        s1, s2 = stems[order[0]], stems[order[1]]
        if len(s1) < 3 or len(s2) < 2:
            return Entity(name, "person", core, "", skip_reason="слишком короткое ФИО")
        extra = [stem(t) for t in patr if t not in main][:1]
        core = " ".join(sorted(core_toks))   # «Иванов Иван» == «Иван Иванов»
        return Entity(name, "person", core, " " + s1, tokens=[s1, s2] + extra)

    # юрлицо
    meaningful = [t for t in core_toks if t not in GENERIC and not t.isdigit()]
    if not meaningful and len(core_toks) <= 2:
        return Entity(name, "org", core, "", skip_reason="только общие слова")
    if len(core.replace(" ", "")) < 3:
        return Entity(name, "org", core, "", skip_reason="слишком короткое название")
    return Entity(name, "org", core, f" {core} ", tokens=core_toks)
