"""
Перенос данных из старой базы SQLite (aml_news.sqlite) в PostgreSQL.

  python manage.py import_sqlite                 # из aml_news.sqlite рядом с проектом
  python manage.py import_sqlite путь.sqlite
  python manage.py import_sqlite --auto          # тихо: только если в Postgres ещё пусто

Переносятся получатели, новости, совпадения (вместе со статусами проверки и
комментариями), пользователи панели, история запусков и настройки автозапуска.
Таблица, в которой в Postgres уже есть данные, пропускается — повторный запуск
ничего не задвоит.
"""
import sqlite3
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

import config
import db as aml_db
from collect import to_dt

# Порядок важен: сначала то, на что ссылаются внешние ключи
TABLES = ["entities", "articles", "matches",
          "auth_group", "auth_user", "auth_user_groups",
          "screening_job", "screening_schedule"]

# Колонки, которых не было в SQLite и у которых в Postgres нет значения по умолчанию
EXTRA_VALUES = {
    "screening_job": {"use_telegram": False},
    "screening_schedule": {"use_telegram": True},
}


def _pg_columns(conn, table):
    rows = conn.execute(
        "SELECT column_name, data_type, is_generated FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s", (table,)).fetchall()
    return {r[0]: r[1] for r in rows if r[2] != "ALWAYS"}


def _convert(value, pg_type):
    if value is None:
        return None
    if pg_type == "boolean":       # в SQLite булевы хранились как 0/1
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "t", "yes")
        return bool(value)
    if pg_type.startswith("timestamp"):
        return to_dt(value)
    if pg_type in ("integer", "bigint", "smallint"):
        return int(value)
    return value


class Command(BaseCommand):
    help = "Перенести данные из старой базы SQLite в PostgreSQL"

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", default=str(config.LEGACY_SQLITE_PATH))
        parser.add_argument("--auto", action="store_true",
                            help="запускать только если в PostgreSQL ещё нет данных скрининга")

    def handle(self, path, auto=False, **opts):
        src_path = Path(path)
        if not src_path.is_file():
            if auto:
                return
            raise CommandError(f"Файл не найден: {src_path}")

        conn = aml_db.connect()
        if auto and any(aml_db.count(conn, f"SELECT COUNT(*) FROM {t}")
                        for t in ("entities", "articles", "matches")):
            conn.close()
            return

        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        src_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.stdout.write(f"Перенос данных из {src_path} в PostgreSQL…")
        total = 0
        for table in TABLES:
            if table not in src_tables:
                continue
            pg_cols = _pg_columns(conn, table)
            if not pg_cols:
                self.stdout.write(f"  {table}: нет такой таблицы в PostgreSQL — пропуск "
                                  "(сначала выполните manage.py migrate)")
                continue
            if aml_db.count(conn, f"SELECT COUNT(*) FROM {table}"):
                self.stdout.write(f"  {table}: в PostgreSQL уже есть данные — пропуск")
                continue
            src_cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
            cols = [c for c in src_cols if c in pg_cols]
            extra = {k: v for k, v in EXTRA_VALUES.get(table, {}).items() if k not in cols}
            all_cols = cols + list(extra)
            n = 0
            with conn.cursor() as cur:
                with cur.copy(f"COPY {table} ({', '.join(all_cols)}) FROM STDIN") as cp:
                    q = f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"'
                    for row in src.execute(q):
                        cp.write_row([_convert(v, pg_cols[c]) for c, v in zip(cols, row)]
                                     + list(extra.values()))
                        n += 1
                if "id" in pg_cols:
                    cur.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                                f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table}")
            conn.commit()
            total += n
            self.stdout.write(f"  {table}: {n}")
        src.close()
        conn.close()
        self.stdout.write(self.style.SUCCESS(f"Готово, перенесено строк: {total}"))
