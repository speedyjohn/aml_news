"""
Негативные новости о Казахстане: разметка (категории, регионы, балл),
сбор из Google News, GDELT, RSS-лент и Telegram-каналов, докачка текста.

Все сборщики работают одинаково: кандидат -> analyze() -> если подходит,
save_item() кладёт его в neg_news (или засчитывает как перепечатку уже
найденного сюжета).
"""
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

import config
from collect import (GNEWS_URL, fetch_page, gdelt_request, gnews_title, log, parse_date, session,
                     strip_html, to_dt)
from db import now
from keywords import KeywordIndex
from normalize import normalize, normalize_with_map

NEG_INDEX = KeywordIndex(config.NEG_KEYWORDS)
REGION_INDEX = KeywordIndex(config.NEG_REGIONS, use_stem=False)
KZ_INDEX = KeywordIndex({"kz": config.NEG_KZ_TERMS}, use_stem=False)
FOREIGN_INDEX = KeywordIndex({"foreign": config.NEG_FOREIGN_TERMS}, use_stem=False)
EXCLUDE_INDEX = KeywordIndex({"exclude": config.NEG_EXCLUDE_TITLE})

KZ_DOMAINS = {d.lower() for d in config.NEG_SOURCE_NAMES}


# ---------------------------------------------------------------------------
# Разметка
# ---------------------------------------------------------------------------
@dataclass
class Verdict:
    keep: bool
    score: int = 0
    categories: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    regions: list = field(default_factory=list)
    kz_signal: str | None = None
    reason: str = ""

    def as_row(self) -> dict:
        return {"score": self.score, "categories": "; ".join(self.categories) or None,
                "keywords": "; ".join(self.keywords) or None,
                "regions": "; ".join(self.regions) or None, "kz_signal": self.kz_signal}


LEAD_CHARS = 600         # «начало текста» для проверки новостей о других странах
FULL_TEXT_CHARS = 1500   # с какой длины считаем, что перед нами полный текст статьи


def _surface(orig: str, idx: list, a: int, b: int) -> str:
    """Исходный текст совпадения: norm[a:b] (a — пробел перед словом) -> слова
    из оригинала, последнее — целиком (с окончанием)."""
    if not orig or b <= a + 1:
        return ""
    s = idx[a + 1]
    e = idx[b - 1] + 1
    while e < len(orig) and (orig[e].isalnum() or orig[e] == "-"):
        e += 1
    return re.sub(r"\s+", " ", orig[s:e]).strip().lower()


