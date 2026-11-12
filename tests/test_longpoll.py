"""Long-poll on GET /api/rooms/{uuid}/messages and GET /api/me/inbox (`wait`).

A file database, not the in-memory StaticPool the other suites use: here a
held request and a post run at the same time, and on one shared connection
the waiter's end-of-check rollback could roll back the poster's transaction.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine, select

import app.longpoll as longpoll
import app.main as _main
import app.models as models
import app.quota as quota
from app.database import get_session
from app.main import app as fastapi_app

client = TestClient(fastapi_app)


@pytest.fixture(autouse=True)
def _file_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'lp.db'}",
                           connect_args={"check_same_thread": False, "timeout": 10})

    @event.listens_for(engine, "connect")
    def _wal(conn, _):
        conn.execute("PRAGMA journal_mode=WAL")

    SQLModel.metadata.create_all(engine)

    def _override():
        with Session(engine, expire_on_commit=False) as s:
            yield s

    prev = fastapi_app.dependency_overrides.get(get_session)
    fastapi_app.dependency_overrides[get_session] = _override
    monkeypatch.setattr(_main, "ROOM_CREATE_LIMIT", 10_000)
    monkeypatch.setattr(_main, "KEY_CREATE_LIMIT", 10_000)
    monkeypatch.setattr(longpoll, "TICK", 0.05)
    yield engine
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev
    else:
        fastapi_app.dependency_overrides.pop(get_session, None)
    engine.dispose()


def _key(agent_id):
    r = client.post("/api/keys", json={"agent_id": agent_id})
    assert r.status_code == 201, r.text
    return r.json()["key"]


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def _room():
    r = client.post("/api/rooms", json={"description": "lp"})
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def _post(room, agent_id, text, headers=None):
    r = client.post(f"/api/rooms/{room}/messages",
                    json={"agent_id": agent_id, "text": text}, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _in_background(fn):
    """Run fn() in a thread; return (thread, box) where box gets
    (response, elapsed)."""
    box = {}

    def run():
        t0 = time.monotonic()
        box["r"] = fn()
        box["dt"] = time.monotonic() - t0
    th = threading.Thread(target=run)
    th.start()
    return th, box


def _idle_polls(engine, agent_id):
    with Session(engine) as s:
        kid = s.exec(select(models.AgentKey.id).where(
            models.AgentKey.agent_id == agent_id)).one()
        day = models.utcnow().strftime("%Y-%m-%d")
        return quota._stored_count(s, f"key:{kid}", day, "read_empty")


# ---------- room ----------

def test_news_already_there_answers_at_once():
    room = _room()
    first = _post(room, "a", "one")
    _post(room, "a", "two")
    t0 = time.monotonic()
    r = client.get(f"/api/rooms/{room}/messages", params={"since": first, "wait": 20})
    assert r.status_code == 200
    assert [m["text"] for m in r.json()["messages"]] == ["two"]
    assert time.monotonic() - t0 < 2


def test_held_read_returns_the_message_posted_meanwhile():
    room = _room()
    last = _post(room, "a", "hello")
    th, box = _in_background(lambda: client.get(
        f"/api/rooms/{room}/messages", params={"since": last, "wait": 20}))
    time.sleep(0.5)
    assert longpoll.held() == 1
    _post(room, "b", "reply")
    th.join(10)
    assert box["r"].status_code == 200
    assert [m["text"] for m in box["r"].json()["messages"]] == ["reply"]
    assert 0.4 < box["dt"] < 3  # woke on the post, not at the 20 s deadline
    assert longpoll.held() == 0


def test_quiet_room_answers_empty_when_the_wait_is_up():
    room = _room()
    last = _post(room, "a", "hello")
    t0 = time.monotonic()
    r = client.get(f"/api/rooms/{room}/messages", params={"since": last, "wait": 1})
    assert r.status_code == 200 and r.json()["messages"] == []
    assert 0.9 < time.monotonic() - t0 < 3


def test_missing_and_bad_wait_are_refused_without_holding():
    room = _room()
    t0 = time.monotonic()
    r = client.get("/api/rooms/00000000-0000-4000-8000-000000000000/messages",
                   params={"wait": 20})
    assert r.status_code == 404
    assert time.monotonic() - t0 < 2
    assert client.get(f"/api/rooms/{room}/messages", params={"wait": 31}).status_code == 400


# ---------- metering ----------

def test_long_wait_is_not_an_idle_poll_short_one_is(_file_db, monkeypatch):
    key = _key("lp-meter")
    room = _room()
    last = _post(room, "lp-meter", "hi", headers=_bearer(key))
    monkeypatch.setattr(longpoll, "FREE_WAIT", 1)
    client.get(f"/api/rooms/{room}/messages",
               params={"since": last, "wait": 1}, headers=_bearer(key))
    assert _idle_polls(_file_db, "lp-meter") == 0
    monkeypatch.setattr(longpoll, "FREE_WAIT", 10)
    client.get(f"/api/rooms/{room}/messages",
               params={"since": last, "wait": 1}, headers=_bearer(key))
    assert _idle_polls(_file_db, "lp-meter") == 1
    client.get(f"/api/rooms/{room}/messages",
               params={"since": last}, headers=_bearer(key))
    assert _idle_polls(_file_db, "lp-meter") == 2


def test_full_worker_answers_at_once_and_meters(_file_db, monkeypatch):
    key = _key("lp-full")
    room = _room()
    last = _post(room, "lp-full", "hi", headers=_bearer(key))
    monkeypatch.setattr(longpoll, "MAX_WAITERS", 0)
    t0 = time.monotonic()
    r = client.get(f"/api/rooms/{room}/messages",
                   params={"since": last, "wait": 20}, headers=_bearer(key))
    assert r.status_code == 200 and r.json()["messages"] == []
    assert time.monotonic() - t0 < 2
    assert _idle_polls(_file_db, "lp-full") == 1


# ---------- inbox ----------

def test_held_inbox_wakes_on_a_reply():
    key = _key("lp-inbox")
    room = _room()
    _post(room, "lp-inbox", "anyone?", headers=_bearer(key))
    th, box = _in_background(lambda: client.get(
        "/api/me/inbox", params={"wait": 20}, headers=_bearer(key)))
    time.sleep(0.5)
    _post(room, "dealer", "here")
    th.join(10)
    assert box["r"].status_code == 200
    rooms = box["r"].json()["rooms"]
    assert [(r["uuid"], r["new_messages"]) for r in rooms] == [(room, 1)]
    assert 0.4 < box["dt"] < 3


def test_held_inbox_ignores_other_rooms_and_own_posts():
    key = _key("lp-quiet")
    mine, other = _room(), _room()
    _post(mine, "lp-quiet", "start", headers=_bearer(key))
    th, box = _in_background(lambda: client.get(
        "/api/me/inbox", params={"wait": 2}, headers=_bearer(key)))
    time.sleep(0.3)
    _post(other, "stranger", "not for you")
    _post(mine, "lp-quiet", "talking to myself", headers=_bearer(key))
    th.join(10)
    assert box["r"].status_code == 200
    assert all(r["new_messages"] == 0 for r in box["r"].json()["rooms"])
    assert box["dt"] > 1.8  # held the whole time


def test_held_inbox_wakes_on_a_knock(_file_db):
    caller, target = _key("lp-caller"), _key("lp-target")
    meet = _room()
    _post(meet, "lp-caller", "hi", headers=_bearer(caller))
    _post(meet, "lp-target", "hi", headers=_bearer(target))
    with Session(_file_db) as s:
        for k in s.exec(select(models.AgentKey).where(
                models.AgentKey.agent_id.in_(["lp-caller", "lp-target"]))).all():
            k.tier = "verified"
            s.add(k)
        s.commit()
    ref = next(m["key_ref"] for m in client.get(f"/api/rooms/{meet}/messages").json()["messages"]
               if m["agent_id"] == "lp-target")
    # Read the meeting room so only the knock is news.
    client.get(f"/api/rooms/{meet}/messages", headers=_bearer(target))
    th, box = _in_background(lambda: client.get(
        "/api/me/inbox", params={"wait": 20}, headers=_bearer(target)))
    time.sleep(0.5)
    r = client.post("/api/knocks", headers=_bearer(caller),
                    json={"to_ref": ref, "agent_id": "lp-caller", "text": "book a slot?"})
    assert r.status_code == 201, r.text
    th.join(10)
    assert box["r"].status_code == 200
    assert [k["room_uuid"] for k in box["r"].json()["knocks"]] == [r.json()["room_uuid"]]
    assert box["dt"] < 3


# ---------- review 12.11.2026 ----------

def test_held_requests_do_not_shed_other_traffic(monkeypatch):
    """A held long-poll gives its overload-guard slot back: with the guard at
    one slot, a held read used to turn every other request into 503."""
    monkeypatch.setattr(_main, "MAX_INFLIGHT", 1)
    room = _room()
    last = _post(room, "a", "hello")
    th, box = _in_background(lambda: client.get(
        f"/api/rooms/{room}/messages", params={"since": last, "wait": 3}))
    time.sleep(0.5)
    assert longpoll.held() == 1
    r = client.get(f"/api/rooms/{room}")
    assert r.status_code == 200, r.text
    th.join(10)
    assert box["r"].status_code == 200
    assert _main._inflight == 0


def test_throttled_caller_is_refused_before_a_short_wait(_file_db, monkeypatch):
    key = _key("lp-throttled")
    room = _room()
    last = _post(room, "lp-throttled", "hi", headers=_bearer(key))
    monkeypatch.setattr(quota, "READ_EMPTY_LIMIT", 1)
    client.get(f"/api/rooms/{room}/messages", params={"since": last}, headers=_bearer(key))
    t0 = time.monotonic()
    r = client.get(f"/api/rooms/{room}/messages",
                   params={"since": last, "wait": 5}, headers=_bearer(key))
    assert r.status_code == 429
    assert "empty_poll_throttled" in r.json()["detail"]
    assert time.monotonic() - t0 < 2


def test_a_gone_client_frees_its_place(_file_db):
    """wait_for stops at the first tick after the client went away."""
    import asyncio

    calls = []

    async def gone():
        calls.append(1)
        return True

    with Session(_file_db) as s:
        waited = asyncio.run(longpoll.wait_for(s, lambda _s: False, 20, gone))
    assert waited is not None and waited < 1
    assert calls and longpoll.held() == 0


# ---------- second review 12.11.2026 ----------

def test_inbox_cursor_waits_past_stale_unread_news():
    """Old unread news used to end every inbox wait at once, turning
    `wait=30` into a tight loop. With since=<cursor> it waits for news newer
    than the cursor."""
    key = _key("lp-cursor")
    stale, live = _room(), _room()
    for room in (stale, live):
        _post(room, "lp-cursor", "hi", headers=_bearer(key))
    _post(stale, "someone", "old news, never read")
    first = client.get("/api/me/inbox", headers=_bearer(key)).json()
    assert any(r["new_messages"] for r in first["rooms"])  # stale news present
    cursor = first["cursor"]
    assert cursor > 0
    t0 = time.monotonic()
    r = client.get("/api/me/inbox", params={"wait": 1, "since": cursor}, headers=_bearer(key))
    assert r.status_code == 200 and time.monotonic() - t0 > 0.9  # held despite stale news
    th, box = _in_background(lambda: client.get(
        "/api/me/inbox", params={"wait": 20, "since": cursor}, headers=_bearer(key)))
    time.sleep(0.5)
    _post(live, "dealer", "fresh")
    th.join(10)
    assert box["r"].status_code == 200 and box["dt"] < 3
    rooms = {r["uuid"]: r["new_messages"] for r in box["r"].json()["rooms"]}
    assert rooms[live] == 1
    assert box["r"].json()["cursor"] > cursor


def test_without_since_unread_news_ends_the_wait_at_once():
    key = _key("lp-nocursor")
    room = _room()
    _post(room, "lp-nocursor", "hi", headers=_bearer(key))
    _post(room, "someone", "unread")
    t0 = time.monotonic()
    r = client.get("/api/me/inbox", params={"wait": 20}, headers=_bearer(key))
    assert r.status_code == 200 and time.monotonic() - t0 < 2


def test_a_long_wait_that_did_not_really_wait_is_metered():
    """A client asking wait=30 and leaving after 50 ms must not get an
    unmetered idle poll (review 12.11.2026)."""
    from app.main import _meter_after
    assert _meter_after(False, None) is True          # worker full
    assert _meter_after(False, 0.05) is True          # client left early
    assert _meter_after(False, longpoll.FREE_WAIT) is False
    assert _meter_after(True, 0.05) is False          # short: metered before the hold


def test_cancelling_twice_still_waits_for_the_check_thread(_file_db):
    """The session must not be closed while a check thread is in it, however
    often the request is cancelled."""
    import asyncio

    finished = []

    def slow_check(_s):
        time.sleep(0.4)
        finished.append(time.monotonic())
        return False

    async def scenario():
        with Session(_file_db) as s:
            task = asyncio.ensure_future(longpoll.check_now(s, slow_check))
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return time.monotonic()

    returned = asyncio.run(scenario())
    assert finished and finished[0] <= returned


# ---------- tests-axis review 12.11.2026: gaps mutations slipped through ----------

def test_throttled_caller_is_refused_before_a_short_inbox_wait(monkeypatch):
    key = _key("lp-inbox-throttled")
    room = _room()
    _post(room, "lp-inbox-throttled", "hi", headers=_bearer(key))
    monkeypatch.setattr(quota, "READ_EMPTY_LIMIT", 1)
    client.get("/api/me/inbox", headers=_bearer(key))
    t0 = time.monotonic()
    r = client.get("/api/me/inbox", params={"wait": 5}, headers=_bearer(key))
    assert r.status_code == 429
    assert "empty_poll_throttled" in r.json()["detail"]
    assert time.monotonic() - t0 < 2


def test_held_inbox_wakes_a_key_that_only_read_the_room():
    key = _key("lp-reader")
    room = _room()
    last = _post(room, "someone", "hello")
    client.get(f"/api/rooms/{room}/messages", headers=_bearer(key))  # read, never post
    th, box = _in_background(lambda: client.get(
        "/api/me/inbox", params={"wait": 20, "since": last}, headers=_bearer(key)))
    time.sleep(0.5)
    _post(room, "someone", "news")
    th.join(10)
    assert box["r"].status_code == 200 and box["dt"] < 3
    assert [(r["uuid"], r["new_messages"]) for r in box["r"].json()["rooms"]] == [(room, 1)]


def test_news_landing_during_the_first_inbox_build_ends_the_wait(monkeypatch):
    """The wait baseline is the newest id read *before* the build: a message
    committed while the inbox is being built must not wait out the timer."""
    key = _key("lp-race")
    room = _room()
    _post(room, "lp-race", "hi", headers=_bearer(key))
    real = _main.inbox.build_inbox
    fired = []

    def build(session, k):
        data = real(session, k)
        if not fired:
            fired.append(1)
            th = threading.Thread(target=_post, args=(room, "dealer", "mid-build"))
            th.start()
            th.join(10)
        return data
    monkeypatch.setattr(_main.inbox, "build_inbox", build)
    t0 = time.monotonic()
    r = client.get("/api/me/inbox", params={"wait": 20}, headers=_bearer(key))
    assert r.status_code == 200 and time.monotonic() - t0 < 3
    assert [(x["uuid"], x["new_messages"]) for x in r.json()["rooms"]] == [(room, 1)]


@pytest.mark.parametrize("path", ["room", "inbox"])
def test_full_worker_meters_a_short_wait_once(_file_db, monkeypatch, path):
    agent = f"lp-full-short-{path}"
    key = _key(agent)
    room = _room()
    last = _post(room, agent, "hi", headers=_bearer(key))
    monkeypatch.setattr(longpoll, "MAX_WAITERS", 0)
    url = f"/api/rooms/{room}/messages" if path == "room" else "/api/me/inbox"
    params = {"since": last, "wait": 1}
    r = client.get(url, params=params, headers=_bearer(key))
    assert r.status_code == 200
    assert _idle_polls(_file_db, agent) == 1
