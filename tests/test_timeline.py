from datetime import timedelta

import httpx
import pytest

from app.database.connection import Database
from app.database.models import Profile, UserError, stamp
from app.database.repository import Repository
from app.services.author_service import Authors
from app.services.tweet_service import Timeline


def post(xid, created="2026-11-21T09:12:00Z", text="publication"):
    return {"id": str(xid), "created_at": created, "text": text}


def page(posts=(), token=None):
    meta = {"result_count": len(posts)}
    if token:
        meta["next_token"] = token
    return httpx.Response(200, json={"data": list(posts), "meta": meta})


@pytest.mark.parametrize("value", ["@Alice", "Alice", "https://x.com/Alice",
                         "https://twitter.com/Alice/?s=20", "https://www.x.com/Alice"])
def test_lookup_cache_and_confirmation(repo, client, value):
    requests = []
    def handle(req):
        requests.append(req)
        return httpx.Response(200, json={"data": {"id": "100", "username": "Alice", "name": "A"}})
    service = Authors(repo, client(handle))
    profile = service.lookup(value)
    assert requests[0].url.path == "/2/users/by/username/Alice"
    assert requests[0].headers["Authorization"] == "Bearer test-only"
    repo.add(profile)
    with pytest.raises(UserError, match="добавлен"):
        service.lookup("@alice")
    assert len(requests) == 1
    with pytest.raises(UserError, match="X User ID"):
        service.lookup("@newname")


@pytest.mark.parametrize("s", ["https://x.com.evil.org/a", "https://user@x.com/a",
                               "https://x.com/a/status/22", "@a b", "@@a", "а", "https://[bad"])
def test_invalid_handles(s):
    with pytest.raises(UserError):
        Authors.normalize(s)


def test_precise_boundary_resume_pagination_since(repo, author, clock, client, budget):
    clock.step(hours=1)
    calls = []
    def first(req):
        calls.append(dict(req.url.params))
        if req.url.params.get("pagination_token"):
            return httpx.Response(503)
        return page([post(105), post(104)], "page2")
    assert Timeline(repo, client(first)).collect()
    assert repo.author("Alice")["last_tweet_id"] is None
    assert not repo.pending()
    with repo.db.connect() as db:
        assert db.execute("SELECT count(*) FROM tweets WHERE processing_status='staged'").fetchone()[0] == 2
    again = Repository(Database(repo.db.path), 42, clock=clock)
    clock.step(hours=2)
    def second(req):
        calls.append(dict(req.url.params))
        if req.url.params.get("since_id"):
            assert req.url.params["since_id"] == "105"
            return page()
        return page([post(104), post(103, "2026-11-21T09:00:00.451Z"),
                     post(102, author["tracking_started_at"]), post(101, "2026-11-20T00:00:00Z"),
                     post(111, "2026-11-21T12:00:00Z")])
    Timeline(again, client(second)).sync(author["id"])
    assert calls[-2]["pagination_token"] == "page2"
    assert calls[0]["end_time"] == calls[-2]["end_time"] < calls[-1]["end_time"]
    assert calls[0]["start_time"] == "2026-11-21T09:00:00Z"
    assert calls[0]["exclude"] == "retweets,replies" and "created_at" in calls[0]["tweet.fields"]
    assert again.author("Alice")["last_tweet_id"] == "105"
    assert {r["x_tweet_id"] for r in again.pending()} == {"103", "104", "105"}
    clock.step(minutes=1)
    def third(req):
        assert req.url.params["since_id"] == "105" and "start_time" not in req.url.params
        return page([post(108, "2026-11-21T10:01:00Z"), post(105)])
    Timeline(again, client(third)).sync(author["id"])
    assert len(again.pending()) == 4
    assert budget.stats()[:3] == (5, 0, 9)