def analyze(title: str | None, summary: str | None = None, text: str | None = None, *,
            kz_outlet: bool = False, source_region: str | None = None, copies: int = 1,
            tone: float | None = None) -> Verdict:
    """Решает, негативная ли это новость о Казахстане, и считает балл 0–100."""
    title = (title or "").strip()
    body = "\n".join(x for x in (summary, text) if x and x.strip() != title)
    nt, map_t = normalize_with_map(title)
    nb, map_b = normalize_with_map(body)

    if title and EXCLUDE_INDEX.search(nt):
        return Verdict(False, reason="исключено по заголовку (спорт, развлечения…)")

    spans_t = NEG_INDEX.find_spans(nt)
    spans_b = NEG_INDEX.find_spans(nb)
    hits_t = [(a, g, w) for a, _, g, w in spans_t]
    hits = hits_t + [(a, g, w) for a, _, g, w in spans_b]
    if not hits:
        return Verdict(False, reason="нет негативных слов")
    # как найденные слова написаны в самом тексте — это и показываем аналитику
    surface = list(dict.fromkeys(
        [_surface(title, map_t, a, b) for a, b, _, _ in spans_t] +
        [_surface(body, map_b, a, b) for a, b, _, _ in spans_b]))

    # регионы: по числу упоминаний; регион самого издания — в конец, если не найден
    reg_count = Counter(g for _, g, _ in REGION_INDEX.find(nt) + REGION_INDEX.find(nb))
    # главные — первые три по частоте; остальные — если упомянуты хотя бы дважды
    # (иначе перечисление «во всех регионах…» или меню сайта дают все 20 регионов)
    regions = [r for i, (r, n) in enumerate(reg_count.most_common()) if i < 3 or n >= 2][:6]
    kz_terms = KZ_INDEX.search(nt) or KZ_INDEX.search(nb)
    mentioned = bool(regions) or kz_terms
    if source_region and source_region not in regions:
        regions.append(source_region)

    if not mentioned and not kz_outlet:
        return Verdict(False, reason="не про Казахстан")
    if not mentioned and len(body) >= FULL_TEXT_CHARS:
        # казахстанские издания пишут и о мире (Nvidia, выборы в США…): если перед нами
        # полный текст и в нём ни разу нет ни Казахстана, ни региона/города — не наше
        return Verdict(False, reason="в тексте нет упоминаний Казахстана")
    if FOREIGN_INDEX.search(nt):
        # Заголовок о другой стране: Казахстан должен быть в заголовке или в начале
        # текста, а не мельком в середине длинной статьи («…и в Казахстане тоже»)
        lead = normalize(body[:LEAD_CHARS])
        if not any(ix.search(s) for ix in (KZ_INDEX, REGION_INDEX) for s in (nt, lead)):
            return Verdict(False, reason="новость о другой стране")

    signal = []
    if kz_outlet:
        signal.append("казахстанское издание")
    if reg_count:
        signal.append("упоминается: " + ", ".join(r for r, _ in reg_count.most_common(3)))
    elif kz_terms:
        signal.append("упоминается Казахстан")

    cat_count = Counter(g for _, g, _ in hits)
    categories = [c for c, _ in cat_count.most_common()]
    words = list(dict.fromkeys(w for _, _, w in hits))

    weights = sorted((config.NEG_CATEGORY_WEIGHT.get(c, 1.0) for c in categories), reverse=True)
    score = 35 * weights[0] + min(sum(12 * w for w in weights[1:]), 36)
    if hits_t:
        score += 15
    if len(words) >= 6:
        score += 15
    elif len(words) >= 3:
        score += 10
    if mentioned:
        score += 10
    if copies >= 4:
        score += 10
    elif copies >= 2:
        score += 5
    if tone is not None and tone <= -5:
        score += 5
    if not mentioned:
        # география не подтверждена текстом (только «казахстанское издание») —
        # в «сильные» такие не попадают
        score = min(score, config.NEG_STRONG_SCORE - 5)
    score = int(round(min(score, 100)))

    v = Verdict(score >= config.NEG_MIN_SCORE, score, categories,
                [w for w in surface if w][:30], regions, "; ".join(signal) or None)
    if not v.keep:
        v.reason = f"балл {score} ниже порога {config.NEG_MIN_SCORE}"
    return v


# ---------------------------------------------------------------------------
# Сохранение
# ---------------------------------------------------------------------------
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|yclid$|ref$|_openstat$|from$)", re.I)


def canonical_url(url: str | None) -> str | None:
    """Без меток рекламных кампаний и якоря — чтобы одна статья не попала дважды."""
    if not url:
        return None
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if p.scheme not in ("http", "https"):
        return url.strip()
    q = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                   if not _TRACKING.match(k)])
    return urlunsplit((p.scheme, p.netloc.lower(), p.path, q, ""))


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host or None


# Городские издания на доменах не в зоне .kz (almaty.tv, astana.tv…)
_KZ_CITY_IN_DOMAIN = re.compile(
    r"(^|[.-])(almaty|astana|shymkent|atyrau|aktobe|aktau|karaganda|qaragandy|pavlodar|"
    r"kostanay|qostanay|oskemen|semey|taraz|uralsk|oral|kokshetau|turkestan|kyzylorda|"
    r"petropavl|zhezkazgan|ekibastuz|temirtau|qazaq|kazakhstan)")


