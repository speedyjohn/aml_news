from django.apps import AppConfig


class ScreeningConfig(AppConfig):
    name = "screening"
    verbose_name = "Скрининг"

    def ready(self):
        # Таблицы entities/articles/matches создаёт main.py; на всякий случай
        # создаём их и здесь, чтобы панель открывалась и на пустой базе.
        from django.conf import settings
        import db as aml_db
        try:
            aml_db.connect(str(settings.AML_DB_PATH)).close()
        except Exception:
            pass

        # Автозапуск — только в процессе, который реально обслуживает панель
        # (при runserver с автоперезагрузкой это дочерний процесс с RUN_MAIN=true)
        import os
        import sys
        argv = " ".join(sys.argv)
        if "runserver" in argv and (os.environ.get("RUN_MAIN") == "true" or "--noreload" in argv):
            from . import scheduler
            scheduler.start()
