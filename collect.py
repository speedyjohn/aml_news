"""
Сбор новостей о нарушениях: GDELT, Google News RSS, обычные RSS-ленты,
плюс докачка полного текста статей.
"""
import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import feedparser
import requests

import config
import keywords
from db import insert_article

session = requests.Session()
session.headers["User-Agent"] = config.HTTP_USER_AGENT

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# GDELT DOC 2.0
# ---------------------------------------------------------------------------
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def _gdelt_date(s: str | None) -> str | None:
    # "20261112T101500Z" -> "2026-11-12 10:15:00"
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s


_gdelt_last = 0.0
_gdelt_pause = None


def _gdelt_request(q, params):
    """Один запрос к GDELT с соблюдением паузы и повторами при 429.
    Возвращает список статей или None, если так и не получилось."""
    global _gdelt_last, _gdelt_pause
    if _gdelt_pause is None:
        _gdelt_pause = config.GDELT_PAUSE_SEC
    for attempt in range(config.GDELT_MAX_RETRIES + 1):
        wait = _gdelt_pause - (time.monotonic() - _gdelt_last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = session.get(GDELT_URL, params=params, timeout=60)
            body = r.text.strip()
        except Exception as e:
            _gdelt_last = time.monotonic()
            log(f"[gdelt] {q!r}: ошибка сети {type(e).__name__}, повтор через 30 с")
            time.sleep(30)
            continue
        _gdelt_last = time.monotonic()

        if r.status_code == 429 or "limit requests" in body[:300].lower():
            backoff = min(15 * 2 ** attempt, 300)
            # раз сервер жалуется — дальше ходим реже
            _gdelt_pause = min(_gdelt_pause + 2, 20)
            log(f"[gdelt] лимит запросов (429), жду {backoff} с и повторяю "
                f"(попытка {attempt + 1}/{config.GDELT_MAX_RETRIES}); пауза теперь {_gdelt_pause} с")
            time.sleep(backoff)
            continue
        if r.status_code != 200 or not body.startswith("{"):
            # пустой ответ или текстовая ошибка запроса — повторять бессмысленно
            if body:
                log(f"[gdelt] {q!r}: {r.status_code} {body[:150]!r}")
            return [] if r.status_code == 200 else None
        try:
            return r.json().get("articles", [])
        except Exception:
            return []
    log(f"[gdelt] {q!r}: не удалось после {config.GDELT_MAX_RETRIES} повторов, пропускаю")
    return None


def gdelt_queries():
    for kw in config.GDELT_KEYWORDS_EN:
        for sc in config.GDELT_SCOPES_EN:
            yield f"{kw} {sc}"
    for kw in config.GDELT_KEYWORDS_RU:
        for sc in config.GDELT_SCOPES_RU:
            yield f"{kw} {sc}"


def collect_gdelt(conn, days: int = 90, slice_days: int | None = None):
    """GDELT отдаёт максимум 250 статей на запрос. Чтобы собрать больше,
    период режется на окна по slice_days дней (например, 7)."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    windows = []
    if slice_days:
        t = start
        while t < end:
            windows.append((t, min(t + timedelta(days=slice_days), end)))
            t += timedelta(days=slice_days)
    else:
        windows.append((start, end))

    queries = list(gdelt_queries())
    total_new = 0
    total = len(queries) * len(windows)
    log(f"[gdelt] запросов: {len(queries)} x окон: {len(windows)} = {total}, "
        f"примерно {total * config.GDELT_PAUSE_SEC // 60} мин")
    n = 0
    for q in queries:
        for a, b in windows:
            n += 1
            params = {
                "query": q, "mode": "ArtList", "format": "json", "maxrecords": 250,
                "sort": "DateDesc",
                "startdatetime": a.strftime("%Y%m%d%H%M%S"),
                "enddatetime": b.strftime("%Y%m%d%H%M%S"),
            }
            arts = _gdelt_request(q, params)
            if arts is None:
                continue
            new = 0
            for x in arts:
                if not x.get("url"):
                    continue
                new += insert_article(
                    conn, url=x["url"], title=x.get("title"), source=x.get("domain"),
                    published=_gdelt_date(x.get("seendate")), lang=x.get("language"),
                    country=x.get("sourcecountry"), collector="gdelt", query=q,
                )
            conn.commit()
            total_new += new
            log(f"[gdelt] {n}/{total} {q!r} {a:%Y-%m-%d}: {len(arts)} найдено, {new} новых")
    return total_new


# ---------------------------------------------------------------------------
# Google News RSS
# ---------------------------------------------------------------------------
GNEWS_URL = "https://news.google.com/rss/search"


def _parse_date(entry) -> str | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return time.strftime("%Y-%m-%d %H:%M:%S", t)
    return entry.get("published")


def collect_gnews(conn, days: int = 30):
    jobs = [(q, loc) for loc in config.GNEWS_LOCALES_RU for q in config.GNEWS_QUERIES_RU]
    jobs += [(q, loc) for loc in config.GNEWS_LOCALES_EN for q in config.GNEWS_QUERIES_EN]
    total_new = 0
    log(f"[gnews] запросов: {len(jobs)}")
    for q, (hl, gl, ceid) in jobs:
        params = {"q": f"{q} when:{days}d", "hl": hl, "gl": gl, "ceid": ceid}
        try:
            r = session.get(GNEWS_URL, params=params, timeout=30)
            feed = feedparser.parse(r.content)
        except Exception as e:
            log(f"[gnews] {q!r} {ceid}: ошибка {e}")
            continue
        new = 0
        for e in feed.entries:
            src = (e.get("source") or {}).get("title")
            title = e.get("title", "")
            if src and title.endswith(" - " + src):
                title = title[: -len(src) - 3]
            new += insert_article(
                conn, url=e.get("link"), title=title, source=src,
                published=_parse_date(e), lang=hl, country=gl, collector="gnews",
                query=q, summary=_strip_html(e.get("summary")),
            )
        conn.commit()
        total_new += new
        log(f"[gnews] {q!r} {ceid}: {len(feed.entries)} найдено, {new} новых")
        time.sleep(config.GNEWS_PAUSE_SEC)
    return total_new


# ---------------------------------------------------------------------------
# Обычные RSS
# ---------------------------------------------------------------------------
def collect_rss(conn):
    total_new = 0
    for name, url in config.RSS_FEEDS:
        try:
            r = session.get(url, timeout=30)
            feed = feedparser.parse(r.content)
            if r.status_code != 200 or (feed.bozo and not feed.entries):
                log(f"[rss] {name}: НЕ ОТКРЫЛАСЬ ({r.status_code}) {url} — проверьте адрес")
                continue
        except Exception as e:
            log(f"[rss] {name}: НЕ ОТКРЫЛАСЬ ({e}) {url}")
            continue
        new = kept = 0
        for e in feed.entries:
            title = e.get("title", "")
            summary = _strip_html(e.get("summary") or e.get("description"))
            if config.RSS_ONLY_WITH_KEYWORDS and not keywords.has_any(f"{title} {summary}"):
                continue
            kept += 1
            new += insert_article(
                conn, url=e.get("link"), title=title, source=name,
                published=_parse_date(e), collector="rss", query=url, summary=summary,
            )
        conn.commit()
        total_new += new
        log(f"[rss] {name}: {len(feed.entries)} в ленте, {kept} с ключевыми словами, {new} новых")
    return total_new


# ---------------------------------------------------------------------------
# Полный текст
# ---------------------------------------------------------------------------
def _fetch_one(url: str):
    import trafilatura
    try:
        r = session.get(url, timeout=25)
        if r.status_code != 200:
            return None, f"error: HTTP {r.status_code}"
        text = trafilatura.extract(r.text, include_comments=False, include_tables=False)
        return (text, "ok") if text else (None, "empty")
    except Exception as e:
        return None, f"error: {type(e).__name__}"


def fetch_text(conn, workers: int = 8, limit: int | None = None):
    """Скачивает полный текст для статей, где его ещё нет.
    Ссылки Google News ведут на редирект через JS и не скачиваются —
    по ним матчинг идёт по заголовку и описанию."""
    rows = conn.execute(
        "SELECT id, url FROM articles WHERE text_status IS NULL "
        "AND url NOT LIKE 'https://news.google.com/%' ORDER BY id"
        + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    log(f"[text] к загрузке: {len(rows)}")
    done = ok = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(_fetch_one, r["url"]): r["id"] for r in rows}
        for f in as_completed(futs):
            text, status = f.result()
            conn.execute(
                "UPDATE articles SET text=?, text_status=?, matched_at=NULL WHERE id=?",
                (text, status, futs[f]),
            )
            # Коммитим сразу: иначе база остаётся заблокированной на запись,
            # пока качаются следующие 100 страниц (это минуты), и панель падает.
            conn.commit()
            done += 1
            ok += status == "ok"
            if done % 100 == 0:
                log(f"[text] {done}/{len(rows)}, с текстом {ok}")
    conn.execute(
        "UPDATE articles SET text_status='skipped' WHERE text_status IS NULL "
        "AND url LIKE 'https://news.google.com/%'"
    )
    conn.commit()
    log(f"[text] готово: {done}, с текстом {ok}")