def is_kz_domain(domain: str | None) -> bool:
    if not domain:
        return False
    return (domain.endswith(".kz") or domain in KZ_DOMAINS
            or any(domain.endswith("." + d) for d in KZ_DOMAINS)
            or bool(_KZ_CITY_IN_DOMAIN.search(domain.rsplit(".", 1)[0])))


def source_name(domain: str | None, fallback: str | None = None) -> str | None:
    if domain:
        if domain in config.NEG_SOURCE_NAMES:
            return config.NEG_SOURCE_NAMES[domain]
        parent = domain.split(".", 1)[-1]
        if parent in config.NEG_SOURCE_NAMES:
            return config.NEG_SOURCE_NAMES[parent]
    return fallback or domain


def title_key(title: str | None) -> str | None:
    """Нормализованный заголовок для склейки перепечаток одного сюжета."""
    toks = normalize(title or "").split()
    return " ".join(toks[:14]) if len(toks) >= 5 else None


_LEAD_JUNK = re.compile(r"^[^\w«\"(]+")


def make_title(text: str, limit: int = 200) -> str:
    """Заголовок для поста без заголовка (Telegram): первая строка / предложение."""
    # первая непустая строка; эмодзи и значки в начале поста не нужны
    first = next((ln for ln in (_LEAD_JUNK.sub("", x).strip() for x in text.splitlines()) if ln), "")
    if len(first) <= limit:
        return first
    cut = first[:limit]
    for sep in (". ", "! ", "? ", "; ", ", "):
        i = cut.rfind(sep)
        if i > limit // 2:
            return cut[:i + 1].strip()
    return cut.rsplit(" ", 1)[0] + "…"


DUP_WINDOW_DAYS = 7


def save_item(conn, item: dict, v: Verdict) -> str:
    """Кладёт новость в базу. Возвращает 'new', 'copy' (перепечатка уже
    найденного сюжета) или 'dup' (эта ссылка уже есть)."""
    url = canonical_url(item.get("url"))
    if url and conn.execute("SELECT 1 FROM neg_news WHERE url = %s", (url,)).fetchone():
        return "dup"
    key = title_key(item.get("title"))
    if key:
        published = item.get("published") or now()
        row = conn.execute(
            "SELECT id, source, also_in FROM neg_news WHERE title_key = %s AND "
            "COALESCE(published, collected_at) BETWEEN %s AND %s ORDER BY id LIMIT 1",
            (key, published - timedelta(days=DUP_WINDOW_DAYS),
             published + timedelta(days=DUP_WINDOW_DAYS))).fetchone()
        if row:
            others = [s for s in (row["also_in"] or "").split("; ") if s]
            src = item.get("source")
            if src and src != row["source"] and src not in others:
                others.append(src)
            conn.execute("UPDATE neg_news SET copies = copies + 1, also_in = %s, "
                         "analyzed_at = NULL WHERE id = %s",
                         ("; ".join(others) or None, row["id"]))
            return "copy"
    data = {
        "url": url, "post_url": item.get("post_url"), "title": item.get("title"),
        "source": item.get("source"), "source_domain": item.get("source_domain"),
        "source_id": item.get("source_id"), "collector": item["collector"],
        "query": item.get("query"), "published": item.get("published"),
        "lang": item.get("lang"), "summary": item.get("summary"), "text": item.get("text"),
        "text_status": item.get("text_status"), "title_key": key,
        "kz_outlet": bool(item.get("kz_outlet")), "tone": item.get("tone"),
        "analyzed_at": now(), **v.as_row(),
    }
    cols = ", ".join(data)
    marks = ", ".join(["%s"] * len(data))
    cur = conn.execute(f"INSERT INTO neg_news ({cols}) VALUES ({marks}) "
                       "ON CONFLICT (url) DO NOTHING", tuple(data.values()))
    return "new" if cur.rowcount else "dup"


