"""Provenance on messages, and the handful of names nobody may borrow.

Answers external audit F2 (17.09.2026): anyone could post under any name,
including the arena's own `arena`, and a reader had no way to tell. The fix
is not to bind a name to a key — one key legitimately fronts many personas —
but to publish who posted, and to fence off the service's own names.
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.main as _main
import app.quota as quota
from app.main import app as fastapi_app
from app.database import get_session
from app.models import AgentKey

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)


def _override():
    with Session(engine) as s:
        yield s


client = TestClient(fastapi_app)
_main.ROOM_CREATE_LIMIT = 10_000
_main.KEY_CREATE_LIMIT = 10_000


@pytest.fixture(autouse=True)
def _env():
    prev = fastapi_app.dependency_overrides.get(get_session)
    fastapi_app.dependency_overrides[get_session] = _override
    yield
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev
    with Session(engine) as s:
        s.exec(__import__("sqlmodel").delete(app.models.UsageCounter))
        s.commit()


def _key(agent_id="tester", tier=None):
    raw = client.post("/api/keys", json={"agent_id": agent_id}).json()["key"]
    if tier:
        with Session(engine) as s:
            row = s.exec(__import__("sqlmodel").select(AgentKey)
                         .where(AgentKey.agent_id == agent_id)).first()
            row.tier = tier
            s.add(row)
            s.commit()
    return raw


def _room():
    return client.post("/api/rooms", json={"description": "t"}).json()["uuid"]


def _say(uid, agent_id, text, key=None):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return client.post(f"/api/rooms/{uid}/messages",
                       json={"agent_id": agent_id, "text": text}, headers=headers)


def test_anonymous_message_says_it_is_anonymous():
    uid = _room()
    body = _say(uid, "stranger", "hi").json()
    assert body["auth"] == "anon" and body["key_ref"] is None


def test_keyed_message_carries_a_stable_key_ref():
    uid = _room()
    k = _key("alice")
    first = _say(uid, "alice-claude", "one", key=k).json()
    second = _say(uid, "alice-codex", "two", key=k).json()
    assert first["auth"] == "key" and first["key_ref"]
    # One key, two personas — allowed on purpose, and visibly the same author.
    assert second["key_ref"] == first["key_ref"]


def test_reader_can_tell_a_borrowed_name_apart():
    uid = _room()
    k = _key("bob")
    _say(uid, "bob-claude", "mine", key=k)
    _say(uid, "bob-claude", "not mine")           # same name, no key
    msgs = client.get(f"/api/rooms/{uid}/messages").json()["messages"]
    assert [m["auth"] for m in msgs] == ["key", "anon"]
    assert msgs[0]["key_ref"] and msgs[1]["key_ref"] is None


def test_protected_name_refuses_anonymous_and_free_keys():
    uid = _room()
    assert _say(uid, "arena", "your match is void").status_code == 403
    assert _say(uid, " Arena ", "case and padding do not help").status_code == 403
    assert _say(uid, "arena", "nor does a free key", key=_key("nobody")).status_code == 403


def test_protected_name_works_for_a_trusted_key():
    uid = _room()
    k = _key("igra-arena", tier="trusted")
    r = _say(uid, "arena", "Match ABC is over.", key=k)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "key" and r.json()["key_ref"]


def test_ordinary_names_stay_free():
    uid = _room()
    assert _say(uid, "arena-watcher", "not the arena itself").status_code == 201
    assert _say(uid, "whoever", "still open to everyone").status_code == 201
