from datetime import timedelta
import sqlite3

import pytest

from app.config import Config, dollars, micros
from app.database.connection import Database
from app.database.models import Profile, UserError, moment, stamp
from app.database.repository import Repository


def test_constraints_transactions_and_owner(repo, clock):
    with repo.db.connect() as db:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0]
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("PRAGMA index_list('tweets')").fetchall()
    with pytest.raises(sqlite3.IntegrityError), repo.db.connect(write=True) as db:
        db.execute("INSERT INTO digest_tweets VALUES(44,66)")
    with pytest.raises(UserError, match="другому"):
        Repository(repo.db, 55)
    with pytest.raises(ValueError):
        stamp(clock().replace(tzinfo=None))
    with pytest.raises(ValueError):
        moment("2026-01-01T00:00:00")
    assert moment(stamp(clock())) == clock()


def test_author_lifecycle(repo, author, clock):
    with pytest.raises(UserError, match="уже"):
        repo.add(Profile("10", "Other", "name"))
    with pytest.raises(UserError):
        repo.add(Profile("11", "ALICE", "name"))
    repo.change_author("ALICE", "rename", "Подпись")
    assert repo.author("alice")["display_name"] == "Подпись"
    with pytest.raises(UserError):
        repo.change_author("Alice", "rename", " ")
    with pytest.raises(UserError):
        repo.change_author("none", "remove")
    with pytest.raises(ValueError):
        repo.change_author("Alice", "typo")
    repo.change_author("Alice", "pause")
    repo.change_author("Alice", "pause")
    assert not repo.authors(active_only=True)
    clock.step(hours=2)
    repo.change_author("alice", "resume")
    assert repo.authors(True)[0]["tracking_started_at"] == stamp(clock())
    repo.change_author("alice", "remove")
    assert repo.counts() == (0, 0, 0)


def test_settings_and_retention(repo, clock, pending, delivery):
    assert repo.settings()["digest_interval_hours"] == 12
    repo.interval(3)
    assert repo.settings()["next_due_at"] == stamp(clock() + timedelta(hours=3))
    repo.budget(0)
    with pytest.raises(UserError):
        repo.interval(5)
    with pytest.raises(UserError):
        repo.budget(-1)
    again = Repository(Database(repo.db.path), 42, clock=clock)
    assert again.settings()["monthly_budget_micros"] == 0
    again.reset()
    did = delivery.queue(pending, ["часть"], clock(), clock())
    with pytest.raises(UserError):
        repo.change_author("alice", "remove")
    clock.step(days=60)
    repo.prune()
    assert repo.history(did)
    assert delivery.flush()
    clock.step(days=31)
    repo.prune()
    assert repo.counts() == (2, 0, 0)
    assert repo.author("Bob")["last_tweet_id"] == "202"


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "0.0000001", "abc", "1000001"])
def test_invalid_amount(value):
    with pytest.raises(UserError):
        micros(value)


def test_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for k in ("TELEGRAM_BOT_TOKEN", "X_BEARER_TOKEN", "TELEGRAM_ALLOWED_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(UserError, match="TELEGRAM_BOT_TOKEN"):
        Config.load()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:local-test")
    monkeypatch.setenv("X_BEARER_TOKEN", "sensitive-test-value")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "-1")
    with pytest.raises(UserError, match="ID"):
        Config.load()
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "42")
    assert "sensitive-test" not in repr(Config.load())
    assert dollars(micros("5,015")) == "5.015"
    assert dollars(0) == "0"
    monkeypatch.setenv("X_POST_READ_USD", "0")
    with pytest.raises(UserError, match="ставки"):
        Config.load()
    monkeypatch.setenv("DIGEST_INTERVAL_HOURS", "4")
    with pytest.raises(UserError, match="INTERVAL"):
        Config.load()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bad")
    with pytest.raises(UserError, match="формат"):
        Config.load()


def test_old_volume_adds_schedule_state_without_losing_posts(repo, pending):
    # An existing installation has the same business tables and no scheduler journal.
    from app.database.connection import Database
    from app.database.repository import Repository
    with repo.db.connect(write=True) as db:
        db.execute("DROP TABLE schedule_state")
    reopened = Repository(Database(repo.db.path), repo.owner, clock=repo.clock)
    assert reopened.settings()["last_check_at"] is None
    assert reopened.settings()["summary_retry_at"] is None
    assert reopened.pending_count() == len(pending) and len(reopened.authors()) == 2
    reopened.record_check(repo.clock(), "Выпуск отправлен.")
    assert Repository(Database(repo.db.path), repo.owner).settings()["last_check_result"] == "Выпуск отправлен."
