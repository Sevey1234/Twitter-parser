from unittest.mock import Mock

import telebot
from telebot import types
import pytest

from app.bot.handlers import register
from app.database.models import Profile
from app.database.repository import Repository
from app.services.author_service import Authors


def message(text, uid=42, kind="private", chat_id=None):
    return types.Message.de_json({"message_id": 1, "date": 10,
        "from": {"id": uid, "first_name": "Owner", "is_bot": False},
        "chat": {"id": uid if chat_id is None else chat_id, "type": kind}, "text": text})


@pytest.fixture
def chat(repo, budget, delivery):
    bot = telebot.TeleBot("123:test", threaded=False)
    bot.send_message = Mock()
    x = Mock()
    x.profile.return_value = Profile("100", "Alice", "Alice <A>")
    scheduler = Mock()
    scheduler.manual.return_value = "Проверяю"
    return register(bot, repo, Authors(repo, x), budget, scheduler, delivery)


def input_text(chat, value):
    chat.bot.process_new_messages([message(value)])
    return chat.bot.send_message.call_args.args[1]


def test_telebot_dispatch_owner_and_restart(chat, repo, clock):
    for utterance in [message("/start", 41), message("/start", 42, "group"), message("/start",42,chat_id=41)]:
        chat.bot.process_new_messages([utterance])
    assert not chat.bot.send_message.called
    assert "кабинет" in input_text(chat, "/start")
    input_text(chat, "/add_author")
    assert "Введите" in input_text(chat, "@@bad")
    assert repo.conversation()[0] == "username"
    assert "Добавить @Alice" in input_text(chat, "https://x.com/Alice")
    assert repo.counts()[0] == 0
    clock.step(minutes=5)
    chat.repo = Repository(repo.db, repo.owner, clock=clock)
    assert "добавлен" in input_text(chat, "Подтвердить")
    assert repo.author("alice")["tracking_started_at"].startswith("2026-11-21T09:05:")
    assert "уже добавлен" in input_text(chat, "/add_author @Alice")
    input_text(chat, "Отмена")
    assert repo.conversation() == (None, {})
    assert "список" in input_text(chat, "/help").lower()


def test_commands_buttons_crud(chat, repo):
    assert "пуст" in input_text(chat, "/authors")
    input_text(chat, "/add_author @Alice")
    assert "Нажмите" in input_text(chat, "сомневаюсь")
    input_text(chat, "Подтвердить")
    assert "@Alice" in input_text(chat, "/authors")
    assert "Пауза" in input_text(chat, "/pause_author @alice")
    assert "включено" in input_text(chat, "/resume_author @alice")
    input_text(chat, "/edit_author @alice")
    input_text(chat, "Моя подпись")
    assert "Моя подпись" in input_text(chat, "/authors")
    input_text(chat, "/interval")
    assert "Выберите" in input_text(chat, "18")
    assert "6 ч" in input_text(chat, "6")
    assert repo.settings()["digest_interval_hours"] == 6 and chat.scheduler.refresh.called
    assert "в месяц" in input_text(chat, "/budget")
    input_text(chat, "1,20")
    assert "1.2" in input_text(chat, "/settings")
    assert "запросов 0" in input_text(chat, "/stats")
    assert "пуста" in input_text(chat, "/history")
    assert "Используйте" in input_text(chat, "/history 1")
    assert "Проверяю" in input_text(chat, "/digest")
    assert "пуста" in input_text(chat, "/delivery")
    input_text(chat, "/reset_settings")
    input_text(chat, "Подтвердить")
    assert repo.settings()["digest_interval_hours"] == 12
    input_text(chat, "/remove_author alice")
    input_text(chat, "/cancel")
    assert repo.author("Alice")
    input_text(chat, "/remove_author none")
    assert "не найден" in chat.bot.send_message.call_args.args[1]
    input_text(chat, "/remove_author alice")
    input_text(chat, "Подтвердить")
    assert not repo.authors()
    assert "команд" in input_text(chat, "привет")
    assert "/help" in input_text(chat, "/unknown")


