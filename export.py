"""Выгрузки: Excel/CSV/JSON для совпадений, имён и негативных новостей, полный JSON-дамп."""
import io
import json
from datetime import date, datetime, timezone

import pandas as pd
from psycopg import IsolationLevel

from db import now

COLUMNS = {
    "score": "Балл",
    "entity_name": "Имя / организация",
    "entity_kind": "Тип",
    "matched_text": "Как написано в новости",
    "categories": "Категории",
    "keywords": "Ключевые слова",
    "source": "Источник",
    "title": "Заголовок",
    "url": "Ссылка",
    "published": "Дата",
    "lang": "Язык",
    "in_title": "Имя в заголовке",
    "frequent": "Частое имя",
    "snippet": "Фрагмент",
    "review_status": "Статус проверки",
    "review_note": "Комментарий",
    "id": "ID совпадения",
}


def _json_default(v):
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def json_dumps(obj, **kw) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default, **kw)


def query_df(conn, sql: str, params=()) -> pd.DataFrame:
    cur = conn.execute(sql, params)
    cols = [c.name for c in cur.description]
    return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=cols)


def naive_local(df: pd.DataFrame, tz: str = "Asia/Almaty") -> pd.DataFrame:
    """Excel не умеет даты с часовым поясом: переводим в местное время без пояса."""
    for c in df.columns:
        if isinstance(df[c].dtype, pd.DatetimeTZDtype):
            df[c] = df[c].dt.tz_convert(tz).dt.tz_localize(None)
        elif df[c].dtype == object and df[c].map(lambda v: isinstance(v, datetime)).any():
            df[c] = pd.to_datetime(df[c], utc=True).dt.tz_convert(tz).dt.tz_localize(None)
    return df


def _write_table(df: pd.DataFrame, out: str, sheet: str, widths: dict | None = None,
                 link_col: str | None = None):
    if out.endswith(".json"):
        with open(out, "w", encoding="utf-8") as f:
            f.write(json_dumps(df.to_dict(orient="records"), indent=2))
        return
    df = naive_local(df)
    if out.endswith(".csv"):
        df.to_csv(out, index=False, encoding="utf-8-sig")
        return
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name=sheet)
        format_sheet(xw.sheets[sheet], df, widths, link_col)


def format_sheet(ws, df: pd.DataFrame, widths: dict | None = None, link_col: str | None = None):
    """Ширина колонок, закреплённая шапка, автофильтр, кликабельные ссылки."""
    from openpyxl.utils import get_column_letter
    for i, col in enumerate(df.columns, start=1):
        letter = get_column_letter(i)
        if widths and col in widths:
            w = widths[col]
        else:
            sample = [len(str(col))] + [len(str(v)) for v in df[col].head(200) if v is not None]
            w = min(max(max(sample) + 2, 8), 70)
        ws.column_dimensions[letter].width = w
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    if link_col and link_col in df.columns:
        ci = list(df.columns).index(link_col) + 1
        for row in ws.iter_rows(min_row=2, min_col=ci, max_col=ci):
            for c in row:
                if c.value and str(c.value).startswith("http"):
                    c.hyperlink = c.value
                    c.style = "Hyperlink"


def export_matches(conn, out: str, min_score: int = 0, status: str | None = None):
    q = "SELECT * FROM matches WHERE score >= %s"
    args = [min_score]
    if status:
        q += " AND review_status = %s"
        args.append(status)
    q += " ORDER BY score DESC, published DESC NULLS LAST"
    df = query_df(conn, q, args)
    df["entity_kind"] = df["entity_kind"].map({"org": "юрлицо", "person": "физлицо"})
    df = df[list(COLUMNS)]
    if not out.endswith(".json"):
        df = df.rename(columns=COLUMNS)
    widths = {"Балл": 7, "Имя / организация": 40, "Тип": 10, "Заголовок": 60, "Ссылка": 50,
              "Фрагмент": 90, "Дата": 18}
    _write_table(df, out, "Совпадения", widths, link_col="Ссылка")
    return len(df)


def export_skipped(conn, out: str):
    df = query_df(conn,
                  "SELECT name AS \"Имя\", kind AS \"Тип\", skip_reason AS \"Почему не ищем\", "
                  "variants AS \"Варианты написания\" FROM entities "
                  "WHERE skip_reason IS NOT NULL ORDER BY skip_reason, name")
    _write_table(df, out, "Не ищутся")
    return len(df)


# ---------------------------------------------------------------------------
# Негативные новости
# ---------------------------------------------------------------------------
NEG_STATUS_LABELS = {"new": "Не проверено", "relevant": "Актуально", "irrelevant": "Не актуально"}
NEG_COLLECTOR_LABELS = {"gnews": "Google News", "gdelt": "GDELT", "rss": "RSS",
                        "telegram": "Telegram", "manual": "Вручную"}
NEG_COLUMNS = {
    "published": "Дата публикации",
    "title": "Название",
    "source": "Издание",
    "url": "Ссылка",
    "score": "Балл",
    "categories": "Категории",
    "regions": "Регион",
    "keywords": "Ключевые слова",
    "review_status": "Статус",
    "review_note": "Комментарий",
    "copies": "Перепечаток",
    "also_in": "Также в изданиях",
    "collector": "Откуда собрано",
    "post_url": "Пост в Telegram",
    "text": "Текст",
    "collected_at": "Собрано",
    "id": "ID",
}
NEG_JSON_FIELDS = ["id", "title", "source", "source_domain", "url", "post_url", "published",
                   "lang", "summary", "text", "text_status", "categories", "keywords", "regions",
                   "kz_signal", "score", "tone", "copies", "also_in", "collector", "query",
                   "review_status", "review_note", "reviewed_by", "reviewed_at", "collected_at"]

