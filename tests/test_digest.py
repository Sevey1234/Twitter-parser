import json
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from telebot.apihelper import ApiTelegramException

from app.ai.summarizer import (
    MAX_BATCH_ITEMS, MAX_SUMMARY_CHARS, InvalidSummary, ModelUnavailable, Summarizer,
    SummaryRejected, batches, evidence_excerpts, normalized, validate,
)
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
        with pytest.raises(SummaryRejected):
            Summarizer(http, "http://ollama", "m").generate(["version 2"])
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
    assert service.run().message == "Дайджест отправлен."
    assert [c.args[0] for c in ai.summarize.call_args_list] == [
            ["News 10/1", "News 10/2"], ["News 20/1", "News 20/2"]]
    html = delivery.bot.send_message.call_args.args[1]
    assert html.index("@Alice") < html.index("первого") < html.index("@Bob") < html.index("второго")
    assert service.run().message == "Новых публикаций нет."
    assert delivery.bot.send_message.call_count == 1 and not repo.pending()
    timeline.collect.return_value = ["Ошибка"]
    assert "ошибки" in service.run().message
    service.lock.acquire()
    assert "уже" in service.run().message
    service.maintenance()
    service.lock.release()
    service.maintenance()


def test_model_failure_leaves_pending_and_no_mixed_snapshot(repo, pending, delivery, clock):
    ai, timeline = Mock(), Mock()
    timeline.collect.return_value = []
    ai.summarize.side_effect = ModelUnavailable("Ollama недоступна.")
    result = DigestService(repo, timeline, ai, delivery).run()
    assert result.status == "warning" and "Ollama" in result.message
    assert repo.settings()["summary_retry_at"]
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
    assert "/delivery" in DigestService(repo, timeline, ai, delivery).run().message
    assert [post["x_tweet_id"] for post in repo.pending()] == ["777"]
    assert len(repo.history()) == 1 and delivery.bot.send_message.call_count == 1
    ai.summarize.assert_not_called()


def test_reactions_links_and_mentions_keep_sources_without_inventing_news():
    transport = Mock()
    ai = Summarizer(transport, "http://ollama", "m")
    assert "Оригиналы" in ai.summarize(["🔥", "https://t.co/Ab9", "@alice 👋"])
    transport.post.assert_not_called()
    ai.generate = Mock(return_value="Автор поздоровался.")
    assert ai.summarize(["https://t.co/Ab9", "GM!", "GM!"]) == "Автор поздоровался."
    ai.generate.assert_called_once_with(["GM!"])


def test_short_original_only_after_two_invalid_translations():
    prompts = []
    def respond(req):
        prompts.append(json.loads(req.content)["messages"][0]["content"])
        return httpx.Response(200, json=model_response("Лишнее утверждение.", "invented"))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        ai = Summarizer(http, "http://ollama", "m")
        assert ai.summarize(["GM <3", "GM <3", "👋"]) == "Короткая публикация в оригинале: «GM <3»"
        assert "Подтверждающая цитата не найдена" in prompts[1]
        assert len(prompts) == 2
        assert ai.summarize(["GM!", "Hi"]) == "Короткие публикации в оригинале: «GM!» · «Hi»"
        with pytest.raises(SummaryRejected):
            ai.summarize(["Long source. " * 20])
        with pytest.raises(SummaryRejected):
            ai.summarize([f"Short {n}" for n in range(11)])


def test_quote_wrapping_and_linebreaks_are_accepted_but_rewording_is_rejected():
    raw = model_response("Вышла версия 2.", "«version  2»")["message"]["content"]
    assert validate(raw, ["Next version\n2 is ready"]) == "Вышла версия 2."
    with pytest.raises(ValueError):
        validate(raw, ["Next version numbers 2 and 3"])
    with pytest.raises(ValueError):
        validate(model_response("Вышла версия 2.", ".")["message"]["content"], ["version 2."])


def test_partial_issue_retry_from_sqlite_after_restart(repo, pending, delivery, clock):
    from app.database.connection import Database
    from app.database.repository import Repository
    ai, timeline = Mock(), Mock()
    timeline.collect.return_value = []
    reason = "Модель указала неверную ссылку на фрагмент источника."
    ai.summarize.side_effect = [SummaryRejected(reason), "Выжимка второго автора."]
    service = DigestService(repo, timeline, ai, delivery)
    report = service.run()
    assert report.status == "warning" and "@Alice" in report.message and "отправлен" in report.message
    assert reason in report.message
    html = delivery.bot.send_message.call_args.args[1]
    assert "@Alice" not in html and "@Bob" in html and "Новых твитов: 2. Авторов: 1." in html
    assert len(repo.pending()) == 2 and "на выжимку: 2" in delivery.inspect_or_resolve("")
    assert [r["x_tweet_id"] for r in repo.pending()] == ["101", "102"]
    assert not service.maintenance()
    clock.step(minutes=4, seconds=59)
    restarted = Repository(Database(repo.db.path), repo.owner, clock=clock)
    assert restarted.settings()["summary_retry_at"] == repo.settings()["summary_retry_at"]
    offline_x = Mock()
    recovered_ai = Mock()
    recovered_ai.summarize.return_value = "Выжимка первого автора."
    recovered = DigestService(restarted, offline_x, recovered_ai, Delivery(restarted, delivery.bot))
    assert not recovered.maintenance()
    clock.step(seconds=1)
    assert recovered.maintenance().status == "sent"
    assert "@Alice" in delivery.bot.send_message.call_args.args[1]
    assert "@Bob" not in delivery.bot.send_message.call_args.args[1]
    assert not restarted.pending() and not restarted.settings()["summary_retry_at"]
    assert not recovered.maintenance() and delivery.bot.send_message.call_count == 2
    offline_x.collect.assert_not_called()
    timeline.collect.assert_called_once()


