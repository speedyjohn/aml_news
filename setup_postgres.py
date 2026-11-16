#!/usr/bin/env python3
"""
Первичная настройка PostgreSQL: создаёт пользователя и базу для программы
и записывает адрес подключения в файл .env.

  python setup_postgres.py                 # спросит пароль администратора postgres
  python setup_postgres.py --url postgresql://aml:пароль@127.0.0.1:5432/aml_news
                                           # база уже есть — только проверить и записать .env

Запускается автоматически из start.bat / start.sh, если файла .env ещё нет.
"""
import argparse
import getpass
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

ENV = Path(__file__).resolve().parent / ".env"


def write_env(url: str):
    lines = []
    if ENV.exists():
        lines = [ln for ln in ENV.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith("AML_DATABASE_URL=")]
    lines.insert(0, f"AML_DATABASE_URL={url}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Адрес базы записан в {ENV}")


def check(url: str):
    with psycopg.connect(url, connect_timeout=10) as c:
        v = c.execute("SHOW server_version").fetchone()[0]
    print(f"Подключение работает (PostgreSQL {v})")


def ask(prompt: str, default: str) -> str:
    v = input(f"{prompt} [{default}]: ").strip()
    return v or default


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--url", help="готовый адрес базы — только проверить и записать в .env")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="5432")
    p.add_argument("--admin", default="postgres", help="администратор PostgreSQL")
    p.add_argument("--db", default="aml_news", help="имя базы для программы")
    p.add_argument("--user", default="aml", help="пользователь базы для программы")
    p.add_argument("--yes", action="store_true", help="не задавать вопросов (кроме пароля)")
    a = p.parse_args()

    if a.url:
        check(a.url)
        write_env(a.url)
        return 0

    print("\nНастройка базы PostgreSQL для AML-скрининга.")
    print("Нужен пароль администратора PostgreSQL (его задавали при установке PostgreSQL).\n")
    if not a.yes:
        a.host = ask("Сервер", a.host)
        a.port = ask("Порт", a.port)
        a.admin = ask("Администратор", a.admin)
    admin_pw = getpass.getpass(f"Пароль пользователя {a.admin}: ")

    try:
        admin = psycopg.connect(host=a.host, port=a.port, user=a.admin, password=admin_pw,
                                dbname="postgres", connect_timeout=10, autocommit=True)
    except psycopg.OperationalError as e:
        print(f"\nНе удалось подключиться к PostgreSQL: {e}".rstrip())
        print("Проверьте, что PostgreSQL запущен, и пароль администратора.")
        return 1

    app_pw = secrets.token_urlsafe(18)
    with admin:
        exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (a.user,)).fetchone()
        if exists:
            admin.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(a.user), sql.Literal(app_pw)))
            print(f"Пользователь {a.user} уже был — пароль обновлён")
        else:
            admin.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(a.user), sql.Literal(app_pw)))
            print(f"Создан пользователь {a.user}")
        if admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (a.db,)).fetchone():
            print(f"База {a.db} уже есть")
            admin.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(a.db), sql.Identifier(a.user)))
        else:
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0")
                          .format(sql.Identifier(a.db), sql.Identifier(a.user)))
            print(f"Создана база {a.db}")

    url = f"postgresql://{quote(a.user)}:{quote(app_pw)}@{a.host}:{a.port}/{quote(a.db)}"
    check(url)
    write_env(url)
    print("\nГотово. Пароль программы хранится только в файле .env — не публикуйте его.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
