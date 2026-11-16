"""
Загрузка списка имён и прогон собранных новостей по нему.
"""
import bisect
import re
import sys
from collections import defaultdict
from dataclasses import dataclass

import ahocorasick
import pandas as pd

import config
import keywords
from db import now
from entities import parse
from normalize import normalize_with_map


class EmptyListError(Exception):
    """В базе нет ни одного имени, по которому можно искать."""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Загрузка списка
# ---------------------------------------------------------------------------
def load_names(conn, path: str, column: str | None = None) -> dict:
    if path.lower().endswith((".csv", ".txt")):
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    else:
        df = pd.read_excel(path, dtype=str, keep_default_na=False)
    col = column or df.columns[0]
    raw = df[col].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    raw = raw[raw != ""].drop_duplicates()

    groups: dict[tuple, dict] = {}
    for name in raw:
        e = parse(name)
        key = (e.kind, e.core)
        g = groups.get(key)
        if g is None:
            groups[key] = {"e": e, "variants": []}
        elif name != g["e"].name and len(g["variants"]) < 20:
            g["variants"].append(name)

    existing = {(r["kind"], r["core"]) for r in conn.execute("SELECT kind, core FROM entities")}
    new = 0
    for key, g in groups.items():
        if key in existing:
            continue
        e = g["e"]
        conn.execute(
            "INSERT INTO entities (name, variants, kind, core, pattern, tokens, skip_reason) "
            "VALUES (?,?,?,?,?,?,?)",
            (e.name, " | ".join(g["variants"]) or None, e.kind, e.core, e.pattern,
             " ".join(e.tokens), e.skip_reason),
        )
        new += 1
    conn.commit()

    stats = {"строк в файле (уникальных)": len(raw), "сущностей после склейки": len(groups),
             "новых добавлено": new}
    for kind, n in conn.execute(
            "SELECT kind, COUNT(*) FROM entities WHERE skip_reason IS NULL GROUP BY kind"):
        stats[f"ищем: {kind}"] = n
    for reason, n in conn.execute(
            "SELECT skip_reason, COUNT(*) FROM entities WHERE skip_reason IS NOT NULL "
            "GROUP BY skip_reason"):
        stats[f"пропущено: {reason}"] = n
    return stats


# ---------------------------------------------------------------------------
# Матчинг
# ---------------------------------------------------------------------------
@dataclass
class Ent:
    id: int
    name: str
    kind: str
    tokens: list


@dataclass
class Hit:
    ent: Ent
    span: tuple          # (start, end) в нормализованном тексте
    score: int
    cats: list
    kws: list
    in_title: bool


_TOK_RE = re.compile(r"[^ ]+")


def _close(tok: str, st: str) -> bool:
    if len(st) < 4:                      # короткие слова (Ли, Те, Kaya) — только точно
        return tok == st
    return tok.startswith(st) and len(tok) - len(st) <= 4


