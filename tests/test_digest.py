import json
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from telebot.apihelper import ApiTelegramException

from app.ai.summarizer import MAX_SUMMARY_CHARS, Summarizer, batches, validate
from app.database.models import UserError
from app.services.delivery import Delivery
from app.services.digest_service import DigestService
from app.services.formatting import render, units


def model_response(text="Вышла версия 2.", evidence="version 2"):
    return {"message": {"content": json.dumps({"sentences": [{"text": text, "evidence": evidence}]})}}


def test_local_model_contract_and_reduction():
    seen = []
    def respond(req):
        cfg = json.loads(req.content)
        assert req.url.path == "/api/chat" and cfg["model"] == "local"
        assert cfg["stream"] is False and cfg["format"]["type"] == "object"
        assert cfg["format"]["properties"]["sentences"]["maxItems"] == 10
        sources = json.loads(cfg["messages"][1]["content"])
        seen.extend(sources)
        return httpx.Response(200, json=model_response("Вышла версия 2.", sources[0][:12]))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        ai = Summarizer(http, "http://ollama", "local")
        texts = [("version 2 — sample " * 180) + str(i) for i in range(4)]
        assert ai.summarize(texts) == "Вышла версия 2."
        assert all(str(i) in "".join(seen) for i in range(4))
        assert "Вышла версия 2." in seen
        with pytest.raises(UserError):
            ai.summarize([])
    texts.append(chr(1) * 1300)
    assert all(len(json.dumps(b, ensure_ascii=False).encode()) <= 5500 for b in batches(texts))
    assert "".join(s for b in batches(texts) for s in b) == "".join(texts)


@pytest.mark.parametrize("text,quote", [("English only", "version 2"), ("Вышла версия 13.", "version 2"),
                                       ("Вышла версия 2.", "invented"), ("я"*(MAX_SUMMARY_CHARS + 1), "version 2")])
def test_invalid_model_output_retried(text, quote):
    calls = []
    def respond(req):
        calls.append(req)
        return httpx.Response(200, json=model_response(text, quote))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(UserError, match="цитатами"):
            Summarizer(http, "http://ollama", "m").summarize(["version 2"])
    assert len(calls) == 2


def test_ten_sentences_fit_and_eleven_are_rejected():
    fact = "Автор подробно описал изменения в инструментах разработки и привёл пример их применения в проекте"
    texts = [f"{fact} {n}." for n in range(11)]
    items = [{"text": text, "evidence": text} for text in texts]
    result = validate(json.dumps({"sentences": items[:10]}), texts)
    assert result == " ".join(texts[:10]) and len(result) > 900
    assert list(batches([result, result])) == [[result, result]]
    with pytest.raises(ValueError):
        validate(json.dumps({"sentences": items}), texts)


def test_unavailable_model_and_empty_response():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as http:
        with pytest.raises(UserError, match="Ollama"):
            Summarizer(http, "http://ollama", "m").summarize(["abc"])
    with pytest.raises(ValueError):
        validate('{"sentences":[]}', ["a"])


class HTMLCheck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.links = [], []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "a", "i"}
        self.stack.append(tag)
        if tag == "a":
            self.links.append(dict(attrs)["href"])

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


def test_html_keeps_entire_author_and_all_source_links(clock):
    ids = list(map(str, range(500)))
    parts = render([("A", "тест <tag> & символы 👋", ["999"]), ("B", "<&>💡"*225, ids)],
                   clock(), clock(), 12, clock())
    docs = []
    assert len(parts) > 2 and "&lt;tag&gt; &amp;" in parts[0]
    assert sum(part.count("Новых твитов:") for part in parts) == 1
    assert "<b>@A</b>\nтест" in parts[0]
    assert any("<b>@B</b>\n&lt;" in part for part in parts)
    assert any("Источники (500):" in part for part in parts)
    for part in parts:
        assert units(part) <= 3700
        doc = HTMLCheck()
        doc.feed(part)
        assert not doc.stack
        docs.extend(doc.links)
    assert docs == ["https://x.com/A/status/999"] + [f"https://x.com/B/status/{i}" for i in ids]
    assert "Новых твитов: 501" in parts[-1] and "Авторов: 2" in parts[-1]


