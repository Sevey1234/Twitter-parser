import logging
from datetime import timedelta, timezone
from threading import Lock

from apscheduler.schedulers.background import BackgroundScheduler

from app.database.models import UserError, moment, stamp
from .digest_service import CheckResult

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, repo, digest, notify):
        self.repo, self.digest, self.notify = repo, digest, notify
        self.engine = BackgroundScheduler(timezone=timezone.utc,
                        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None})
        self.manual_lock = Lock()

    def refresh(self):
        row = self.repo.settings()
        due = moment(row["next_due_at"])
        self.engine.add_job(self.scheduled, "interval", hours=row["digest_interval_hours"],
                            next_run_time=max(due, self.repo.clock()), id="digest", replace_existing=True)

    def scheduled(self):
        row = self.repo.settings()
        due = moment(row["next_due_at"])
        if due > self.repo.clock():
            return
        started = self.repo.clock()
        self.repo.record_check(started, "Выполняется.")
        result = self.execute(False)
        self.repo.record_check(started, result.message)
        # A concurrent /interval command may have installed a different schedule.
        step = timedelta(hours=row["digest_interval_hours"])
        following = due + ((self.repo.clock() - due) // step + 1) * step
        with self.repo.db.connect(write=True) as db:
            db.execute("UPDATE bot_users SET next_due_at=? WHERE next_due_at=?",
                       (stamp(following), row["next_due_at"]))
        self.refresh()

    def execute(self, manual):
        try:
            # A scheduled check waits for an overlapping maintenance or manual job.
            result = self.digest.run(wait=not manual)
        except UserError as exc:
            result = CheckResult("warning", str(exc))
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
