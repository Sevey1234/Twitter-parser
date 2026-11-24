import logging
import sqlite3
from datetime import timezone
from threading import Lock

from apscheduler.schedulers.background import BackgroundScheduler

from app.database.models import UserError, moment, next_slot
from .digest_service import CheckResult

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, repo, digest, notify):
        self.repo, self.digest, self.notify = repo, digest, notify
        self.engine = BackgroundScheduler(timezone=timezone.utc,
                        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None})
        self.manual_lock = Lock()
        self._check_lock = Lock()
        self._timer_lock = Lock()

    def refresh(self):
        with self._timer_lock:
            row = self.repo.settings()
            due = moment(row["next_due_at"])
            self.engine.add_job(self.scheduled, "interval", hours=row["digest_interval_hours"],
                                next_run_time=max(due, self.repo.clock()), id="digest", replace_existing=True)

    def scheduled(self):
        # The exact timer and minute tick can wake for the same SQLite deadline.
        if not self._check_lock.acquire(blocking=False):
            return
        try:
            self._run_due()
        finally:
            self._check_lock.release()

    def _run_due(self):
        row = self.repo.settings()
        due = moment(row["next_due_at"])
        started = self.repo.clock()
        if due > started:
            return
        self.repo.record_check(started, "Выполняется.")
        result = self.execute(False, scheduled_for=due)
        following = next_slot(due, row["digest_interval_hours"], self.repo.clock())
        self.repo.finish_check(result.message, due, following)
        self.refresh()

    def execute(self, manual, *, scheduled_for=None):
        try:
            # A scheduled check waits for an overlapping maintenance or manual job.
            result = self.digest.run(wait=not manual, scheduled_for=scheduled_for)
        except UserError as exc:
            result = CheckResult("warning", str(exc))
        except sqlite3.OperationalError as exc:
            if not manual and any(word in str(exc).lower() for word in ("locked", "busy")):
                # Keep the deadline in SQLite: maintenance will retry once the database is available.
                raise
            result = CheckResult("warning", "Ошибка доступа к SQLite. Подробности в журнале контейнера.")
            log.error("Ошибка доступа к SQLite: %s", getattr(exc, "sqlite_errorname", type(exc).__name__))
        except Exception as exc:
            log.error("Ошибка цикла: %s", type(exc).__name__)
            result = CheckResult("warning", "Сбой обработки. Состояние сохранено, повторите /digest.")
        label = "Ручная проверка" if manual else "Плановая проверка"
        log.info("%s: %s", label, result.message)
        if manual and result.status != "sent":
            self._notify(result.message)
        elif not manual and result.status == "warning":
            self._notify(f"{label}. {result.message}")
        return result

    def _maintenance(self):
        self.scheduled()
        result = self.digest.maintenance()
        if result:
            log.info("Повтор выжимки: %s", result.message)
            if result.status == "warning":
                self._notify("Повтор выжимки. " + result.message)

    def _notify(self, message):
        try:
            self.notify(message)
        except Exception as exc:
            log.warning("Не удалось отправить статус проверки: %s", type(exc).__name__)

    def manual(self):
        if not self.manual_lock.acquire(blocking=False):
            return "Ручная проверка уже поставлена в очередь."
        try:
            self.engine.add_job(self._manual_job, "date", id="manual", replace_existing=True)
        except Exception:
            self.manual_lock.release()
            raise
        return "Проверяю новые публикации."

    def _manual_job(self):
        try:
            self.execute(True)
        finally:
            self.manual_lock.release()

    def start(self):
        self.refresh()
        self.engine.add_job(self._maintenance, "interval", minutes=1, id="outbox")
        self.engine.start()

    def stop(self):
        self.engine.shutdown(wait=True)
