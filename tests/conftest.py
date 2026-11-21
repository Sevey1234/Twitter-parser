from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app.database.connection import Database
from app.database.models import Profile
from app.database.repository import Repository
from app.services.budget_service import Budget
from app.services.delivery import Delivery
from app.services.tweet_service import Timeline
from app.services.twitter_client import TwitterClient


def pytest_configure(config):
    # Even an explicit --cov-fail-under=80 must enforce the course's strict threshold.
    threshold = config.getoption("cov_fail_under")
    floor = max(threshold or 0, 81)
    config.option.cov_fail_under = floor
    plugin = config.pluginmanager.getplugin("_cov")
    if plugin:
        plugin.options.cov_fail_under = floor


class Clock:
    def __init__(self):
        self.time = datetime(2026, 11, 21, 9, 0, 0, 450000, timezone.utc)

    def __call__(self):
        return self.time

    def step(self, **kw):
        self.time += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def repo(tmp_path, clock):
    return Repository(Database(tmp_path / "digest.db"), 42, clock=clock)


@pytest.fixture
def budget(repo):
    return Budget(repo, 5000, 10000)


@pytest.fixture
def client(budget):
    clients = []
    def factory(handler):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(http)
        return TwitterClient(http, "test-only", budget)
    yield factory
    for http in clients:
        http.close()


@pytest.fixture
def author(repo):
    repo.add(Profile("10", "Alice", "Alice"))
    return repo.author("alice")


@pytest.fixture
def delivery(repo):
    bot = Mock()
    bot.send_message.return_value = SimpleNamespace(message_id=12)
    return Delivery(repo, bot)


@pytest.fixture
def pending(repo, clock, client):
    repo.add(Profile("10", "Alice", "Alice"))
    repo.add(Profile("20", "Bob", "Bob"))
    clock.step(hours=1)
    def timeline(request):
        aid = request.url.path.split("/")[-2]
        posts = [{"id": aid + str(i), "text": f"News {aid}/{i}", "created_at": "2026-11-21T09:10:00Z"}
                 for i in (1, 2)]
        return httpx.Response(200, json={"data": posts, "meta": {"result_count": len(posts)}})
    sync = Timeline(repo, client(timeline))
    assert sync.collect() == []
    return repo.pending()
