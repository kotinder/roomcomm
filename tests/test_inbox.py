"""Inbox ("did anyone look for me?"): watermarks, digest, mentions.

Same in-memory-DB pattern as test_auth.py. Quota mode is irrelevant here
except for the empty-inbox idle-poll test, which flips READ_EMPTY_LIMIT.
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, delete
from sqlalchemy.pool import StaticPool

import app.models as models
import app.main as _main
import app.quota as quota
from app.main import app as fastapi_app
from app.database import get_session

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
SQLModel.metadata.create_all(engine)


def _override():
    with Session(engine) as s:
        yield s


client = TestClient(fastapi_app)

_main.ROOM_CREATE_LIMIT = 10_000
_main.KEY_CREATE_LIMIT = 10_000


@pytest.fixture(autouse=True)
def _inbox_test_env():
    prev = fastapi_app.dependency_overrides.get(get_session)
    fastapi_app.dependency_overrides[get_session] = _override
    quota.QUOTA_MODE = "hard"
    prev_limit = quota.READ_EMPTY_LIMIT
    yield
    quota.READ_EMPTY_LIMIT = prev_limit
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev
    with Session(engine) as s:
        s.exec(delete(models.UsageCounter))
        s.exec(delete(models.RoomSeen))
        s.commit()


def _new_key(agent_id="tester"):
    r = client.post("/api/keys", json={"agent_id": agent_id})
    assert r.status_code == 201
    return r.json()


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def _new_room():
    r = client.post("/api/rooms", json={"description": "t"})
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def _post(room, agent_id, text, headers=None):
    r = client.post(
        f"/api/rooms/{room}/messages",
        json={"agent_id": agent_id, "text": text},
        headers=headers or {},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _inbox(key):
    r = client.get("/api/me/inbox", headers=_bearer(key))
    assert r.status_code == 200, r.text
    return r.json()


# ---------- basics ----------

def test_inbox_requires_key():
    r = client.get("/api/me/inbox")
    assert r.status_code == 401


def test_post_then_new_messages_counted():
    k = _new_key("alice-agent")["key"]
    room = _new_room()
    _post(room, "alice-agent", "hi", headers=_bearer(k))
    # own post advances the watermark → nothing new yet
    data = _inbox(k)
    assert [r["uuid"] for r in data["rooms"]] == [room]
    assert data["rooms"][0]["new_messages"] == 0
    # someone else replies (anonymously)
    _post(room, "bob", "hello alice")
    _post(room, "bob", "you there?")
    data = _inbox(k)
    assert data["rooms"][0]["new_messages"] == 2
    assert data["rooms"][0]["last_from"] == "bob"
    assert data["rooms"][0]["last_msg_id"] > 0


def test_keyed_read_advances_watermark():
    k = _new_key("carol-agent")["key"]
    room = _new_room()
    _post(room, "carol-agent", "start", headers=_bearer(k))
    _post(room, "dave", "reply 1")
    _post(room, "dave", "reply 2")
    assert _inbox(k)["rooms"][0]["new_messages"] == 2
    # reading WITH the key marks the room read
    r = client.get(f"/api/rooms/{room}/messages", headers=_bearer(k))
    assert r.status_code == 200 and len(r.json()["messages"]) == 3
    assert _inbox(k)["rooms"][0]["new_messages"] == 0
    # anonymous read must NOT touch someone's watermark
    _post(room, "dave", "reply 3")
    client.get(f"/api/rooms/{room}/messages")
    assert _inbox(k)["rooms"][0]["new_messages"] == 1


def test_inbox_itself_is_side_effect_free():
    k = _new_key("erin-agent")["key"]
    room = _new_room()
    _post(room, "erin-agent", "start", headers=_bearer(k))
    _post(room, "frank", "ping")
    assert _inbox(k)["rooms"][0]["new_messages"] == 1
    assert _inbox(k)["rooms"][0]["new_messages"] == 1  # unchanged


# ---------- mentions ----------

def test_mention_in_a_foreign_room_is_not_shown():
    # The key is the boundary, not the name: anyone can take any agent_id, so
    # a mention in a room the key never joined would hand a private room's
    # UUID (= access) and text to a stranger who picked that name (01.10.2026).
    k = _new_key("grace-agent")["key"]
    other_room = _new_room()  # grace never joined it
    _post(other_room, "heidi", "let's ask grace-agent to join us here")
    data = _inbox(k)
    assert data["rooms"] == [] and data["mentions"] == []


def test_mention_found_in_a_room_the_key_joined():
    k = _new_key("grace2-agent")["key"]
    room = _new_room()
    _post(room, "grace2-agent", "hi", headers=_bearer(k))
    _post(room, "heidi", "grace2-agent, join us here")
    ms = _inbox(k)["mentions"]
    assert len(ms) == 1
    assert ms[0]["room_uuid"] == room and ms[0]["by"] == "heidi"
    assert "grace2-agent" in ms[0]["text"]


def test_own_messages_and_read_ones_are_not_mentions():
    k = _new_key("ivan-agent")["key"]
    room = _new_room()
    # own post naming yourself — not a mention
    _post(room, "ivan-agent", "ivan-agent was here", headers=_bearer(k))
    assert _inbox(k)["mentions"] == []
    # someone mentions ivan; visible until the room is read with the key
    _post(room, "judy", "ivan-agent: see this")
    assert len(_inbox(k)["mentions"]) == 1
    client.get(f"/api/rooms/{room}/messages", headers=_bearer(k))
    assert _inbox(k)["mentions"] == []


def test_short_agent_id_yields_no_mentions():
    k = _new_key("ai")["key"]  # < 3 chars — substring search would be noise
    room = _new_room()
    _post(room, "kim", "ai is everywhere these days")
    assert _inbox(k)["mentions"] == []


# ---------- idle-poll economics ----------

def test_empty_inbox_counts_as_idle_poll():
    quota.READ_EMPTY_LIMIT = 2
    k = _new_key("lazy-agent")["key"]
    # no rooms, no mentions → every poll is an idle poll
    for _ in range(2):
        assert client.get("/api/me/inbox", headers=_bearer(k)).status_code == 200
    r = client.get("/api/me/inbox", headers=_bearer(k))
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_inbox_with_news_is_never_throttled():
    quota.READ_EMPTY_LIMIT = 1
    k = _new_key("busy-agent")["key"]
    room = _new_room()
    _post(room, "busy-agent", "start", headers=_bearer(k))
    _post(room, "mallory", "news!")
    for _ in range(3):
        assert client.get("/api/me/inbox", headers=_bearer(k)).status_code == 200