def test_empty_expired_and_cyclic_pages(repo, author, clock, client):
    calls = []
    def pages(req):
        calls.append(req.url.params.get("pagination_token"))
        return [page([post(11)], "old"),
                httpx.Response(400, json={"detail": "invalid pagination_token"}),
                page([post(11)], "new"), page([post(10)])][len(calls)-1]
    clock.step(hours=1)
    Timeline(repo, client(pages)).sync(author["id"])
    assert calls == [None, "old", None, "new"]
    assert len(repo.pending()) == 2
    clock.step(hours=1)
    cycle = Timeline(repo, client(lambda r: page([post(12)], "loop")))
    assert "повторил" in cycle.collect()[0]
    assert repo.author("Alice")["last_tweet_id"] == "11"
    repo.change_author("Alice", "pause")
    repo.change_author("Alice", "resume")
    clock.step(minutes=2)
    empty = Timeline(repo, client(lambda r: httpx.Response(200, json={"meta": {"result_count": 0}})))
    empty.sync(author["id"])
    assert repo.author("Alice")["last_tweet_id"] is None
    assert empty.window(author["id"]) is None


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"data": [post(1)], "meta": {"result_count": 2}}),
    httpx.Response(200, json={"meta": {}, "errors": [{"detail": "partial"}]}),
    httpx.Response(200, json={"data": [{"id": "bad"}], "meta": {"result_count": 1}}),
    httpx.Response(200, json={"data": [post(12)], "errors": [{"title": "partial"}]}),
    httpx.Response(200, content=b"not JSON"), httpx.Response(401), httpx.Response(402),
    httpx.Response(403), httpx.Response(404), httpx.Response(500)])
def test_errors_do_not_advance(repo, author, clock, client, response):
    clock.step(hours=1)
    assert Timeline(repo, client(lambda r: response)).collect()
    assert not repo.pending()
    assert repo.author("Alice")["last_tweet_id"] is None


def test_429_and_reservation(repo, author, clock, client, budget):
    repo.budget(30_000)
    clock.step(hours=1)
    def limited(req):
        assert req.url.params["max_results"] == "6"
        return httpx.Response(429, headers={"Retry-After": "90","x-rate-limit-reset": "broken"})
    sync = Timeline(repo, client(limited))
    assert "429" in sync.collect()[0]
    assert "Лимит" in sync.collect()[0]
    assert budget.stats() == (1, 0, 0, 0)
    clock.step(seconds=91)
    assert "429" in sync.collect()[0]
    ticket = budget.reserve("profile", 1)
    assert budget.stats()[-1] == 10000
    budget.settle(ticket, 1)
    repo.budget(4000)
    clock.step(hours=1)
    assert "бюджета" in sync.collect()[0]
    clock.step(days=31)
    assert budget.stats() == (0, 0, 0, 0)


def test_network_unknown_and_concurrent_budget(repo, client, budget):
    import concurrent.futures
    repo.budget(25_000)
    def broken(request):
        raise httpx.ReadTimeout("test")
    with pytest.raises(UserError, match="Резерв"):
        client(broken).profile("name")
    assert budget.stats() == (1, 0, 0, 10_000)
    def reserve():
        try:
            return budget.reserve("profile", 1).id
        except UserError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        assert sum(v is not None for v in pool.map(lambda i: reserve(), range(3))) == 1


def test_profile_and_wire_compatibility(repo, author, clock, client):
    for data in [{"id": "wrong", "username": "a", "name": "x"},
                 {"id": "1", "username": "a", "name": "x", "protected": True}]:
        with pytest.raises(UserError):
            client(lambda r: httpx.Response(200, json={"data": data})).profile("a")
    clock.step(hours=1)
    def field(req):
        if "tweet.fields" in req.url.params:
            return httpx.Response(400, json={"detail": "unknown parameter tweet.fields"})
        assert "created_at" in req.url.params["post.fields"]
        row = dict(post(1), note_post={"text": "полный текст"})
        return page([row])
    assert not Timeline(repo, client(field)).collect()
    assert repo.pending()[0]["text"] == "полный текст"


def test_limit_and_author_removed_mid_request(repo, author, clock, client):
    clock.step(hours=1)
    sync = Timeline(repo, client(lambda r: page()))
    a, state = sync.window(author["id"])
    full = dict(state, seen_count=800)
    with pytest.raises(UserError, match="800"):
        sync.save_page(a, full, [], None, 100)
    repo.change_author("Alice", "remove")
    assert not sync.save_page(a, state, [], None, 100)
