"""Cross-worker state (app/shared.py): Redis-backed limits, once-a-day, locks,
and the fallback to per-process state when Redis is absent or down."""
import pytest

from app import shared


@pytest.fixture
def fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    pytest.importorskip("lupa")  # Lua scripting in fakeredis
    client = fakeredis.FakeRedis()
    monkeypatch.setattr(shared, "REDIS_URL", "redis://fake")
    monkeypatch.setattr(shared, "_client", client)
    monkeypatch.setattr(shared, "_hit_script", client.register_script(shared._HIT_LUA))
    monkeypatch.setattr(shared, "_down_until", 0.0)
    return client


def test_without_redis_everything_falls_back(monkeypatch):
    monkeypatch.setattr(shared, "REDIS_URL", "")
    assert shared.rate_hit("x", "1.2.3.4", 1, 60) is None
    assert shared.rate_count("x", "1.2.3.4", 60) is None
    assert shared.rate_add("x", "1.2.3.4", 60) is False
    assert shared.first_time("k", 60) is None
    assert shared.try_lock("l", 60) is None


def test_rate_hit_admits_up_to_limit_then_refuses(fake_redis):
    assert [shared.rate_hit("msg", "ip", 3, 60) for _ in range(3)] == [0, 0, 0]
    retry = shared.rate_hit("msg", "ip", 3, 60)
    assert retry is not None and 0 < retry <= 61
    assert shared.rate_hit("msg", "other-ip", 3, 60) == 0  # per subject


def test_rate_count_and_add(fake_redis):
    assert shared.rate_count("admin_fail", "ip", 3600) == 0
    assert shared.rate_add("admin_fail", "ip", 3600)
    assert shared.rate_add("admin_fail", "ip", 3600)
    assert shared.rate_count("admin_fail", "ip", 3600) == 2


def test_first_time(fake_redis):
    assert shared.first_time("limit:day:key:1:msg", 60) is True
    assert shared.first_time("limit:day:key:1:msg", 60) is False


def test_lock_is_exclusive_and_only_owner_unlocks(fake_redis):
    token = shared.try_lock("refresh:r", 60)
    assert token
    assert shared.try_lock("refresh:r", 60) == ""
    shared.unlock("refresh:r", "someone-else")
    assert shared.try_lock("refresh:r", 60) == ""
    shared.unlock("refresh:r", token)
    assert shared.try_lock("refresh:r", 60)


def test_redis_failure_marks_down_and_falls_back(monkeypatch):
    class Broken:
        def set(self, *a, **k):
            raise ConnectionError("boom")
    monkeypatch.setattr(shared, "REDIS_URL", "redis://broken")
    monkeypatch.setattr(shared, "_client", Broken())
    monkeypatch.setattr(shared, "_down_until", 0.0)
    assert shared.first_time("k", 60) is None
    assert not shared.available()  # marked down: no second timeout per request


def test_app_rate_limit_is_shared_through_redis(fake_redis):
    """The A2A post limiter (_check_msg_rate) counts in Redis: what one worker
    admitted, another sees. Simulated by filling the shared bucket directly."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from app import main
    req = Request({"type": "http", "headers": [(b"x-real-ip", b"9.9.9.9")],
                   "client": ("9.9.9.9", 1)})
    for _ in range(main.MSG_POST_LIMIT):
        assert shared.rate_hit("messages_posted", "9.9.9.9",
                               main.MSG_POST_LIMIT, main.MSG_POST_WINDOW) == 0
    with pytest.raises(HTTPException) as e:
        main._check_msg_rate(req)
    assert e.value.status_code == 429