def test_grouping_no_duplicates_and_no_empty_digest(repo, pending, delivery, clock):
    timeline, ai = Mock(), Mock()
    timeline.collect.return_value = []
    ai.summarize.side_effect = ["Новости первого автора.", "Новости второго автора."]
    service = DigestService(repo, timeline, ai, delivery)
    assert service.run() == "Дайджест отправлен."
    assert [c.args[0] for c in ai.summarize.call_args_list] == [
            ["News 10/1", "News 10/2"], ["News 20/1", "News 20/2"]]
    html = delivery.bot.send_message.call_args.args[1]
    assert html.index("@Alice") < html.index("первого") < html.index("@Bob") < html.index("второго")
    assert service.run() == "Новых публикаций нет."
    assert delivery.bot.send_message.call_count == 1 and not repo.pending()
    timeline.collect.return_value = ["Ошибка"]
    assert "ошибки" in service.run()
    service.lock.acquire()
    assert "уже" in service.run()
    service.maintenance()
    service.lock.release()
    service.maintenance()


def test_model_failure_leaves_pending_and_no_mixed_snapshot(repo, pending, delivery, clock):
    ai, timeline = Mock(), Mock()
    ai.summarize.side_effect = UserError("Ollama")
    with pytest.raises(UserError):
        DigestService(repo, timeline, ai, delivery).run()
    assert len(repo.pending()) == 4 and not repo.history()
    repo.change_author("Alice", "pause")
    with pytest.raises(UserError, match="изменился"):
        delivery.queue(pending, ["test"], clock(), clock())
    assert not repo.history()


def tg_error(code, delay=1):
    return ApiTelegramException("sendMessage", None, {"description": "test", "error_code": code,
                                "parameters": {"retry_after": delay}})


def test_confirmed_chunks_429_and_restart(repo, pending, delivery, clock):
    did = delivery.queue(pending, ["a","b","c"], clock(), clock())
    delivery.bot.send_message.side_effect = [SimpleNamespace(message_id=100), tg_error(429, 45)]
    assert not delivery.flush()
    assert [p["status"] for p in repo.history(did)] == ["sent","pending","pending"]
    assert repo.history(did)[0]["message_id"] == 100
    restarted = Delivery(repo, Mock())
    restarted.bot.send_message.side_effect = [SimpleNamespace(message_id=101),SimpleNamespace(message_id=102)]
    restarted.recover()
    assert not restarted.flush() and not restarted.bot.send_message.called
    assert "pending" in restarted.inspect_or_resolve("")
    with pytest.raises(UserError):
        restarted.queue(pending, ["dup"], clock(), clock())
    clock.step(seconds=45)
    assert restarted.flush() and restarted.open_id() is None
    assert len(restarted.bot.send_message.call_args_list) == 2
    assert repo.history()[0]["status"] == "sent" and not repo.pending()


@pytest.mark.parametrize("failure", [TimeoutError(), tg_error(502)])
def test_ambiguous_send_requires_owner_and_never_automatic(repo, pending, delivery, clock, failure):
    did = delivery.queue(pending, ["a"], clock(), clock())
    delivery.bot.send_message.side_effect = failure
    assert not delivery.flush()
    assert "Если видите" in delivery.inspect_or_resolve("")
    assert not delivery.flush() and delivery.bot.send_message.call_count == 1
    with pytest.raises(UserError):
        delivery.inspect_or_resolve("1 99 sent")
    delivery.inspect_or_resolve(f"{did} 1 retry")
    delivery.bot.send_message.side_effect = None
    assert delivery.flush()
    assert "пуста" in delivery.inspect_or_resolve("")


def test_crash_between_send_and_ack(repo, pending, clock, delivery):
    did = delivery.queue(pending, ["a"], clock(), clock())
    delivery.mark(did, 1, "sending", expected="pending")
    delivery.recover()
    assert not delivery.flush() and not delivery.bot.send_message.called
    delivery.inspect_or_resolve(f"{did} 1 sent")
    assert repo.history()[0]["status"] == "sent"
    with pytest.raises(UserError):
        delivery.mark(did, 1, "sent", expected="uncertain")


def test_collection_continues_while_telegram_delivery_is_uncertain(repo, clock, delivery, pending):
    from app.database.models import stamp
    delivery.queue(pending, ["старый выпуск"], clock(), clock())
    delivery.bot.send_message.side_effect = TimeoutError()
    assert not delivery.flush()
    timeline = Mock()
    def collect():
        with repo.db.connect(write=True) as db:
            db.execute("""INSERT INTO tweets(x_tweet_id,author_id,text,created_at,fetched_at,processing_status)
                       VALUES(?,?,?,?,?,'pending')""",
                       ("777", pending[0]["author_id"], "new post", stamp(clock()), stamp(clock())))
        return []
    timeline.collect.side_effect = collect
    ai = Mock()
    assert "/delivery" in DigestService(repo, timeline, ai, delivery).run()
    assert [post["x_tweet_id"] for post in repo.pending()] == ["777"]
    assert len(repo.history()) == 1 and delivery.bot.send_message.call_count == 1
    ai.summarize.assert_not_called()
