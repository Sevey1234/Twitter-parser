from datetime import timedelta

from app.database.models import UserError, moment, stamp
from .twitter_client import ExpiredPage


class Timeline:
    def __init__(self, repo, twitter):
        self.repo, self.twitter = repo, twitter

    def window(self, aid):
        # Keep a stable upper bound for every page, including retries after a restart.
        end = stamp((self.repo.clock() - timedelta(seconds=10)).replace(microsecond=0))
        with self.repo.db.connect(write=True) as db:
            author = db.execute("SELECT * FROM authors WHERE id=? AND is_active=1", (aid,)).fetchone()
            if not author or max(author["tracking_started_at"], author["checked_at"] or "") >= end:
                return None
            db.execute("INSERT OR IGNORE INTO sync_state(author_id,end_time) VALUES(?,?)", (aid, end))
            state = db.execute("SELECT * FROM sync_state WHERE author_id=?", (aid,)).fetchone()
            return author, state

    def save_page(self, author, state, posts, token, size):
        lower = moment(author["tracking_started_at"])
        upper = moment(state["end_time"])
        floor_id = int(author["last_tweet_id"] or 0)
        in_window = [p for p in posts if lower < p.created < upper and int(p.x_id) > floor_id]
        eligible = [p for p in in_window if (p.original_created or p.created) > lower]
        high = max([int(state["high_id"] or 0), *(int(p.x_id) for p in in_window)])
        seen = state["seen_count"] + len(posts)
        if seen >= 800 and not token:
            raise UserError("Достигнут предел ленты X (800). Полнота не подтверждена, курсор сохранён.")
        with self.repo.db.connect(write=True) as db:
            row = db.execute("SELECT * FROM sync_state WHERE author_id=?", (author["id"],)).fetchone()
            if not row or dict(row) != dict(state):
                return False
            when = stamp(self.repo.clock())
            db.executemany("""INSERT INTO tweets(x_tweet_id,author_id,text,created_at,fetched_at,processing_status)
                           SELECT ?,?,?,?,?,'staged' WHERE NOT EXISTS
                           (SELECT 1 FROM removed_posts WHERE x_tweet_id=?)
                           ON CONFLICT(x_tweet_id) DO UPDATE SET text=excluded.text
                           WHERE tweets.processing_status IN ('pending','staged')""",
                           [(p.original_id or p.x_id, author["id"], p.text, stamp(p.original_created or p.created),
                             when, p.original_id or p.x_id) for p in eligible])
            if token:
                db.execute("""UPDATE sync_state SET pagination_token=?,page_size=?,high_id=?,seen_count=?
                           WHERE author_id=?""", (token, size, str(high) if high else None, seen, author["id"]))
            else:
                db.execute("""UPDATE tweets SET processing_status='pending' WHERE author_id=?
                           AND processing_status='staged'""", (author["id"],))
                db.execute("""UPDATE authors SET last_tweet_id=coalesce(?,last_tweet_id),checked_at=?,
                           last_error=NULL WHERE id=?""", (str(high) if high else None, state["end_time"], author["id"]))
                db.execute("DELETE FROM sync_state WHERE author_id=?", (author["id"],))
        return True

    def sync(self, aid):
        visited = set()
        restarted = False
        while True:
            window = self.window(aid)
            if not window:
                return
            author, state = window
            try:
                posts, token, size = self.twitter.timeline(author, state)
            except ExpiredPage:
                if restarted or not state["pagination_token"]:
                    raise
                with self.repo.db.connect(write=True) as db:
                    db.execute("""UPDATE sync_state SET pagination_token=NULL,seen_count=0,page_size=NULL,
                               high_id=NULL WHERE author_id=? AND end_time=? AND pagination_token=?""",
                               (aid, state["end_time"], state["pagination_token"]))
                visited.clear()
                restarted = True
                continue
            if token and (token == state["pagination_token"] or token in visited):
                raise UserError("X повторил токен страницы. Курсор сохранён.")
            if not self.save_page(author, state, posts, token, size) or not token:
                return
            visited.add(token)

    def collect(self):
        errors = []
        for author in self.repo.authors(active_only=True):
            try:
                self.sync(author["id"])
            except UserError as exc:
                with self.repo.db.connect(write=True) as db:
                    db.execute("UPDATE authors SET last_error=? WHERE id=?", (str(exc), author["id"]))
                errors.append(f"@{author['username']}: {exc}")
        return errors
