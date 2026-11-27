"""
Сбор новостей о нарушениях: GDELT, Google News RSS, обычные RSS-ленты,
плюс докачка полного текста статей.
"""
import html
import re
import sys
import time
from calendar import timegm
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests

import config
import keywords
from db import insert_article

session = requests.Session()
session.headers["User-Agent"] = config.HTTP_USER_AGENT

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def to_dt(value) -> datetime | None:
    """Дата из чего угодно (struct_time, ISO, RFC 822, «20261112T101500Z») -> datetime UTC.
    Нераспознанное -> None (в базу лучше пустая дата, чем мусор)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, time.struct_time):
        return datetime.fromtimestamp(timegm(value), timezone.utc)
    s = str(value).strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# GDELT DOC 2.0
# ---------------------------------------------------------------------------
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


_gdelt_last = 0.0
_gdelt_pause = config.GDELT_PAUSE_SEC


def gdelt_request(q: str, start: datetime, end: datetime):
    """Один запрос к GDELT с соблюдением паузы и повторами при 429.
    Возвращает список статей или None, если так и не получилось."""
    global _gdelt_last, _gdelt_pause
    params = {
        "query": q, "mode": "ArtList", "format": "json", "maxrecords": 250, "sort": "DateDesc",
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
    }
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
            arts = gdelt_request(q, a, b)
            if arts is None:
                continue
            new = 0
            for x in arts:
                if not x.get("url"):
                    continue
                new += insert_article(
                    conn, url=x["url"], title=x.get("title"), source=x.get("domain"),
                    published=to_dt(x.get("seendate")), lang=x.get("language"),
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


def parse_date(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return to_dt(t)
    return to_dt(entry.get("published") or entry.get("updated"))


def gnews_title(entry) -> tuple[str, dict]:
    """(заголовок без приписки « - Издание», сведения об издании)."""
    src = entry.get("source") or {}
    title = entry.get("title", "")
    name = src.get("title")
    if name and title.endswith(" - " + name):
        title = title[: -len(name) - 3]
    return title, src


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
            title, src = gnews_title(e)
            new += insert_article(
                conn, url=e.get("link"), title=title, source=src.get("title"),
                published=parse_date(e), lang=hl, country=gl, collector="gnews",
                query=q, summary=strip_html(e.get("summary")),
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
            summary = strip_html(e.get("summary") or e.get("description"))
            if config.RSS_ONLY_WITH_KEYWORDS and not keywords.has_any(f"{title} {summary}"):
                continue
            kept += 1
            new += insert_article(
                conn, url=e.get("link"), title=title, source=name,
                published=parse_date(e), collector="rss", query=url, summary=summary,
            )
        conn.commit()
        total_new += new
        log(f"[rss] {name}: {len(feed.entries)} в ленте, {kept} с ключевыми словами, {new} новых")
    return total_new


# ---------------------------------------------------------------------------
# Полный текст
# ---------------------------------------------------------------------------
def fetch_page(url: str, with_meta: bool = False):
    """Скачивает страницу и достаёт из неё текст статьи.
    Возвращает (text, status) или, с with_meta, (text, status, meta), где
    meta — {"title", "date", "sitename"} (что удалось найти)."""
    import trafilatura
    text, meta = None, {}
    try:
        r = session.get(url, timeout=25)
        if r.status_code != 200:
            status = f"error: HTTP {r.status_code}"
        else:
            if with_meta:
                doc = trafilatura.bare_extraction(r.text, url=url, include_comments=False,
                                                  include_tables=False, with_metadata=True)
                if doc is not None and not isinstance(doc, dict):
                    doc = doc.as_dict()
                doc = doc or {}
                meta = {"title": doc.get("title"), "date": to_dt(doc.get("date")),
                        "sitename": doc.get("sitename")}
                text = doc.get("text")
            else:
                text = trafilatura.extract(r.text, include_comments=False, include_tables=False)
            text = text or None
            status = "ok" if text else "empty"
    except Exception as e:
        text, status = None, f"error: {type(e).__name__}"
    return (text, status, meta) if with_meta else (text, status)


def fetch_text(conn, workers: int = 8, limit: int | None = None):
    """Скачивает полный текст для статей, где его ещё нет.
    Ссылки Google News ведут на редирект через JS и не скачиваются —
    по ним матчинг идёт по заголовку и описанию."""
    rows = conn.execute(
        "SELECT id, url FROM articles WHERE text_status IS NULL "
        "AND url NOT LIKE 'https://news.google.com/%%' ORDER BY id"
        + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    conn.commit()
    log(f"[text] к загрузке: {len(rows)}")
    done = ok = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_page, r["url"]): r["id"] for r in rows}
        for f in as_completed(futs):
            text, status = f.result()
            conn.execute(
                "UPDATE articles SET text=%s, text_status=%s, matched_at=NULL WHERE id=%s",
                (text, status, futs[f]),
            )
            # Коммитим сразу: не держим транзакцию открытой, пока качаются
            # следующие страницы (это минуты).
            conn.commit()
            done += 1
            ok += status == "ok"
            if done % 100 == 0:
                log(f"[text] {done}/{len(rows)}, с текстом {ok}")
    conn.execute(
        "UPDATE articles SET text_status='skipped' WHERE text_status IS NULL "
        "AND url LIKE 'https://news.google.com/%%'"
    )
    conn.commit()
    log(f"[text] готово: {done}, с текстом {ok}")
