@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv (
    echo Первый запуск: создаю окружение и ставлю зависимости, это займёт пару минут...
    python -m venv .venv
    if errorlevel 1 (
        echo Не найден Python. Установите Python 3.11+ с python.org и отметьте "Add to PATH".
        pause
        exit /b 1
    )
)
call .venv\Scripts\activate.bat

pip install -q -r requirements.txt
if errorlevel 1 ( pause & exit /b 1 )

rem База PostgreSQL: при первом запуске создаём пользователя и базу, адрес пишем в .env
if not exist .env (
    python setup_postgres.py
    if errorlevel 1 ( pause & exit /b 1 )
)

python manage.py migrate -v0
if errorlevel 1 (
    echo Не удалось подключиться к базе. Проверьте, что PostgreSQL запущен, и адрес в файле .env
    pause & exit /b 1
)
rem Один раз переносим данные из старой базы aml_news.sqlite, если она есть
python manage.py import_sqlite --auto
python manage.py ensure_admin

echo.
echo Панель: http://127.0.0.1:8000  (не закрывайте это окно, пока работаете)
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000/"
python manage.py runserver 127.0.0.1:8000
pause