class Matcher:
    def __init__(self, conn):
        self.by_key: dict[str, list[Ent]] = defaultdict(list)
        rows = conn.execute(
            "SELECT id, name, kind, pattern, tokens FROM entities "
            "WHERE skip_reason IS NULL AND pattern <> ''"
        ).fetchall()
        for r in rows:
            self.by_key[r["pattern"]].append(
                Ent(r["id"], r["name"], r["kind"], (r["tokens"] or "").split()))
        log(f"[match] шаблонов: {len(self.by_key)} (сущностей: {len(rows)})")
        if not self.by_key:
            raise EmptyListError(
                "список получателей пуст — искать нечего. Сначала загрузите список "
                "(запуск «Загрузить список имён» или файл в «Полном цикле»).")
        self.A = ahocorasick.Automaton()
        for key in self.by_key:
            self.A.add_word(key, key)
        self.A.make_automaton()

    def _person_span(self, norm, s, ent):
        """s — позиция пробела перед основой-триггером. Проверяем, что рядом
        (±3 слова) есть вторая часть ФИО. Возвращает (span, нашлось_отчество) или None."""
        tok_end = norm.find(" ", s + 1)
        if tok_end < 0 or not _close(norm[s + 1:tok_end], ent.tokens[0]):
            return None
        a, b = max(0, s - 80), min(len(norm), tok_end + 80)
        toks = [(m.start(), m.end(), m.group()) for m in _TOK_RE.finditer(norm, a, b)]
        i = next((k for k, t in enumerate(toks) if t[0] == s + 1), None)
        if i is None:
            return None
        # «Илияса Максутовича Амирова»: имя и фамилия максимум через одно слово
        if ent.tokens[0] == ent.tokens[1]:
            ctx = toks[max(0, i - 1):i] + toks[i + 1:i + 2]
        else:
            ctx = toks[max(0, i - 2):i] + toks[i + 1:i + 3]
        m2 = next((t for t in ctx if _close(t[2], ent.tokens[1])), None)
        if m2 is None:
            return None
        patr = len(ent.tokens) > 2 and any(_close(t[2], ent.tokens[2]) for t in ctx)
        start = min(s + 1, m2[0])
        end = max(tok_end, m2[1])
        return (start, end), patr

    def match(self, norm: str, title_end: int, keep_weak: bool = False) -> list[Hit]:
        kw = keywords.find(norm)
        kw_pos = [p for p, _, _ in kw]
        any_kw = bool(kw)
        best: dict[int, Hit] = {}
        W = config.KEYWORD_WINDOW

        for end, key in self.A.iter(norm):
            s = end - len(key) + 1
            for ent in self.by_key[key]:
                if ent.kind == "org":
                    span = (s + 1, end)
                    n = len(ent.tokens)
                    score = 35 + min(15 * (n - 1), 30)
                    if n == 1:
                        score -= 20 if len(ent.tokens[0]) < 5 else 10
                else:
                    r = self._person_span(norm, s, ent)
                    if r is None:
                        continue
                    span, patr = r
                    score = 45 + (15 if patr else 0)
                    if min(len(t) for t in ent.tokens[:2]) < 4:
                        score -= 10              # короткие имена (Ли, Kaya) — частые тёзки

                lo = bisect.bisect_left(kw_pos, span[0] - W)
                hi = bisect.bisect_right(kw_pos, span[1] + W)
                near = kw[lo:hi]
                cats = sorted({c for _, _, c in near})
                kws = sorted({w for _, w, _ in near})
                in_title = span[0] < title_end
                kw_in_title = bool(kw_pos) and kw_pos[0] < title_end

                if cats:
                    score += 20 + min(5 * (len(cats) - 1), 15)
                elif in_title and any_kw:
                    score += 10
                    cats = sorted({c for _, _, c in kw})
                    kws = sorted({w for _, w, _ in kw})
                elif not keep_weak:
                    continue
                if in_title and kw_in_title:
                    score += 10
                score = min(score, 100)

                prev = best.get(ent.id)
                if prev is None or score > prev.score:
                    best[ent.id] = Hit(ent, span, score, cats, kws, in_title)

        # «Freedom Finance» внутри «Freedom Finance Europe» — оставляем только длинное
        hits = list(best.values())
        orgs = [h for h in hits if h.ent.kind == "org"]
        def inside(h):
            return any(o is not h and o.span[0] <= h.span[0] and h.span[1] <= o.span[1]
                       and (o.span[1] - o.span[0]) > (h.span[1] - h.span[0]) for o in orgs)
        return [h for h in hits if h.ent.kind != "org" or not inside(h)]


def _snippet(orig: str, idx: list, span: tuple):
    o1 = idx[span[0]]
    o2 = idx[max(span[1] - 1, span[0])] + 1
    n = config.SNIPPET_CHARS
    a, b = max(0, o1 - n), min(len(orig), o2 + n)
    snip = ("…" if a > 0 else "") + orig[a:b] + ("…" if b < len(orig) else "")
    return orig[o1:o2], re.sub(r"\s+", " ", snip).strip()


