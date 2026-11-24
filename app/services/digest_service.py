import logging
from dataclasses import dataclass
from datetime import timedelta
from itertools import groupby
from threading import Lock
from typing import Literal

from app.ai.summarizer import ModelUnavailable, SummaryRejected
from app.database.models import moment, next_slot
from .formatting import render

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckResult:
    status: Literal["sent", "empty", "busy", "warning"]
    message: str


class DigestService:
    def __init__(self, repo, timeline, ai, delivery):
        self.repo, self.timeline, self.ai, self.delivery = repo, timeline, ai, delivery
        self.lock = Lock()

    def run(self, *, wait=False, scheduled_for=None):
        if not self.lock.acquire(blocking=wait):
            return CheckResult("busy", "Проверка уже выполняется.")
        try:
            ready = self.delivery.flush()
            # Keep collecting while Telegram is offline or the owner checks an uncertain part.
            read_errors = self.timeline.collect()
            if not ready:
                return CheckResult("warning", "Предыдущий дайджест в очереди. Подробности в /delivery.")
            result = self._publish(can_retry=True, scheduled_for=scheduled_for)
            if read_errors:
                note = "Есть ошибки чтения X. Подробности в /authors."
                return CheckResult("warning", note if result.status == "empty" else result.message + " " + note)
            return result
        finally:
            self.lock.release()

    def _publish(self, *, can_retry, scheduled_for=None):
        groups, selected, rejected = [], [], []
        problem = ""
        for handle, group in groupby(self.repo.pending(), key=lambda r: r["username"]):
            posts = list(group)
            try:
                summary = self.ai.summarize([r["text"] for r in posts])
            except SummaryRejected as exc:
                log.warning("Выжимка @%s: %s", handle, exc)
                rejected.append(f"@{handle} — {exc}")
                continue
            except ModelUnavailable as exc:
                problem = str(exc)
                break
            groups.append((handle, summary, [r["x_tweet_id"] for r in posts]))
            selected.extend(posts)

        notes = []
        if rejected:
            notes.append("Отложена выжимка. " + " ".join(rejected[:10]))
            if len(rejected) > 10:
                notes.append(f"Других отложенных авторов: {len(rejected) - 10}.")
        if problem:
            notes.append(problem)
        needs_retry = bool(notes)
        if needs_retry:
            if can_retry:
                self.repo.set_summary_retry(self.repo.clock() + timedelta(minutes=5))
                notes.append("Твиты сохранены. Время повтора — в /settings.")
            else:
                notes.append("Твиты сохранены до следующей проверки. Можно повторить через /digest.")

        result = CheckResult("empty", "Новых публикаций нет.")
        if groups:
            if self._send(groups, selected, scheduled_for):
                result = CheckResult("sent", "Дайджест отправлен.")
            else:
                result = CheckResult("warning", "Дайджест сохранён и ожидает доставки. Подробности в /delivery.")
        # Keep a due retry across a crash until its work is durably queued or deferred.
        if not can_retry or not needs_retry:
            self.repo.set_summary_retry(None)
        if needs_retry:
            if groups:
                notes.insert(0, result.message)
            return CheckResult("warning", " ".join(notes))
        return result

    def _send(self, groups, rows, scheduled_for):
        settings = self.repo.settings()
        hours = settings["digest_interval_hours"]
        end = self.repo.clock()
        history = self.repo.history()
        last_end = moment(history[0]["period_end"]) if history else end - timedelta(hours=hours)
        start = min(last_end, min(moment(r["created_at"]) for r in rows))
        next_due = moment(settings["next_due_at"])
        if next_due == scheduled_for:
            next_due = next_slot(next_due, hours, end)
        self.delivery.queue(rows, render(groups, start, end, hours, next_due), start, end)
        return self.delivery.flush()

    def maintenance(self):
        if self.lock.acquire(blocking=False):
            try:
                ready = self.delivery.flush()
                self.repo.prune()
                retry = self.repo.settings()["summary_retry_at"]
                if ready and retry and moment(retry) <= self.repo.clock():
                    return self._publish(can_retry=False)
            finally:
                self.lock.release()
