#!/usr/bin/env bash
# Запуск панели на Mac / Linux: ./start.sh
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Первый запуск: создаю окружение и ставлю зависимости..."
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt
# База PostgreSQL: при первом запуске создаём пользователя и базу, адрес пишем в .env
[ -f .env ] || python setup_postgres.py
python manage.py migrate -v0
python manage.py import_sqlite --auto   # один раз переносим данные из старой aml_news.sqlite
python manage.py ensure_admin
echo "Панель: http://127.0.0.1:8000  (Ctrl+C — остановить)"
( sleep 3; (xdg-open http://127.0.0.1:8000/ || open http://127.0.0.1:8000/) >/dev/null 2>&1 ) &
python manage.py runserver 127.0.0.1:8000