def test_chat_archive_and_unexpected_error(chat, pending, delivery, clock):
    did = delivery.queue(pending, ["независимый текст"], clock(), clock())
    assert "в очереди" in input_text(chat, "/history")
    assert "Архив" in input_text(chat, f"/history {did}")
    assert chat.bot.send_message.call_args.kwargs["parse_mode"] == "HTML"
    chat.scheduler.manual.side_effect = ValueError("secret")
    assert "secret" not in input_text(chat, "/digest")


def test_scheduler_due_restart_and_manual(repo, clock):
    from app.services.digest_service import CheckResult
    from app.services.scheduler import Scheduler
    digest, notify = Mock(), Mock()
    digest.run.return_value = CheckResult("empty", "Новых публикаций нет.")
    scheduler = Scheduler(repo, digest, notify)
    # The test clock is independent of the runner clock; inspect triggers without firing them.
    scheduler.refresh()
    scheduler.engine.start(paused=True)
    try:
        assert scheduler.engine.get_job("digest").trigger.interval.total_seconds() == 43200
        repo.interval(3)
        scheduler.refresh()
        assert scheduler.engine.get_job("digest").trigger.interval.total_seconds() == 10800
        scheduler.scheduled()
        assert not digest.run.called
        clock.step(hours=10)
        scheduler.scheduled()
        assert digest.run.call_count == 1
        assert repo.settings()["next_due_at"].startswith("2026-11-21T21:00")
    finally:
        scheduler.stop()
    after = Scheduler(repo, digest, notify)
    after.refresh()
    assert after.engine.get_job("digest").next_run_time.hour == 21
    assert "Проверяю" in after.manual()
    assert "в очередь" in after.manual()
    digest.run.return_value = CheckResult("empty", "Новых публикаций нет.")
    after._manual_job()
    notify.assert_called_with("Новых публикаций нет.")
    from app.database.models import UserError
    for err in (UserError("ошибка"), RuntimeError("private")):
        digest.run.side_effect = err
        after.execute(True)
        assert "private" not in notify.call_args.args[0]
    digest.run.side_effect = None
    digest.run.return_value = CheckResult("sent", "Дайджест отправлен.")
    notify.reset_mock()
    after.execute(True)
    notify.assert_not_called()


def test_main_real_wiring_and_shutdown(monkeypatch, tmp_path):
    from app import main
    from app.config import Config
    from app.database.models import UserError
    bot = telebot.TeleBot("42:test", threaded=False)
    bot.get_me = Mock()
    bot.infinity_polling = Mock()
    bot.stop_bot = Mock()
    monkeypatch.setattr(main.telebot, "TeleBot", lambda *args, **kwargs: bot)
    monkeypatch.setattr(main.signal, "signal", Mock())
    cfg = Config("42:test", "test", 42, str(tmp_path / "bot.db"), 12, 5_000_000, 5000, 10000,
                 "http://localhost", "local")
    main.serve(cfg)
    bot.infinity_polling.assert_called_once()
    bot.stop_bot.assert_called_once()
    bot.get_me.side_effect = RuntimeError("token")
    with pytest.raises(UserError, match="Telegram недоступен"):
        main.serve(cfg)
    monkeypatch.setattr(main.Config, "load", lambda: cfg)
    assert main.main() == 2
    monkeypatch.setattr(main, "serve", lambda c: None)
    assert main.main() == 0