EXCEL_CELL_LIMIT = 32000     # Excel не принимает в ячейку больше 32 767 символов


def neg_dataframe(rows: list[dict], with_text: bool = True) -> pd.DataFrame:
    """Таблица негативных новостей для Excel (русские заголовки, понятные значения)."""
    cols = [c for c in NEG_COLUMNS if with_text or c != "text"]
    df = pd.DataFrame(rows, columns=list(NEG_COLUMNS)).reindex(columns=cols)
    if not df.empty:
        df["review_status"] = df["review_status"].map(NEG_STATUS_LABELS).fillna(df["review_status"])
        df["collector"] = df["collector"].map(NEG_COLLECTOR_LABELS).fillna(df["collector"])
        if "text" in df:
            df["text"] = df["text"].map(
                lambda t: t if not isinstance(t, str) or len(t) <= EXCEL_CELL_LIMIT
                else t[:EXCEL_CELL_LIMIT] + " …(обрезано)")
    return naive_local(df).rename(columns=NEG_COLUMNS)


NEG_WIDTHS = {"Дата публикации": 17, "Название": 70, "Издание": 22, "Ссылка": 45, "Балл": 7,
              "Категории": 40, "Регион": 22, "Ключевые слова": 35, "Статус": 14,
              "Комментарий": 30, "Перепечаток": 9, "Также в изданиях": 30, "Откуда собрано": 13,
              "Пост в Telegram": 30, "Текст": 100, "Собрано": 17, "ID": 8}


def neg_xlsx_bytes(rows: list[dict], with_text: bool = True) -> bytes:
    df = neg_dataframe(rows, with_text)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="Негативные новости")
        format_sheet(xw.sheets["Негативные новости"], df, NEG_WIDTHS, link_col="Ссылка")
    return buf.getvalue()


def _neg_where(min_score=0, status=None, days=None):
    where, args = ["score >= %s"], [min_score]
    if status:
        where.append("review_status = %s")
        args.append(status)
    if days:
        where.append("COALESCE(published, collected_at) >= now() - make_interval(days => %s)")
        args.append(days)
    return " AND ".join(where), args


def export_neg(conn, out: str, min_score: int = 0, status: str | None = None,
               days: int | None = None, with_text: bool = True) -> int:
    where, args = _neg_where(min_score, status, days)
    cur = conn.execute(f"SELECT * FROM neg_news WHERE {where} "
                       "ORDER BY published DESC NULLS LAST, id DESC", args)
    cols = [c.name for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if out.endswith(".json"):
        fields = [f for f in NEG_JSON_FIELDS if with_text or f != "text"]
        data = {"exported_at": now(), "count": len(rows),
                "items": [{k: r.get(k) for k in fields} for r in rows]}
        with open(out, "w", encoding="utf-8") as f:
            f.write(json_dumps(data, indent=2))
    elif out.endswith(".csv"):
        neg_dataframe(rows, with_text).to_csv(out, index=False, encoding="utf-8-sig")
    else:
        with open(out, "wb") as f:
            f.write(neg_xlsx_bytes(rows, with_text))
    return len(rows)


# ---------------------------------------------------------------------------
# Полный дамп
# ---------------------------------------------------------------------------
DUMP_TABLES = ("entities", "articles", "matches")
NEG_DUMP_TABLES = ("neg_sources", "neg_news")
_TEXT_COLUMNS = {"articles": "text", "neg_news": "text"}
_SKIP_COLUMNS = {"search"}     # служебный tsvector для поиска


def iter_dump_json(conn, with_text: bool = True, tables=DUMP_TABLES):
    """Дамп таблиц одним JSON, по кускам (чтобы не держать базу в памяти):
    {"exported_at": ..., "counts": {...}, "<таблица>": [...], ...}.
    Читаем в одной транзакции REPEATABLE READ — снимок согласован, даже если
    параллельно идёт запуск."""
    conn.commit()        # уровень изоляции меняется только вне транзакции
    conn.isolation_level = IsolationLevel.REPEATABLE_READ
    try:
        with conn.transaction():
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
            head = {"exported_at": now(), "with_text": with_text, "counts": counts}
            yield json_dumps(head)[:-1]          # без закрывающей «}»
            for t in tables:
                cur = conn.execute(f"SELECT * FROM {t} LIMIT 0")
                cols = [c.name for c in cur.description if c.name not in _SKIP_COLUMNS]
                if not with_text and t in _TEXT_COLUMNS:
                    cols.remove(_TEXT_COLUMNS[t])
                yield f',\n"{t}": ['
                first = True
                with conn.cursor(name=f"dump_{t}") as cur:     # серверный курсор
                    cur.itersize = 1000
                    cur.execute(f"SELECT {', '.join(cols)} FROM {t} ORDER BY id")
                    while rows := cur.fetchmany(500):
                        chunk = ",\n".join(json_dumps(dict(zip(cols, r))) for r in rows)
                        yield ("\n" if first else ",\n") + chunk
                        first = False
                yield "\n]"
            yield "\n}\n"
    finally:
        conn.isolation_level = None


def dump_json(conn, out: str, with_text: bool = True, tables=DUMP_TABLES) -> dict:
    """Пишет дамп в файл, возвращает количество строк по таблицам."""
    with open(out, "w", encoding="utf-8") as f:
        for chunk in iter_dump_json(conn, with_text, tables):
            f.write(chunk)
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
