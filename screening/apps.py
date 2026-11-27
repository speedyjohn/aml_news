import os
import sys

from django.apps import AppConfig


class ScreeningConfig(AppConfig):
    name = "screening"
    verbose_name = "Скрининг"

    def ready(self):
        # Таблицы скрининга и негативных новостей создаёт db.py (их же использует
        # main.py). При старте панели доводим схему до актуальной версии, чтобы
        # панель открывалась и на пустой базе.
        import db as aml_db
        argv = " ".join(sys.argv)
        if any(cmd in argv for cmd in ("runserver", "migrate", "import_sqlite")):
            try:
                aml_db.connect().close()
            except aml_db.DatabaseNotConfigured as e:
                print(f"[panel] {e}", file=sys.stderr)
            except Exception as e:
                print(f"[panel] не удалось подключиться к PostgreSQL: {e}", file=sys.stderr)

        # Автозапуск — только в процессе, который реально обслуживает панель
        # (при runserver с автоперезагрузкой это дочерний процесс с RUN_MAIN=true)
        if "runserver" in argv and (os.environ.get("RUN_MAIN") == "true" or "--noreload" in argv):
            from . import scheduler
            scheduler.start()
