"""
Фоновый поток панели: раз в минуту подхватывает очередь запусков и проверяет
настройки «Автозапуска» — когда подошло время, ставит запуск в очередь.
Работает, пока открыта панель.
"""
import threading
import time

from django.db import close_old_connections
from django.utils import timezone

_started = False


def _tick():
    from . import jobs
    from .models import Job, Schedule

    # после перезапуска панели очередь должна поехать дальше сама,
    # а не ждать, пока кто-нибудь откроет страницу «Запуски»
    jobs.reconcile()
    s = Schedule.objects.filter(pk=1).first()
    if not s or not s.enabled:
        return
    if s.last_run_at and timezone.now() < s.next_run_at():
        return
    if jobs.is_busy() or Job.objects.filter(status="queued").exists():
        return   # дождёмся, пока освободится
    job = Job.objects.create(command=s.command, use_gdelt=s.use_gdelt, use_gnews=s.use_gnews,
                             use_rss=s.use_rss, days=s.days, trigger="по расписанию")
    s.last_run_at, s.last_job = timezone.now(), job
    s.save(update_fields=["last_run_at", "last_job"])
    jobs.submit(job)


def _loop():
    time.sleep(10)
    while True:
        try:
            close_old_connections()
            _tick()
        except Exception as e:     # не роняем поток из-за временной ошибки
            print("[scheduler]", e)
        time.sleep(60)


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="aml-scheduler").start()