UPSERT = """
INSERT INTO matches (entity_id, article_id, entity_name, entity_kind, source, title, url,
                     published, lang, matched_text, categories, keywords, in_title, score,
                     score_raw, snippet, created_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(entity_id, article_id) DO UPDATE SET
    matched_text=excluded.matched_text, categories=excluded.categories,
    keywords=excluded.keywords, in_title=excluded.in_title, score=excluded.score,
    score_raw=excluded.score_raw, snippet=excluded.snippet, title=excluded.title
"""


def _stale_match_ids(conn, article_ids: list, found: set) -> list:
    """Старые совпадения по этим статьям, которые при новой проверке не нашлись
    (изменился список, ключевые слова или настройки). Уже проверенные вручную
    и с комментарием не трогаем."""
    marks = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"SELECT id, entity_id, article_id FROM matches WHERE article_id IN ({marks}) "
        "AND review_status = 'new' AND COALESCE(review_note, '') = ''", article_ids)
    return [(r["id"],) for r in rows if (r["entity_id"], r["article_id"]) not in found]


def run_match(conn, all_articles: bool = False, keep_weak: bool = False, batch: int = 500):
    m = Matcher(conn)
    where = "" if all_articles else "WHERE matched_at IS NULL"
    total = conn.execute(f"SELECT COUNT(*) FROM articles {where}").fetchone()[0]
    log(f"[match] статей к обработке: {total}")
    done = found = removed = 0
    last_id = 0
    while True:
        rows = conn.execute(
            f"SELECT * FROM articles {where} {'AND' if where else 'WHERE'} id > ? "
            f"ORDER BY id LIMIT ?", (last_id, batch)).fetchall()
        if not rows:
            break
        ts = now()
        # Сначала считаем весь пакет в памяти, потом пишем одной короткой
        # транзакцией — чтобы не держать базу заблокированной, пока идёт матчинг.
        upserts, processed = [], []
        for a in rows:
            last_id = a["id"]
            title = a["title"] or ""
            orig = "\n".join(x for x in (title, a["summary"], a["text"]) if x)
            norm, idx = normalize_with_map(orig)
            title_end = bisect.bisect_left(idx, len(title)) if title else 0
            for h in m.match(norm, title_end, keep_weak):
                matched, snip = _snippet(orig, idx, h.span)
                upserts.append((
                    h.ent.id, a["id"], h.ent.name, h.ent.kind, a["source"], title, a["url"],
                    a["published"], a["lang"], matched, "; ".join(h.cats), "; ".join(h.kws),
                    int(h.in_title), h.score, h.score, snip, ts))
            processed.append((ts, a["id"]))
        stale = _stale_match_ids(conn, [a["id"] for a in rows], {(u[0], u[1]) for u in upserts})
        conn.executemany("DELETE FROM matches WHERE id=?", stale)
        conn.executemany(UPSERT, upserts)
        conn.executemany("UPDATE articles SET matched_at=? WHERE id=?", processed)
        conn.commit()
        found += len(upserts)
        removed += len(stale)
        done += len(processed)
        log(f"[match] {done}/{total}, совпадений записано: {found}")
    if removed:
        log(f"[match] убрано устаревших непроверенных совпадений: {removed}")
    mark_frequent(conn)
    return found


def mark_frequent(conn):
    """Имена, которые всплывают в очень многих статьях, — скорее всего обычные слова."""
    n, pen = config.FREQUENT_ENTITY_ARTICLES, config.FREQUENT_PENALTY
    conn.execute("""
        UPDATE matches SET frequent = (entity_id IN (
            SELECT entity_id FROM matches GROUP BY entity_id HAVING COUNT(*) >= ?))""", (n,))
    conn.execute("UPDATE matches SET score = MAX(score_raw - CASE WHEN frequent THEN ? ELSE 0 END, 0)",
                 (pen,))
    conn.commit()
    k = conn.execute("SELECT COUNT(DISTINCT entity_id) FROM matches WHERE frequent").fetchone()[0]
    if k:
        log(f"[match] «частых» имён (>= {n} статей): {k}, балл снижен на {pen}")