def consider(conn, item: dict, stats: Counter) -> Verdict:
    v = analyze(item.get("title"), item.get("summary"), item.get("text"),
                kz_outlet=item.get("kz_outlet", False), source_region=item.get("region"),
                tone=item.get("tone"))
    stats["seen"] += 1
    if v.keep:
        stats[save_item(conn, item, v)] += 1
    else:
        stats["skipped"] += 1
    return v


def format_stats(stats: Counter) -> str:
    s = (f"просмотрено {stats['seen']}, новых {stats['new']}, перепечаток {stats['copy']}, "
         f"уже были {stats['dup']}, не подошли {stats['skipped']}")
    return s + (f", старше периода {stats['old']}" if stats["old"] else "")


# ---------------------------------------------------------------------------
# Google News
# ---------------------------------------------------------------------------
def gnews_queries() -> list[str]:
    """Все запросы — с site:kz (только казахстанские сайты, до 100 результатов);
    общие — ещё и без ограничения, чтобы поймать издания не в зоне .kz
    (kursiv.media, the-steppe.com, azattyq.org…). Чужие новости отсеет analyze()."""
    city = [t.format(city=c) for t in config.NEG_GNEWS_CITY_TEMPLATES
            for c in config.NEG_GNEWS_CITIES]
    qs = [f"{q} site:kz" for q in config.NEG_GNEWS_QUERIES + city]
    if config.NEG_GNEWS_ALSO_UNRESTRICTED:
        qs += config.NEG_GNEWS_QUERIES
    return list(dict.fromkeys(qs))


def collect_gnews(conn, days: int = 7) -> Counter:
    total = Counter()
    jobs = [(q, loc) for loc in config.NEG_GNEWS_LOCALES for q in gnews_queries()]
    log(f"[neg/gnews] запросов: {len(jobs)}")
    for i, (q, (hl, gl, ceid)) in enumerate(jobs, 1):
        params = {"q": f"{q} when:{min(days, 30)}d", "hl": hl, "gl": gl, "ceid": ceid}
        try:
            r = session.get(GNEWS_URL, params=params, timeout=30)
            feed = feedparser.parse(r.content)
        except Exception as e:
            log(f"[neg/gnews] {q!r}: ошибка {type(e).__name__}")
            continue
        st = Counter()
        for e in feed.entries:
            title, src = gnews_title(e)
            domain = domain_of(src.get("href"))
            consider(conn, {
                "url": e.get("link"), "title": title, "collector": "gnews", "query": q,
                "source": source_name(domain, src.get("title")), "source_domain": domain,
                "published": parse_date(e), "lang": hl, "kz_outlet": is_kz_domain(domain),
                # у Google News нет текста, ссылка — редирект: текст не докачивается
                "text_status": "skipped",
            }, st)
        conn.commit()
        total.update(st)
        log(f"[neg/gnews] {i}/{len(jobs)} {q!r}: {format_stats(st)}")
        time.sleep(config.GNEWS_PAUSE_SEC)
    return total


