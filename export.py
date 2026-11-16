import json

import pandas as pd

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


def export_matches(conn, out: str, min_score: int = 0, status: str | None = None):
    q = "SELECT * FROM matches WHERE score >= ?"
    args = [min_score]
    if status:
        q += " AND review_status = ?"
        args.append(status)
    q += " ORDER BY score DESC, published DESC"
    df = pd.read_sql_query(q, conn, params=args)
    df["entity_kind"] = df["entity_kind"].map({"org": "юрлицо", "person": "физлицо"})
    df = df[list(COLUMNS)]

    if out.endswith(".json"):
        with open(out, "w", encoding="utf-8") as f:
            json.dump(df.to_dict(orient="records"), f, ensure_ascii=False, indent=2)
    elif out.endswith(".csv"):
        df.rename(columns=COLUMNS).to_csv(out, index=False, encoding="utf-8-sig")
    else:
        df = df.rename(columns=COLUMNS)
        with pd.ExcelWriter(out, engine="openpyxl") as xw:
            df.to_excel(xw, index=False, sheet_name="Совпадения")
            ws = xw.sheets["Совпадения"]
            widths = {"A": 7, "B": 40, "C": 10, "D": 30, "E": 30, "F": 30, "G": 20,
                      "H": 60, "I": 50, "J": 18, "K": 6, "L": 8, "M": 8, "N": 90, "O": 14, "P": 30, "Q": 10}
            for col, w in widths.items():
                ws.column_dimensions[col].width = w
            ws.freeze_panes = "C2"
            ws.auto_filter.ref = ws.dimensions
            for row in ws.iter_rows(min_row=2, min_col=9, max_col=9):
                for c in row:
                    if c.value:
                        c.hyperlink = c.value
                        c.style = "Hyperlink"
    return len(df)


def export_skipped(conn, out: str):
    df = pd.read_sql_query(
        "SELECT name AS 'Имя', kind AS 'Тип', skip_reason AS 'Почему не ищем', "
        "variants AS 'Варианты написания' FROM entities WHERE skip_reason IS NOT NULL "
        "ORDER BY skip_reason, name", conn)
    if out.endswith(".csv"):
        df.to_csv(out, index=False, encoding="utf-8-sig")
    else:
        df.to_excel(out, index=False)
    return len(df)


DUMP_TABLES = ("entities", "articles", "matches")


def iter_dump_json(conn, with_text: bool = True):
    """Полный дамп данных скрининга одним JSON, по кускам (чтобы не держать базу в памяти):
    {"exported_at": ..., "counts": {...}, "entities": [...], "articles": [...], "matches": [...]}.
    Читаем в одной транзакции — снимок согласован, даже если параллельно идёт запуск."""
    conn.execute("BEGIN")
    try:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in DUMP_TABLES}
        head = {"exported_at": now(), "with_text": with_text, "counts": counts}
        yield json.dumps(head, ensure_ascii=False)[:-1]          # без закрывающей «}»
        for t in DUMP_TABLES:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            if t == "articles" and not with_text:
                cols.remove("text")
            yield f',\n"{t}": ['
            cur = conn.execute(f"SELECT {', '.join(cols)} FROM {t} ORDER BY id")
            first = True
            while rows := cur.fetchmany(500):
                chunk = ",\n".join(json.dumps(dict(zip(cols, r)), ensure_ascii=False)
                                   for r in rows)
                yield ("\n" if first else ",\n") + chunk
                first = False
            yield "\n]"
        yield "\n}\n"
    finally:
        conn.rollback()


def dump_json(conn, out: str, with_text: bool = True) -> dict:
    """Пишет дамп в файл, возвращает количество строк по таблицам."""
    with open(out, "w", encoding="utf-8") as f:
        for chunk in iter_dump_json(conn, with_text):
            f.write(chunk)
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in DUMP_TABLES}
