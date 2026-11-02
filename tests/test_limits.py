"""The limits the docs promise, checked at their edges on REST and MCP alike
(audit 2026-11): text ≤ 10 000 chars, agent_id ≤ 100, 1000 messages per
room, 72 h of silence and the room is gone, daily quotas, and per-IP
accounting that a client cannot reset by sending its own forwarding header.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, delete

import app.main as _main
import app.mcp_server as mcp_server
import app.models as models
import app.quota as quota
from app import posting, ttl
from app.database import get_session
from app.main import app as fastapi_app

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)


def _override():
    with Session(engine) as s:
        yield s


client = TestClient(fastapi_app)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    prev = fastapi_app.dependency_overrides.get(get_session)
    fastapi_app.dependency_overrides[get_session] = _override
    monkeypatch.setattr(mcp_server, "engine", engine)
    monkeypatch.setattr(_main.shared, "rate_hit", lambda *a, **k: None)
    monkeypatch.setattr(_main, "ROOM_CREATE_LIMIT", 10_000)
    monkeypatch.setattr(_main, "MSG_POST_LIMIT", 10_000)
    monkeypatch.setattr(_main, "KEY_CREATE_LIMIT", 10_000)
    yield
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev
    with Session(engine) as s:
        s.exec(delete(models.UsageCounter))
        s.commit()


def _room(**body) -> str:
    r = client.post("/api/rooms", json={"description": "limits", **body})
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def _post(uid, text="hi", agent_id="a", headers=None):
    return client.post(f"/api/rooms/{uid}/messages",
                       json={"agent_id": agent_id, "text": text}, headers=headers or {})


def _ctx(ip="10.5.5.5", key=""):
    headers = {"x-real-ip": ip}
    if key:
        headers["authorization"] = f"Bearer {key}"
    req = SimpleNamespace(headers=headers, client=SimpleNamespace(host="127.0.0.1"))
    return SimpleNamespace(request_context=SimpleNamespace(request=req))


# ---------- text ≤ 10 000, agent_id ≤ 100 ----------

def test_text_limit_is_exactly_10000_on_rest():
    uid = _room()
    assert _post(uid, "x" * 10_000).status_code == 201
    assert _post(uid, "x" * 10_001).status_code == 400


def test_agent_id_limit_is_exactly_100_on_rest():
    uid = _room()
    assert _post(uid, agent_id="a" * 100).status_code == 201
    assert _post(uid, agent_id="a" * 101).status_code == 400


def test_text_and_agent_id_limits_on_mcp():
    uid = _room()
    mcp_server.send_message(uid, "a" * 100, "x" * 10_000, _ctx())
    with pytest.raises(ValueError):
        mcp_server.send_message(uid, "a", "x" * 10_001, _ctx())
    with pytest.raises(ValueError):
        mcp_server.send_message(uid, "a" * 101, "hi", _ctx())


# ---------- 1000 messages per room ----------

def _fill_to(uid: str, n: int) -> None:
    with Session(engine) as s:
        room = s.get(models.Room, uid)
        room.messages_count = n
        s.add(room)
        s.commit()


def test_room_cap_is_1000_on_rest():
    uid = _room()
    _fill_to(uid, posting.MAX_MESSAGES_PER_ROOM - 1)
    assert _post(uid, "the last one").status_code == 201
    r = _post(uid, "one too many")
    assert r.status_code == 429 and r.json()["detail"].startswith("room_full")


def test_room_cap_is_1000_on_mcp():
    uid = _room()
    _fill_to(uid, posting.MAX_MESSAGES_PER_ROOM - 1)
    mcp_server.send_message(uid, "a", "the last one", _ctx())
    with pytest.raises(Exception, match="room_full"):
        mcp_server.send_message(uid, "a", "one too many", _ctx())


# ---------- 72 h of silence ----------

def test_default_lifetime_is_72_hours():
    assert ttl.DEFAULT_HOURS == 72
    before = ttl.utcnow()
    uid = _room()
    with Session(engine) as s:
        exp = ttl._as_utc(s.get(models.Room, uid).expires_at)
    assert timedelta(hours=72) - timedelta(minutes=1) <= exp - before <= timedelta(hours=72, minutes=1)


def test_silent_room_is_gone_after_72_hours(monkeypatch):
    uid = _room()
    assert _post(uid).status_code == 201
    later = ttl.utcnow() + timedelta(hours=72, seconds=5)
    monkeypatch.setattr(ttl, "utcnow", lambda: later)
    assert client.get(f"/api/rooms/{uid}/messages").status_code == 410
    assert _post(uid, "anyone there?").status_code == 410
    with pytest.raises(Exception, match="room_expired"):
        mcp_server.read_messages(uid, _ctx())


def test_room_is_alive_just_before_72_hours(monkeypatch):
    uid = _room()
    soon = ttl.utcnow() + timedelta(hours=71, minutes=58)
    monkeypatch.setattr(ttl, "utcnow", lambda: soon)
    assert client.get(f"/api/rooms/{uid}/messages").status_code == 200


# ---------- daily quotas ----------

def test_anon_message_quota_is_shared_by_rest_and_mcp(monkeypatch):
    monkeypatch.setattr(quota, "QUOTA_MODE", "hard")
    monkeypatch.setitem(quota.TIER_QUOTAS, "anon", (2, 3))
    uid = _room()
    ip = "10.6.6.6"
    assert _post(uid, headers={"X-Real-IP": ip}).status_code == 201
    mcp_server.send_message(uid, "a", "second", _ctx(ip))
    # REST and MCP bill the same subject: the third message is over budget on both.
    r = _post(uid, headers={"X-Real-IP": ip})
    assert r.status_code == 429 and r.json()["detail"].startswith("quota_exceeded")
    with pytest.raises(RuntimeError, match="quota_exceeded"):
        mcp_server.send_message(uid, "a", "fourth", _ctx(ip))


def test_x_forwarded_for_does_not_reset_the_ip_budget(monkeypatch):
    monkeypatch.setattr(quota, "QUOTA_MODE", "hard")
    monkeypatch.setitem(quota.TIER_QUOTAS, "anon", (1, 3))
    uid = _room()
    ip = "10.7.7.7"
    assert _post(uid, headers={"X-Real-IP": ip}).status_code == 201
    r = _post(uid, headers={"X-Real-IP": ip, "X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 429


# ---------- keys ----------

def test_bad_key_is_an_error_not_a_silent_anonymous_post():
    uid = _room()
    assert _post(uid, headers={"Authorization": "Bearer rk_" + "x" * 43}).status_code == 401
    assert _post(uid, headers={"Authorization": "Basic Zm9vOmJhcg=="}).status_code == 401
    with pytest.raises(ValueError, match="unknown key"):
        mcp_server.send_message(uid, "a", "hi", _ctx(key="rk_" + "x" * 43))


def test_keys_never_appear_in_answers():
    raw = client.post("/api/keys", json={"agent_id": "quiet"}).json()["key"]
    me = client.get("/api/keys/me", headers={"Authorization": f"Bearer {raw}"})
    assert me.status_code == 200
    assert raw not in me.text and quota.hash_key(raw) not in me.text
    uid = _room()
    posted = _post(uid, headers={"Authorization": f"Bearer {raw}"})
    assert posted.status_code == 201
    listing = client.get(f"/api/rooms/{uid}/messages").text
    assert raw not in listing and quota.hash_key(raw) not in listing


def test_write_key_room_refuses_strangers_on_rest_and_mcp():
    r = client.post("/api/rooms", json={"description": "guarded", "write_policy": "key"})
    uid, wk = r.json()["uuid"], r.json()["write_key"]
    assert _post(uid).status_code == 403
    assert _post(uid, headers={"X-Room-Key": "wk_wrong"}).status_code == 403
    assert _post(uid, headers={"X-Room-Key": wk}).status_code == 201
    with pytest.raises(ValueError, match="write-protected"):
        mcp_server.send_message(uid, "a", "hi", _ctx())
    mcp_server.send_message(uid, "a", "hi", _ctx(), room_key=wk)
