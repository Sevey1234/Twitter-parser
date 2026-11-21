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
    assert "Подтверд" in str(chat.bot.send_message.call_args) or repo.conversation()[0] == "username"
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
    from app.services.scheduler import Scheduler
    digest, notify = Mock(), Mock()
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
    digest.run.return_value = "Новых публикаций нет."
    after._manual_job()
    notify.assert_called_with("Новых публикаций нет.")
    from app.database.models import UserError
    for err in (UserError("ошибка"), RuntimeError("private")):
        digest.run.side_effect = err
        after.execute(True)
        assert "private" not in notify.call_args.args[0]
    digest.run.side_effect = None
    digest.run.return_value = "Дайджест отправлен."
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
