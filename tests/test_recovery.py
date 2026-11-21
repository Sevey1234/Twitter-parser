from datetime import timedelta

import httpx
import pytest

from app.database.models import UserError, stamp
from app.services.tweet_service import Timeline


def snowflake(dt, sequence=0):
    milliseconds = int(dt.timestamp() * 1000)
    return str(((milliseconds - 1288834974657) << 22) + sequence)


def test_edited_posts_cannot_import_old_history_or_repeat_a_digest(repo, author, clock, client, delivery):
    started = clock()
    clock.step(hours=1)
    old = snowflake(started - timedelta(days=1))
    new = snowflake(started + timedelta(seconds=4))
    edit_old = snowflake(started + timedelta(minutes=45))
    def edited(original, xid, when):
        return {"id": xid, "text": "edited", "created_at": stamp(when),
                "edit_history_tweet_ids": [original, xid]}
    payload = [edited(old, edit_old, started + timedelta(minutes=45)),
               edited(new, new, started + timedelta(seconds=4))]
    def respond(req):
        return httpx.Response(200, json={"data": payload, "meta": {"result_count": len(payload)}})
    sync = Timeline(repo, client(respond))
    sync.sync(author["id"])
    assert [r["x_tweet_id"] for r in repo.pending()] == [new]
    assert repo.author("Alice")["last_tweet_id"] == edit_old
    delivery.queue(repo.pending(), ["выжимка"], clock(), clock())
    delivery.flush()
    clock.step(hours=1)
    recent = clock() - timedelta(minutes=3)
    payload[:] = [edited(new, snowflake(recent), recent)]
    sync.sync(author["id"])
    assert not repo.pending() and repo.counts() == (1, 1, 1)


def test_erasure_suppresses_staged_source_and_redacts_archive(repo, pending, delivery, clock):
    xid = pending[0]["x_tweet_id"]
    did = delivery.queue(pending, ["выпуск"], clock(), clock())
    delivery.mark(did, 1, "sending", expected="pending")
    with pytest.raises(UserError):
        delivery.erase(xid)
    delivery.mark(did, 1, "sent", message_id=111)
    delivery.bot.delete_message.side_effect = RuntimeError("too old")
    assert "Вручную" in delivery.erase(xid)
    assert not repo.history() and not repo.pending()
    with repo.db.connect() as db:
        assert db.execute("SELECT x_tweet_id FROM removed_posts").fetchone()[0] == xid


def test_commands_to_erase_and_cancel(repo, pending, delivery, clock, budget):
    from unittest.mock import Mock
    from app.bot.handlers import Chat
    chat = Chat(delivery.bot, repo, Mock(), budget, Mock(), delivery)
    with pytest.raises(UserError):
        chat.command("forget_tweet", "bad")
    xid = pending[0]["x_tweet_id"]
    did = delivery.queue(pending, ["выпуск"], clock(), clock())
    delivery.flush()
    chat.command("forget_tweet", xid)
    chat.answer("Подтвердить")
    delivery.bot.delete_message.assert_called_once_with(42, 12)
    assert not repo.history(did)


def test_do_not_reread_content_removed_during_pagination(repo, author, clock, client, delivery):
    clock.step(hours=1)
    posts = [{"id":"111","text":"withdrawn","created_at":"2026-11-21T09:30:00Z"}]
    delivery.erase("111")
    sync = Timeline(repo, client(lambda r: httpx.Response(200, json={"data":posts, "meta":{"result_count":1}})))
    sync.sync(author["id"])
    assert not repo.pending() and repo.author("Alice")["last_tweet_id"] == "111"
