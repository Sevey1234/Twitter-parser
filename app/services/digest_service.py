from datetime import timedelta
from itertools import groupby
from threading import Lock

from app.database.models import moment
from .formatting import render


class DigestService:
    def __init__(self, repo, timeline, ai, delivery):
        self.repo, self.timeline, self.ai, self.delivery = repo, timeline, ai, delivery
        self.lock = Lock()

    def run(self):
        if not self.lock.acquire(blocking=False):
            return "Проверка уже выполняется."
        try:
            ready = self.delivery.flush()
            # Keep collecting while Telegram is offline or the owner checks an uncertain part.
            errors = self.timeline.collect()
            if not ready:
                return "Предыдущий дайджест в очереди. Новые записи сохранены. Подробности в /delivery."
            rows = self.repo.pending()
            if not rows:
                return "Есть ошибки чтения. Подробности в /authors." if errors else "Новых публикаций нет."
            groups = []
            for handle, group in groupby(rows, key=lambda r: r["username"]):
                posts = list(group)
                summary = self.ai.summarize([r["text"] for r in posts])
                groups.append((handle, summary, [r["x_tweet_id"] for r in posts]))
            settings = self.repo.settings()
            hours = settings["digest_interval_hours"]
            end = self.repo.clock()
            history = self.repo.history()
            last_end = moment(history[0]["period_end"]) if history else end - timedelta(hours=hours)
            start = min(last_end, min(moment(r["created_at"]) for r in rows))
            next_due = moment(settings["next_due_at"])
            if next_due <= end:
                step = timedelta(hours=hours)
                next_due += ((end - next_due) // step + 1) * step
            parts = render(groups, start, end, hours, next_due)
            self.delivery.queue(rows, parts, start, end)
            delivered = self.delivery.flush()
            if not delivered:
                return "Дайджест сохранён и ожидает доставки. Подробности в /delivery."
            return ("Дайджест отправлен. Ошибки отдельных авторов — в /authors." if errors
                    else "Дайджест отправлен.")
        finally:
            self.lock.release()

    def maintenance(self):
        if self.lock.acquire(blocking=False):
            try:
                self.delivery.flush()
                self.repo.prune()
            finally:
                self.lock.release()
