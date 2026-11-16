"""База SQLite: схема и подключение. С ней работают и main.py, и панель."""
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,            -- первый встреченный вариант написания
    variants    TEXT,                     -- другие варианты из списка, через " | "
    kind        TEXT NOT NULL,            -- org | person
    core        TEXT,
    pattern     TEXT,
    tokens      TEXT,
    skip_reason TEXT,                     -- почему не ищем (NULL = ищем)
    UNIQUE(kind, core)
);

CREATE TABLE IF NOT EXISTS articles (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,
    title        TEXT,
    source       TEXT,
    published    TEXT,
    lang         TEXT,
    country      TEXT,
    collector    TEXT,                    -- gdelt | gnews | rss
    query        TEXT,                    -- по какому запросу нашли
    summary      TEXT,
    text         TEXT,                    -- полный текст, если скачан
    text_status  TEXT,                    -- NULL | ok | empty | error: ...
    collected_at TEXT,
    matched_at   TEXT                     -- NULL = ещё не прогоняли по списку
);

-- Одна строка = одно совпадение «человек/организация + новость»
CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entities(id),
    article_id    INTEGER NOT NULL REFERENCES articles(id),
    entity_name   TEXT,
    entity_kind   TEXT,
    source        TEXT,
    title         TEXT,
    url           TEXT,
    published     TEXT,
    lang          TEXT,
    matched_text  TEXT,                   -- как имя написано в самой новости
    categories    TEXT,                   -- категории нарушений рядом с именем
    keywords      TEXT,                   -- конкретные слова
    in_title      INTEGER,
    score         INTEGER,                -- итоговый балл (0-100)
    score_raw     INTEGER,                -- балл до штрафа за «частое имя»
    frequent      INTEGER DEFAULT 0,      -- 1 = имя встречается в очень многих статьях
    snippet       TEXT,
    created_at    TEXT,
    review_status TEXT DEFAULT 'new',     -- new | relevant | false_positive
    review_note   TEXT,
    UNIQUE(entity_id, article_id)
);

CREATE INDEX IF NOT EXISTS ix_matches_score  ON matches(score DESC);
CREATE INDEX IF NOT EXISTS ix_matches_entity ON matches(entity_name);
CREATE INDEX IF NOT EXISTS ix_matches_article ON matches(article_id);
CREATE INDEX IF NOT EXISTS ix_articles_match ON articles(matched_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str) -> sqlite3.Connection:
    # timeout: если в этот момент пишет панель — подождать, а не падать сразу
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def insert_article(conn, **a) -> bool:
    """True, если статья новая."""
    a.setdefault("collected_at", now())
    cols = ", ".join(a)
    q = ", ".join("?" for _ in a)
    cur = conn.execute(f"INSERT OR IGNORE INTO articles ({cols}) VALUES ({q})", tuple(a.values()))
    return cur.rowcount > 0