def test_overlapping_manual_and_scheduled_checks_keep_due_slot(chat, repo, clock, delivery):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.database.models import moment
    from app.services.digest_service import DigestService
    from app.services.scheduler import Scheduler

    repo.interval(6)
    due = repo.settings()["next_due_at"]
    clock.step(hours=6)
    manual_entered, release_manual, scheduled_started = Event(), Event(), Event()
    timeline = Mock()

    def collect():
        if not manual_entered.is_set():
            manual_entered.set()
            assert release_manual.wait(5)
            return []
        return ["X temporarily unavailable"]

    timeline.collect.side_effect = collect
    service = DigestService(repo, timeline, Mock(), delivery)
    notify = Mock()
    scheduler = Scheduler(repo, service, notify)
    original_record = repo.record_check

    def record(started, status):
        original_record(started, status)
        if status == "Выполняется.":
            scheduled_started.set()

    repo.record_check = record
    with ThreadPoolExecutor(2) as pool:
        manual = pool.submit(scheduler.execute, True)
        assert manual_entered.wait(3)
        planned = pool.submit(scheduler.scheduled)
        try:
            assert scheduled_started.wait(3)
            assert repo.settings()["next_due_at"] == due
            assert "Выполняется." in input_text(chat, "/settings")
        finally:
            release_manual.set()
        assert manual.result(timeout=3).status == "empty"
        planned.result(timeout=3)

    row = repo.settings()
    assert moment(row["last_check_at"]) == clock() and moment(row["next_due_at"]).hour == 21
    assert timeline.collect.call_count == 2
    assert "Плановая проверка. Есть ошибки чтения X" in notify.call_args.args[0]
    settings = input_text(chat, "/settings")
    assert "18:00 МСК (21.11.2026 15:00 UTC)" in settings and "Есть ошибки чтения X" in settings
    assert "22.11.2026 00:00 МСК (21.11.2026 21:00 UTC)" in settings


def test_quiet_empty_scheduled_check_and_retry_notification(chat, repo, clock, delivery, pending):
    from app.ai.summarizer import ModelUnavailable
    from app.services.digest_service import DigestService
    from app.services.scheduler import Scheduler

    ai, timeline, notify = Mock(), Mock(), Mock()
    timeline.collect.return_value = []
    ai.summarize.side_effect = ModelUnavailable("Ollama недоступна.")
    service = DigestService(repo, timeline, ai, delivery)
    scheduler = Scheduler(repo, service, notify)
    clock.step(hours=12)
    scheduler.scheduled()
    assert "Ollama" in notify.call_args.args[0]
    assert "Повтор выжимки:" in input_text(chat, "/settings")
    assert "на выжимку: 4" in input_text(chat, "/delivery")
    clock.step(minutes=5)
    scheduler._maintenance()
    assert notify.call_count == 2 and "Повтор выжимки." in notify.call_args.args[0]
    assert "Повтор выжимки:" not in input_text(chat, "/settings")
    for author in ("Alice", "Bob"):
        repo.change_author(author, "pause")
    clock.step(hours=12)
    scheduler.scheduled()
    assert notify.call_count == 2
    assert repo.settings()["last_check_result"] == "Новых публикаций нет."
    assert not repo.history() and not delivery.bot.send_message.called


def test_apscheduler_actually_runs_overdue_slot_and_publishes_short_posts(repo, delivery, pending):
    from datetime import timedelta
    from threading import Event

    from apscheduler.events import EVENT_JOB_EXECUTED

    from app.ai.summarizer import Summarizer
    from app.database.models import moment, now, stamp
    from app.services.digest_service import DigestService
    from app.services.scheduler import Scheduler

    repo.clock = now
    due = now() - timedelta(seconds=2)
    with repo.db.connect(write=True) as db:
        db.execute("UPDATE bot_users SET next_due_at=?", (stamp(due),))
        db.execute("UPDATE tweets SET text='🔥' WHERE processing_status='pending'")
    timeline, transport, notify = Mock(), Mock(), Mock()
    timeline.collect.return_value = []
    service = DigestService(repo, timeline, Summarizer(transport, "http://ollama", "model"), delivery)
    scheduler = Scheduler(repo, service, notify)
    finished = Event()
    scheduler.engine.add_listener(lambda event: finished.set() if event.job_id == "digest" else None,
                                  EVENT_JOB_EXECUTED)
    scheduler.start()
    try:
        assert finished.wait(5)
    finally:
        scheduler.stop()
    assert repo.settings()["last_check_result"] == "Дайджест отправлен."
    assert moment(repo.settings()["next_due_at"]) == due + timedelta(hours=12)
    html = delivery.bot.send_message.call_args.args[1]
    assert "Новых твитов: 4. Авторов: 2." in html and "МСК (" in html
    assert not repo.pending() and len(repo.history()) == 1
    timeline.collect.assert_called_once()
    transport.post.assert_not_called()
    notify.assert_not_called()