def test_failed_local_retry_is_bounded_without_sending_empty_issue(repo, pending, delivery, clock):
    ai, timeline = Mock(), Mock()
    ai.summarize.side_effect = ModelUnavailable("Ollama недоступна.")
    timeline.collect.return_value = []
    service = DigestService(repo, timeline, ai, delivery)
    due = repo.settings()["next_due_at"]
    assert service.run().status == "warning"
    first_retry = repo.settings()["summary_retry_at"]
    clock.step(minutes=2)
    assert service.run().status == "warning" and repo.settings()["summary_retry_at"] == first_retry
    clock.step(minutes=3)
    assert "следующей проверки" in service.maintenance().message
    clock.step(minutes=12)
    assert not service.maintenance() and not repo.settings()["summary_retry_at"]
    assert repo.settings()["next_due_at"] == due
    assert not repo.history() and repo.pending_count() == len(pending)
    assert ai.summarize.call_count == 3 and timeline.collect.call_count == 2
    delivery.bot.send_message.assert_not_called()


def test_malformed_first_response_can_be_repaired():
    requests = []
    def respond(req):
        requests.append(json.loads(req.content)["messages"][0]["content"])
        payload = {"message": {"content": "{broken"}} if len(requests) == 1 else model_response(evidence=["1.1"])
        return httpx.Response(200, json=payload)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        ai = Summarizer(http, "http://ollama", "local")
        assert ai.summarize(["👀"]) == "В публикации только ссылка, упоминание или реакция. Оригинал доступен ниже."
        assert ai.summarize(["version 2"]) == "Вышла версия 2."
    assert len(requests) == 2 and "формату JSON" in requests[1]


def test_excerpts_are_contiguous_and_keep_source_boundaries():
    first = 'Слово\u00a0с переносом\nи «кавычками». ' * 13
    identifier = "9" * 210
    texts = [first, "👋", identifier + " подписей"]
    excerpts = evidence_excerpts(texts)
    assert "1.2" in excerpts and "2.1" not in excerpts
    assert excerpts["3.1"] == identifier
    for key, quote in excerpts.items():
        index = int(key.partition(".")[0]) - 1
        assert quote in normalized(texts[index])
    many = [str(n) for n in range(3 * MAX_BATCH_ITEMS)]
    groups = list(batches(many))
    assert max(map(len, groups)) <= MAX_BATCH_ITEMS
    assert [text for group in groups for text in group] == many


@pytest.mark.parametrize("sentence,refs", [
    ("Версия 2.", []), ("Версия 2.", ["9.9"]), ("Версия 2.", "1.1"),
    ("Версия 2.", [True]), ("Версия 2.", ["1.1"] * 5),
    ("Срок — 2027 год.", ["1.1"]), ("Version 2.", ["1.1"]),
])
def test_repair_rejects_unknown_references_or_unsupported_facts(sentence, refs):
    sources = ["Version 2.", "The deadline is in 2027."]
    reply = model_response(sentence, refs)["message"]["content"]
    with pytest.raises(InvalidSummary):
        validate(reply, sources, excerpts=evidence_excerpts(sources))


def test_translated_evidence_recovers_in_same_issue_without_reading_x_again(repo, pending, delivery):
    first = ("The release is ready. " * 9) + "The release number is 12."
    with repo.db.connect(write=True) as db:
        db.execute("UPDATE tweets SET text=? WHERE x_tweet_id='101'", (first,))
        db.execute("UPDATE tweets SET text=? WHERE x_tweet_id='102'", ("The next update is due in 2027.",))
    requests = []
    def respond(req):
        cfg = json.loads(req.content)
        data = json.loads(cfg["messages"][1]["content"])
        requests.append(data)
        field = cfg["format"]["properties"]["sentences"]["items"]["properties"]["evidence"]
        if isinstance(data, dict):
            assert "News 20" not in str(data)
            assert field["items"]["enum"] == list(data)
            selected = [k for k, quote in data.items() if "12" in quote or "2027" in quote]
            assert len(selected) == 2
            result = model_response("Вышла версия 12, следующее обновление ожидается в 2027 году.", selected)
        elif first in data:
            # Old failure: the model translated the hidden quote along with the summary.
            result = model_response("Вышла новая версия.", "Новая версия готова")
        else:
            assert field["type"] == "string" and first not in data
            result = model_response("Опубликованы новости второго автора.", "News 20/1")
        return httpx.Response(200, json=result)

    timeline = Mock()
    timeline.collect.return_value = []
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        digest = DigestService(repo, timeline, Summarizer(http, "http://ollama", "model"), delivery)
        assert digest.run().status == "sent"
        timeline.collect.assert_called_once()
        assert len(requests) == 3 and not repo.pending() and not repo.settings()["summary_retry_at"]
        html = delivery.bot.send_message.call_args.args[1]
        assert "версия 12" in html and "2027" in html and "Новых твитов: 4. Авторов: 2." in html
        for post in pending:
            assert f"/status/{post['x_tweet_id']}" in html
        assert digest.run().status == "empty" and len(repo.history()) == 1
        assert delivery.bot.send_message.call_count == 1
