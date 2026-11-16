"""
Запуск команд main.py из панели в фоне. Одновременно выполняется один запуск
(SQLite не любит нескольких писателей), остальные ждут в очереди.
"""
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from django.conf import settings
from django.db import OperationalError, close_old_connections, connection
from django.utils import timezone

from .models import Job

_lock = threading.Lock()
_procs: dict[int, subprocess.Popen | None] = {}   # job_id -> текущий процесс
_stop_requested: set[int] = set()

MAIN = Path(settings.BASE_DIR) / "main.py"
LOG_DIR = Path(settings.MEDIA_ROOT) / "logs"


def _steps(job: Job) -> list[list[str]]:
    base = [sys.executable, "-u", str(MAIN), "--db", str(settings.AML_DB_PATH)]

    def collect():
        args = base + ["collect", "--days", str(job.days)]
        if job.use_gdelt:
            args.append("--gdelt")
        if job.use_gnews:
            args.append("--gnews")
        if job.use_rss:
            args.append("--rss")
        if job.slice_days:
            args += ["--slice-days", str(job.slice_days)]
        return args

    def load_names():
        args = base + ["load-names", job.names_file.path]
        if job.names_column:
            args += ["--column", job.names_column]
        return args

    def match():
        args = base + ["match"]
        # с новым списком нужно перепроверить и уже обработанные статьи
        if job.match_all or job.names_file:
            args.append("--all")
        if job.keep_weak:
            args.append("--keep-weak")
        return args

    if job.command == "collect":
        return [collect()]
    if job.command == "fetch-text":
        return [base + ["fetch-text"]]
    # Если к запуску приложен файл со списком — сначала загружаем его
    pre = [load_names()] if job.names_file else []
    if job.command == "match":
        return pre + [match()]
    if job.command == "load-names":
        return [load_names()]
    # pipeline
    return pre + [collect(), base + ["fetch-text"], match()]


def _execute(job: Job, log_path: Path) -> int:
    """Выполняет шаги запуска по очереди, вывод пишет в лог. Возвращает код завершения."""
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    rc = 0
    with open(log_path, "ab") as log:
        for argv in _steps(job):
            if job.pk in _stop_requested:
                break
            log.write(("\n$ " + " ".join(argv[2:]) + "\n").encode())
            log.flush()
            p = subprocess.Popen(argv, cwd=settings.BASE_DIR, stdout=log,
                                 stderr=subprocess.STDOUT, env=env)
            _procs[job.pk] = p
            rc = p.wait()
            if rc != 0:
                break
    return rc


def _run(job_id: int):
    close_old_connections()
    try:
        job = Job.objects.filter(pk=job_id).first()
        if job is None:
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"job_{job.pk}.log"
        job.status, job.started_at, job.log_file = "running", timezone.now(), str(log_path)
        job.save(update_fields=["status", "started_at", "log_file"])

        try:
            rc = _execute(job, log_path)
        except Exception:
            # сбой самой панели (не main.py): причину — в лог, запуск получает «Ошибка»
            rc = -1
            with open(log_path, "a", encoding="utf-8") as log:
                log.write("\n[панель] не удалось выполнить запуск:\n" + traceback.format_exc())

        job.returncode = rc
        job.finished_at = timezone.now()
        job.status = ("stopped" if job_id in _stop_requested
                      else "done" if rc == 0 else "failed")
        job.save(update_fields=["returncode", "finished_at", "status"])
    finally:
        with _lock:
            _procs.pop(job_id, None)
            _stop_requested.discard(job_id)
        connection.close()
        start_next()


def start_next():
    """Запускает следующий запуск из очереди, если сейчас ничего не выполняется."""
    with _lock:
        if _procs:
            return
        nxt = Job.objects.filter(status="queued").order_by("id").first()
        if not nxt:
            return
        _procs[nxt.pk] = None
        threading.Thread(target=_run, args=(nxt.pk,), daemon=True).start()


def submit(job: Job):
    job.status = "queued"
    job.save(update_fields=["status"])
    start_next()


def stop(job: Job) -> bool:
    with _lock:
        if job.pk not in _procs:
            if job.status == "queued":
                job.status = "stopped"
                job.save(update_fields=["status"])
                return True
            return False
        _stop_requested.add(job.pk)
        p = _procs.get(job.pk)
    if p and p.poll() is None:
        p.terminate()
    return True


def reconcile():
    """Запуски со статусом «выполняется», которыми не владеет этот процесс
    панели (например, панель перезапускали), помечаются как прерванные."""
    with _lock:
        owned = list(_procs)
    # Сначала только читаем: страница лога обновляется каждые несколько секунд,
    # и лишняя запись в базу во время работы main.py упирается в блокировку.
    try:
        stale = Job.objects.filter(status="running").exclude(pk__in=owned)
        if stale.exists():
            stale.update(status="interrupted", finished_at=timezone.now())
        if not owned and Job.objects.filter(status="queued").exists():
            start_next()
    except OperationalError:
        pass   # база занята — попробуем при следующем открытии страницы


def is_busy() -> bool:
    return bool(_procs)


def read_log(job: Job, max_bytes: int = 200_000) -> str:
    if not job.log_file or not os.path.exists(job.log_file):
        return ""
    with open(job.log_file, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        text = "…(начало лога обрезано)\n" + text.split("\n", 1)[-1]
    return text
