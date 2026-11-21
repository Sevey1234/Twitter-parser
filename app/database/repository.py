import json
import sqlite3
from datetime import timedelta

from .models import UserError, now, stamp

INTERVALS = (3, 6, 12, 24)


class Repository:
    def __init__(self, database, owner, hours=12, budget=5_000_000, clock=now):
        self.db, self.owner, self.clock = database, owner, clock
        self.defaults = hours, budget
        with self.db.connect(write=True) as db:
            existing = db.execute("SELECT telegram_user_id FROM bot_users").fetchone()
            if existing and existing[0] != owner:
                raise UserError("Эта база принадлежит другому Telegram ID. Проверьте путь к базе.")
            db.execute("""INSERT INTO bot_users(telegram_user_id,digest_interval_hours,
                       monthly_budget_micros,next_due_at) VALUES(?,?,?,?)
                       ON CONFLICT(telegram_user_id) DO NOTHING""",
                       (owner, hours, budget, self.next_due(hours)))

    def next_due(self, hours):
        return stamp(self.clock() + timedelta(hours=hours))

    def settings(self):
        with self.db.connect() as db:
            return db.execute("SELECT * FROM bot_users").fetchone()

    def interval(self, hours):
        if hours not in INTERVALS:
            raise UserError("Выберите 3, 6, 12 или 24 часа.")
        with self.db.connect(write=True) as db:
            db.execute("UPDATE bot_users SET digest_interval_hours=?, next_due_at=?",
                       (hours, self.next_due(hours)))

    def budget(self, micros):
        if not 0 <= micros <= 10**12:
            raise UserError("Бюджет должен быть от 0 до 1 000 000 USD.")
        with self.db.connect(write=True) as db:
            db.execute("UPDATE bot_users SET monthly_budget_micros=?", (micros,))

    def reset(self):
        hours, budget = self.defaults
        with self.db.connect(write=True) as db:
            db.execute("""UPDATE bot_users SET digest_interval_hours=?, monthly_budget_micros=?,
                       next_due_at=?, state=NULL, state_data='{}'""",
                       (hours, budget, self.next_due(hours)))

    def conversation(self):
        row = self.settings()
        return row["state"], json.loads(row["state_data"])

    def set_state(self, state=None, data=None):
        with self.db.connect(write=True) as db:
            db.execute("UPDATE bot_users SET state=?,state_data=?", (state, json.dumps(data or {})))

    def authors(self, active_only=False):
        with self.db.connect() as db:
            return db.execute("""SELECT * FROM authors WHERE (?=0 OR is_active=1)
                              ORDER BY username COLLATE NOCASE""", (active_only,)).fetchall()

    def author(self, username):
        with self.db.connect() as db:
            return db.execute("SELECT * FROM authors WHERE username=?", (username,)).fetchone()

    def add(self, profile):
        when = stamp(self.clock())
        try:
            with self.db.connect(write=True) as db:
                db.execute("""INSERT INTO authors(x_user_id,username,display_name,tracking_started_at,
                           created_at) VALUES(?,?,?,?,?)""",
                           (profile.x_id, profile.username, profile.name, when, when))
        except sqlite3.IntegrityError:
            raise UserError("Этот автор уже добавлен.") from None

    def change_author(self, username, action, name=None):
        with self.db.connect(write=True) as db:
            author = db.execute("SELECT * FROM authors WHERE username=?", (username,)).fetchone()
            if not author:
                raise UserError("Автор не найден в списке.")
            aid = author["id"]
            if action == "rename":
                if not name or not 1 <= len(name.strip()) <= 80:
                    raise UserError("Введите подпись длиной от 1 до 80 символов.")
                db.execute("UPDATE authors SET display_name=? WHERE id=?", (name.strip(), aid))
                return
            if action == "remove":
                if db.execute("SELECT 1 FROM tweets WHERE author_id=? AND processing_status='queued'",
                              (aid,)).fetchone():
                    raise UserError("Сначала завершите доставку подготовленного дайджеста.")
                db.execute("DELETE FROM authors WHERE id=?", (aid,))
                return
            if action not in ("pause", "resume"):
                raise ValueError("Unknown author operation")
            active = action == "resume"
            if active == bool(author["is_active"]):
                return
            db.execute("DELETE FROM sync_state WHERE author_id=?", (aid,))
            db.execute("DELETE FROM tweets WHERE author_id=? AND processing_status='staged'", (aid,))
            if active:
                db.execute("""UPDATE authors SET is_active=1,tracking_started_at=?,last_tweet_id=NULL,
                           checked_at=NULL,last_error=NULL WHERE id=?""", (stamp(self.clock()), aid))
            else:
                db.execute("UPDATE authors SET is_active=0 WHERE id=?", (aid,))

    def pending(self):
        with self.db.connect() as db:
            return db.execute("""SELECT t.*,a.username FROM tweets t JOIN authors a ON a.id=t.author_id
                              WHERE t.processing_status='pending' AND a.is_active=1
                              ORDER BY a.username COLLATE NOCASE,t.created_at,t.id""").fetchall()

    def history(self, digest_id=None):
        with self.db.connect() as db:
            if digest_id is None:
                return db.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 10").fetchall()
            return db.execute("SELECT * FROM digest_parts WHERE digest_id=? ORDER BY part",
                              (digest_id,)).fetchall()

    def prune(self, days=30):
        cutoff = stamp(self.clock() - timedelta(days=days))
        with self.db.connect(write=True) as db:
            db.execute("""DELETE FROM tweets WHERE id IN (SELECT dt.tweet_id FROM digest_tweets dt
                       JOIN digests d ON d.id=dt.digest_id WHERE d.status='sent' AND d.sent_at<?)""",
                       (cutoff,))
            db.execute("DELETE FROM digests WHERE status='sent' AND sent_at<?", (cutoff,))

    def counts(self):
        with self.db.connect() as db:
            return tuple(db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                         for table in ("authors", "tweets", "digests"))