# ---------------------------------------------------------------------------
# GDELT (запросы с фильтром тональности)
# ---------------------------------------------------------------------------
def collect_gdelt(conn, days: int = 7, workers: int = 8) -> Counter:
    total = Counter()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=min(days, 90))
    log(f"[neg/gdelt] запросов: {len(config.NEG_GDELT_QUERIES)}")
    for q in config.NEG_GDELT_QUERIES:
        arts = gdelt_request(q, start, end)
        if arts is None:
            # GDELT так и не ответил после всех повторов (лимит запросов): остальные
            # запросы сейчас тоже упрутся в лимит — не ждём их, попробуем в следующий раз
            log("[neg/gdelt] GDELT не отвечает (лимит запросов) — остальные запросы "
                "пропускаю до следующего запуска")
            break
        items = []
        for x in arts:
            url = canonical_url(x.get("url"))
            if not url:
                continue
            domain = domain_of(url)
            items.append({
                "url": url, "title": x.get("title"), "collector": "gdelt", "query": q,
                "source": source_name(domain), "source_domain": domain,
                "published": to_dt(x.get("seendate")), "lang": x.get("language"),
                "kz_outlet": x.get("sourcecountry") == "Kazakhstan" or is_kz_domain(domain),
            })
        # Заголовок без негативных слов — ещё не повод выбросить: у GDELT отбор по
        # тональности всего текста. Для таких скачиваем текст и решаем по нему.
        st = Counter()
        need_text = []
        for it in items:
            if conn.execute("SELECT 1 FROM neg_news WHERE url = %s", (it["url"],)).fetchone():
                st["seen"] += 1
                st["dup"] += 1
                continue
            v = analyze(it["title"], kz_outlet=it["kz_outlet"])
            if v.keep:
                consider(conn, it, st)
            elif v.reason in ("нет негативных слов", "не про Казахстан") or v.reason.startswith("балл"):
                need_text.append(it)
            else:
                st["seen"] += 1
                st["skipped"] += 1
        if need_text:
            with ThreadPoolExecutor(workers) as ex:
                futs = {ex.submit(fetch_page, it["url"], True): it for it in need_text}
                for f in as_completed(futs):
                    it = futs[f]
                    text, status, meta = f.result()
                    it.update(text=text, text_status=status)
                    it["published"] = it["published"] or meta.get("date")
                    if it["source"] == it["source_domain"] and meta.get("sitename"):
                        it["source"] = meta["sitename"]       # домен -> название издания
                    consider(conn, it, st)
        conn.commit()
        total.update(st)
        log(f"[neg/gdelt] {q!r}: найдено {len(arts)}; {format_stats(st)}")
    return total


# ---------------------------------------------------------------------------
# Источники из таблицы neg_sources: RSS и Telegram
# ---------------------------------------------------------------------------
def _sources(conn, kind: str, only_id: int | None = None):
    q = "SELECT * FROM neg_sources WHERE kind = %s AND enabled"
    args = [kind]
    if only_id:
        q += " AND id = %s"
        args.append(only_id)
    return conn.execute(q + " ORDER BY id", args).fetchall()


def _source_done(conn, src, status: str, found: int, st: Counter, newest=None):
    conn.execute("UPDATE neg_sources SET last_run_at = now(), last_status = %s, last_found = %s, "
                 "last_kept = %s, total_kept = total_kept + %s, "
                 "last_item_at = COALESCE(%s, last_item_at) WHERE id = %s",
                 (status, found, st["new"], st["new"], newest, src["id"]))
    conn.commit()


def collect_rss(conn, days: int = 7, only_id: int | None = None) -> Counter:
    total = Counter()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    for src in _sources(conn, "rss", only_id):
        st = Counter()
        try:
            r = session.get(src["handle"], timeout=30)
            feed = feedparser.parse(r.content)
            if r.status_code != 200 or (feed.bozo and not feed.entries):
                raise RuntimeError(f"HTTP {r.status_code}" if r.status_code != 200
                                   else "не похоже на RSS")
        except Exception as e:
            msg = str(e) if isinstance(e, RuntimeError) else type(e).__name__
            log(f"[neg/rss] {src['name']}: НЕ ОТКРЫЛАСЬ ({msg}) {src['handle']}")
            _source_done(conn, src, f"error: {msg}", 0, st)
            continue
        newest = max((d for d in map(parse_date, feed.entries) if d), default=None)
        for e in feed.entries:
            published = parse_date(e)
            if published and published < since:
                st["old"] += 1
                continue                      # старые записи (бывают «замёрзшие» ленты)
            link = e.get("link")
            content = e.get("content") or []
            full = strip_html(content[0].get("value")) if content else ""
            summary = strip_html(e.get("summary") or e.get("description"))
            domain = domain_of(link)
            consider(conn, {
                "url": link, "title": strip_html(e.get("title")), "collector": "rss",
                "query": src["handle"], "source": src["name"], "source_domain": domain,
                "source_id": src["id"], "published": published,
                "summary": summary or None,
                # некоторые ленты отдают статью целиком — тогда качать не нужно
                "text": full if len(full) > 500 else None,
                "text_status": "ok" if len(full) > 500 else None,
                "kz_outlet": src["kz"], "region": src["region"],
            }, st)
        _source_done(conn, src, "ok", len(feed.entries), st, newest)
        total.update(st)
        log(f"[neg/rss] {src['name']}: в ленте {len(feed.entries)}; {format_stats(st)}")
    return total


