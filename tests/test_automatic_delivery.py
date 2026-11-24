import sqlite3
from datetime import timedelta
from threading import Event
from unittest.mock import Mock

import httpx
import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from app.database.models import Profile, moment, now, stamp
from app.services.digest_service import DigestService
from app.services.scheduler import Scheduler
from app.services.tweet_service import Timeline


def test_running_bot_collects_and_sends_at_its_future_deadline(repo, clock, client, delivery):
    clock.time = now() - timedelta(hours=3)
    repo.add(Profile("10", "Alice", "Alice"))
    source = {"id": "103", "created_at": stamp(now() - timedelta(minutes=5)), "text": "Вышел выпуск."}
    x = Mock(return_value=httpx.Response(200, json={"data": [source], "meta": {"result_count": 1}}))
    ai, notify = Mock(), Mock()
    ai.summarize.return_value = source["text"]
    scheduler = Scheduler(repo, DigestService(repo, Timeline(repo, client(x)), ai, delivery), notify)
    finished = Event()
    scheduler.engine.add_listener(lambda e: finished.set() if e.job_id == "digest" else None, EVENT_JOB_EXECUTED)
    # Arrange a real upcoming three-hour deadline without waiting three hours in the suite.
    clock.time = now() - timedelta(hours=3) + timedelta(seconds=1)
    repo.interval(3)
    due = moment(repo.settings()["next_due_at"])
    repo.clock = now
    scheduler.start()
    try:
        assert finished.wait(10)
    finally:
        scheduler.stop()
    x.assert_called_once()
    delivery.bot.send_message.assert_called_once()
    assert "Alice/status/103" in delivery.bot.send_message.call_args.args[1]
    state = repo.settings()
    assert moment(state["last_check_at"]) >= due and state["last_check_result"] == "Дайджест отправлен."
    assert moment(state["next_due_at"]) == due + timedelta(hours=3)
    notify.assert_not_called()


@pytest.mark.parametrize("fail_at", ["record_check", "collect"])
def test_missed_deadline_recovered_by_live_scheduler(repo, clock, client, delivery, monkeypatch, fail_at):
    # The first timer fires but SQLite is temporarily busy. Nothing has been fetched or queued.
    clock.time = now() - timedelta(hours=3, minutes=1)
    repo.interval(3)
    due = moment(repo.settings()["next_due_at"])
    repo.add(Profile("10", "Alice", "Alice"))
    source = {"id": "101", "created_at": stamp(due - timedelta(minutes=5)), "text": "Короткая новость."}
    x = Mock(return_value=httpx.Response(200, json={"data": [source], "meta": {"result_count": 1}}))
    clock.time = now()
    ai, notify = Mock(), Mock()
    ai.summarize.return_value = source["text"]
    timeline = Timeline(repo, client(x))
    scheduler = Scheduler(repo, DigestService(repo, timeline, ai, delivery), notify)
    owner = repo if fail_at == "record_check" else timeline
    original = getattr(owner, fail_at)

    def temporarily_locked(*args):
        monkeypatch.setattr(owner, fail_at, original)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(owner, fail_at, temporarily_locked)
    missed, tick = Event(), Event()

    def listener(event):
        if event.job_id == "digest" and event.code == EVENT_JOB_ERROR:
            missed.set()
        if event.job_id == "outbox" and event.code == EVENT_JOB_EXECUTED:
            tick.set()

    scheduler.engine.add_listener(listener, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
    scheduler.start()
    try:
        assert missed.wait(5)
        assert moment(repo.settings()["next_due_at"]) == due and not repo.history()
        # Wake the registered maintenance job now instead of waiting a real minute.
        scheduler.engine.modify_job("outbox", next_run_time=now())
        assert tick.wait(5)
    finally:
        scheduler.stop()

    delivery.bot.send_message.assert_called_once()
    assert "Alice/status/101" in delivery.bot.send_message.call_args.args[1]
    assert moment(repo.settings()["next_due_at"]) == due + timedelta(hours=3)
    assert repo.settings()["last_check_result"] == "Дайджест отправлен."
    x.assert_called_once()
    notify.assert_not_called()


def test_overdue_manual_issue_shows_the_outstanding_deadline(repo, clock, pending, delivery):
    ai, timeline = Mock(), Mock()
    ai.summarize.return_value = "Новые публикации."
    timeline.collect.return_value = []
    due = repo.settings()["next_due_at"]
    clock.time = moment(due) + timedelta(minutes=20)
    service = DigestService(repo, timeline, ai, delivery)
    assert service.run().status == "sent"
    html = delivery.bot.send_message.call_args.args[1]
    assert "Наступило время плановой проверки — 22.11.2026 00:00 МСК" in html
    assert repo.settings()["next_due_at"] == due and repo.settings()["last_check_at"] is None


def test_interval_change_during_collection_keeps_the_new_deadline(repo, clock, delivery):
    repo.interval(3)
    clock.step(hours=3)
    timeline = Mock()
    scheduler = Scheduler(repo, DigestService(repo, timeline, Mock(), delivery), Mock())

    def change_interval():
        repo.interval(12)
        scheduler.refresh()
        return []

    timeline.collect.side_effect = change_interval
    scheduler.scheduled()
    following = clock() + timedelta(hours=12)
    assert moment(repo.settings()["next_due_at"]) == following
    assert scheduler.engine.get_job("digest").next_run_time == following


@pytest.mark.parametrize("request_manual", [False, True], ids=["automatic-only", "manual-between-slots"])
def test_posts_after_empty_slot_follow_the_three_hour_schedule(repo, clock, client, delivery, request_manual):
    clock.time = moment("2026-11-24T12:11:00Z")
    repo.interval(3)
    repo.add(Profile("10", "Alice", "Alice"))
    posts = [
        {"id": "102", "text": "Вторая запись.", "created_at": "2026-11-24T15:43:00Z"},
        {"id": "101", "text": "Первая запись.", "created_at": "2026-11-24T15:20:00Z"},
    ]

    def timeline(request):
        params = request.url.params
        found = [p for p in posts if moment(p["created_at"]) < moment(params["end_time"])
                 and int(p["id"]) > int(params.get("since_id", "0"))]
        return httpx.Response(200, json={"data": found, "meta": {"result_count": len(found)}})

    x, ai, notify = Mock(side_effect=timeline), Mock(), Mock()
    ai.summarize.return_value = "Две записи."
    scheduler = Scheduler(repo, DigestService(repo, Timeline(repo, client(x)), ai, delivery), notify)
    clock.step(hours=3)  # 18:11 Moscow
    scheduler.scheduled()
    assert repo.settings()["last_check_result"] == "Новых публикаций нет."
    delivery.bot.send_message.assert_not_called()
    clock.step(minutes=46)  # 18:57 Moscow
    scheduler._maintenance()
    x.assert_called_once()
    if request_manual:
        assert scheduler.execute(True).status == "sent"
    assert moment(repo.settings()["next_due_at"]) == moment("2026-11-24T18:11:00Z")
    clock.time = moment("2026-11-24T18:11:00Z")  # 21:11 Moscow
    scheduler.scheduled()
    scheduler._maintenance()
    assert x.call_count == 2 + int(request_manual)
    html = delivery.bot.send_message.call_args.args[1]
    assert "Alice/status/101" in html and "Alice/status/102" in html
    delivery.bot.send_message.assert_called_once()
    ai.summarize.assert_called_once()
    assert len(repo.history()) == 1 and repo.pending_count() == 0
    notify.assert_not_called()
