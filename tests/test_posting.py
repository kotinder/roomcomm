"""The write path of a post (app/posting.py) and how it fails under contention.

What used to happen on every single message: COUNT(*) over the room to check
the 1000 cap, INSERT, then an UPDATE of the room to push its indexed TTL
column out — the count and the TTL write both inside SQLite's single write
lock. These tests pin the replacement: one UPDATE that takes a slot and
extends the TTL at once, and a 503 rather than a 500 when the lock is busy.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError as SAOperationalError
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select, func

import app.models  # noqa: F401
import app.main as _main
from app import main, posting, quota
from app.database import get_session, wal_checkpoint
from app.main import app as fastapi_app
from app.models import Message, Room

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


@pytest.fixture(autouse=True)
def _posting_test_env():
    """Point the app at THIS module's DB only while its tests run — modules
    share one app object (same reasoning as test_ttl)."""
    prev = fastapi_app.dependency_overrides.get(get_session)
    prev_mode = quota.QUOTA_MODE
    fastapi_app.dependency_overrides[get_session] = _override
    quota.QUOTA_MODE = "soft"  # the daily budget is not what's under test here
    yield
    quota.QUOTA_MODE = prev_mode
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev


def _room() -> str:
    r = client.post("/api/rooms", json={"description": "write path"})
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def _post(uid: str, text: str = "hello", **payload):
    return client.post(f"/api/rooms/{uid}/messages",
                       json={"agent_id": "a", "text": text, **payload})


def _counter(uid: str) -> int:
    with Session(engine) as s:
        return posting.room_message_count(s, uid)


def _real_count(uid: str) -> int:
    with Session(engine) as s:
        return s.exec(select(func.count()).select_from(Message)
                      .where(Message.room_uuid == uid)).one()


def test_counter_keeps_step_with_the_messages_table():
    uid = _room()
    for i in range(3):
        assert _post(uid, f"m{i}").status_code == 201
    assert _counter(uid) == 3 == _real_count(uid)


def test_the_cap_is_the_counter_and_it_is_exact():
    uid = _room()
    with Session(engine) as s:  # one short of full, without 999 inserts
        s.get(Room, uid).messages_count = posting.MAX_MESSAGES_PER_ROOM - 1
        s.commit()
    assert _post(uid, "last one").status_code == 201
    r = _post(uid, "over the line")
    assert r.status_code == 429
    assert r.json()["detail"].startswith("room_full")
    # The rejected post took no slot: its UPDATE matched no row, so nothing
    # was written at all.
    assert _counter(uid) == posting.MAX_MESSAGES_PER_ROOM


def test_a_rejected_post_consumes_no_slot():
    uid = _room()
    # A signature that does not verify — rejected before the slot claim.
    r = _post(uid, "signed badly", pubkey_hex="ab" * 32, signature_hex="cd" * 64,
              ts_iso="2026-10-27T00:00:00Z")
    assert r.status_code == 400
    assert _counter(uid) == 0 == _real_count(uid)


def test_a_busy_write_lock_answers_503_not_500(monkeypatch):
    uid = _room()

    def busy(session, room, cap=posting.MAX_MESSAGES_PER_ROOM):
        raise SAOperationalError("UPDATE rooms SET …", (),
                                 Exception("database is locked"))

    monkeypatch.setattr(posting, "claim_slot", busy)
    before = main._db_busy_total
    r = _post(uid, "during a write storm")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"
    assert r.json()["detail"].startswith("db_busy")
    assert main._db_busy_total == before + 1


def test_other_operational_errors_are_still_faults(monkeypatch):
    uid = _room()

    def broken(session, room, cap=posting.MAX_MESSAGES_PER_ROOM):
        raise SAOperationalError("UPDATE rooms SET …", (),
                                 Exception("no such column: nonsense"))

    monkeypatch.setattr(posting, "claim_slot", broken)
    monkeypatch.setattr(main.notify, "is_configured", lambda: False)
    quiet = TestClient(fastapi_app, raise_server_exceptions=False)
    r = quiet.post(f"/api/rooms/{uid}/messages",
                   json={"agent_id": "a", "text": "boom"})
    assert r.status_code == 500


def test_wal_checkpoint_is_callable_from_a_background_thread():
    busy, pages = wal_checkpoint()
    assert busy in (0, 1)
    assert pages >= -1  # -1 when there is nothing to move