TG_URL = "https://t.me/s/{handle}"
_SKIP_LINK_HOSTS = ("t.me", "telegram.me", "telegram.org", "instagram.com", "youtube.com",
                    "youtu.be", "wa.me", "whatsapp.com", "tiktok.com", "facebook.com",
                    "vk.com", "x.com", "twitter.com", "apps.apple.com", "play.google.com")


def _tg_text(el) -> str:
    for br in el.iter("br"):
        br.tail = "\n" + (br.tail or "")
    return re.sub(r"[ \t]+", " ", el.text_content()).strip()


def parse_telegram_page(html: str):
    """Посты со страницы t.me/s/<канал>: [(id, datetime, text, [ссылки])], before."""
    import lxml.html
    doc = lxml.html.fromstring(html)
    posts = []
    for m in doc.xpath('//div[@data-post]'):
        try:
            pid = int(m.get("data-post").rsplit("/", 1)[1])
        except (ValueError, IndexError):
            continue
        texts = m.xpath('.//div[contains(@class, "js-message_text")]')
        if not texts:
            continue                           # только фото / видео без подписи
        links = [a for a in texts[0].xpath('.//a/@href')
                 if a.startswith("http") and domain_of(a) not in _SKIP_LINK_HOSTS
                 and not any((domain_of(a) or "").endswith("." + h) for h in _SKIP_LINK_HOSTS)]
        dt = m.xpath('.//a[contains(@class, "tgme_widget_message_date")]//time/@datetime')
        posts.append((pid, to_dt(dt[0]) if dt else None, _tg_text(texts[0]), links))
    before = None
    prev = doc.xpath('//link[@rel="prev"]/@href')
    if prev and "before=" in prev[0]:
        before = prev[0].split("before=", 1)[1].split("&", 1)[0]
    return posts, before


def collect_telegram(conn, days: int = 7, pages: int | None = None,
                     only_id: int | None = None) -> Counter:
    pages = pages or config.NEG_TELEGRAM_PAGES
    since = datetime.now(timezone.utc) - timedelta(days=days)
    total = Counter()
    for src in _sources(conn, "telegram", only_id):
        handle = src["handle"].lstrip("@").rsplit("/", 1)[-1]
        st = Counter()
        found = 0
        before, status, newest = None, "ok", None
        for _ in range(pages):
            url = TG_URL.format(handle=handle) + (f"?before={before}" if before else "")
            try:
                r = session.get(url, timeout=30)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                posts, before = parse_telegram_page(r.text)
            except Exception as e:
                status = f"error: {e if isinstance(e, RuntimeError) else type(e).__name__}"
                log(f"[neg/telegram] {src['name']}: {status}")
                break
            if not posts and not found:
                status = "error: канал не найден или закрыт"
                break
            found += len(posts)
            dates = [p[1] for p in posts if p[1]] + ([newest] if newest else [])
            newest = max(dates, default=None)
            for pid, dt, text, links in posts:
                if dt and dt < since:
                    continue
                post_url = f"https://t.me/{handle}/{pid}"
                article = canonical_url(links[0]) if links else None
                consider(conn, {
                    # если пост ссылается на статью — храним статью (докачаем полный текст),
                    # иначе сам пост — это и есть текст новости
                    "url": article or post_url, "post_url": post_url,
                    "title": make_title(text), "collector": "telegram", "query": "@" + handle,
                    "source": src["name"],
                    "source_domain": domain_of(article) if article else "t.me",
                    "source_id": src["id"], "published": dt,
                    "summary": text if article else None, "text": None if article else text,
                    "text_status": None if article else "ok",
                    "kz_outlet": src["kz"], "region": src["region"],
                }, st)
            conn.commit()
            oldest = min((p[1] for p in posts if p[1]), default=None)
            if not before or (oldest and oldest < since):
                break
            time.sleep(config.NEG_TELEGRAM_PAUSE_SEC)
        _source_done(conn, src, status, found, st, newest)
        total.update(st)
        log(f"[neg/telegram] {src['name']} (@{handle}): постов {found}; {format_stats(st)}")
    return total


# ---------------------------------------------------------------------------
# Полный текст и повторная разметка
# ---------------------------------------------------------------------------
def fetch_text(conn, workers: int = 8, limit: int | None = None) -> int:
    conn.execute("UPDATE neg_news SET text_status = 'skipped' WHERE text_status IS NULL "
                 "AND (url IS NULL OR url LIKE 'https://news.google.com/%%')")
    conn.commit()
    rows = conn.execute("SELECT id, url FROM neg_news WHERE text_status IS NULL ORDER BY id DESC"
                        + (" LIMIT %s" if limit else ""), (limit,) if limit else ()).fetchall()
    conn.commit()
    log(f"[neg/text] к загрузке: {len(rows)}")
    done = ok = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_page, r["url"], True): r["id"] for r in rows}
        for f in as_completed(futs):
            text, status, meta = f.result()
            conn.execute(
                "UPDATE neg_news SET text = %s, text_status = %s, "
                "published = COALESCE(published, %s), title = COALESCE(NULLIF(title, ''), %s), "
                "analyzed_at = NULL WHERE id = %s",
                (text, status, meta.get("date"), meta.get("title"), futs[f]))
            conn.commit()
            done += 1
            ok += status == "ok"
            if done % 50 == 0:
                log(f"[neg/text] {done}/{len(rows)}, с текстом {ok}")
    log(f"[neg/text] готово: {done}, с текстом {ok}")
    return ok


def reanalyze(conn, all_rows: bool = False, batch: int = 500) -> Counter:
    """Пересчитывает категории, регионы и балл (после докачки текста или правки
    словарей в config.py). Непроверенные новости, которые больше не проходят
    отбор, удаляются; проверенные вручную и добавленные вручную — остаются."""
    st = Counter()
    where = "TRUE" if all_rows else "n.analyzed_at IS NULL"
    last_id = 0
    while True:
        rows = conn.execute(
            f"SELECT n.id, n.title, n.summary, n.text, n.kz_outlet, n.copies, n.tone, "
            f"n.collector, n.review_status, n.review_note, s.region AS src_region "
            f"FROM neg_news n LEFT JOIN neg_sources s ON s.id = n.source_id "
            f"WHERE {where} AND n.id > %s ORDER BY n.id LIMIT %s", (last_id, batch)).fetchall()
        if not rows:
            break
        updates, deletes = [], []
        for r in rows:
            last_id = r["id"]
            v = analyze(r["title"], r["summary"], r["text"], kz_outlet=r["kz_outlet"],
                        source_region=r["src_region"], copies=r["copies"], tone=r["tone"])
            protected = (r["collector"] == "manual" or r["review_status"] != "new"
                         or (r["review_note"] or "").strip())
            if not v.keep and not protected:
                deletes.append((r["id"],))
                continue
            row = v.as_row()
            updates.append((row["score"], row["categories"], row["keywords"], row["regions"],
                            row["kz_signal"], r["id"]))
        with conn.cursor() as cur:
            cur.executemany("UPDATE neg_news SET score = %s, categories = %s, keywords = %s, "
                            "regions = %s, kz_signal = %s, analyzed_at = now() WHERE id = %s",
                            updates)
            cur.executemany("DELETE FROM neg_news WHERE id = %s", deletes)
        conn.commit()
        st["updated"] += len(updates)
        st["deleted"] += len(deletes)
    log(f"[neg/analyze] обновлено {st['updated']}, убрано (больше не подходят) {st['deleted']}")
    return st
