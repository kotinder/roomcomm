""""You are awaited elsewhere" on REST and MCP (A2A has its own tests in
test_a2a.py): the notice rides on the calls an
agent already makes in its loop, so it reaches agents that never learned
about the inbox.

Same in-memory-DB pattern as test_inbox.py. MCP tools are called directly
with a stub context (the MCP session manager may only start once per test
process), and their results are validated against the very output model
FastMCP checks them with.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, delete
from sqlalchemy.pool import StaticPool

import app.inbox as inbox
import app.main as _main
import app.mcp_server as mcp_server
import app.models as models
import app.quota as quota
from app.database import get_session
from app.main import app as fastapi_app

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
def _env(monkeypatch):
    prev = fastapi_app.dependency_overrides.get(get_session)
    fastapi_app.dependency_overrides[get_session] = _override
    monkeypatch.setattr(mcp_server, "engine", engine)
    monkeypatch.setattr(quota, "QUOTA_MODE", "hard")
    inbox._await_cache.clear()
    yield
    inbox._await_cache.clear()
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev
    with Session(engine) as s:
        s.exec(delete(models.UsageCounter))
        s.exec(delete(models.RoomSeen))
        s.commit()


def _key(name):
    r = client.post("/api/keys", json={"agent_id": name})
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['key']}"}


def _room(h):
    r = client.post("/api/rooms", json={"description": "t"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def _post(room, agent_id, text, h=None):
    r = client.post(f"/api/rooms/{room}/messages", json={"agent_id": agent_id, "text": text},
                    headers=h or {})
    assert r.status_code == 201, r.text
    return r.json()


def _scene(name):
    """`name` posted in `mine` and `called`; then bob posts in `mine` and carl
    calls `name` in `called`; `quiet` is where `name` polls."""
    h = _key(name)
    host = _key("host-" + name)
    mine, called, quiet = _room(host), _room(host), _room(host)
    _post(mine, name, "hello", h)
    _post(called, name, "here too", h)
    _post(quiet, name, "watching", h)
    _post(mine, "bob", "news")
    _post(called, "carl", f"{name}, are you there?")
    return h, mine, called, quiet


# ---------- REST ----------

def test_rest_empty_poll_carries_awaiting():
    h, mine, called, quiet = _scene("ann-rest")
    r = client.get(f"/api/rooms/{quiet}/messages?since=999999", headers=h)
    assert r.status_code == 200 and r.json()["messages"] == []
    aw = r.json()["awaiting"]
    assert aw["rooms"] == [{"uuid": called, "new_messages": 1},
                           {"uuid": mine, "new_messages": 1}]
    assert [m["room_uuid"] for m in aw["mentions"]] == [called]
    assert "inbox" in aw["hint"]
    assert aw["mentions"][0]["at"].endswith("Z")  # like every other timestamp


def test_rest_mention_in_a_room_the_key_never_joined_stays_hidden():
    # Anyone may take any agent_id; only the key opens a room (01.10.2026).
    h, mine, called, quiet = _scene("hal-rest")
    stranger = _key("hal-rest")           # same name, different key
    r = client.get("/api/keys/me", headers=stranger).json()
    assert "awaiting" not in r
    host = _key("host-x")
    private = _room(host)
    _post(private, "carl", "hal-rest, the vault code is 1234")
    for hh in (h, stranger):
        aw = client.get("/api/keys/me", headers=hh).json().get("awaiting") or {}
        assert private not in str(aw)
    # and the real key still gets its own mention: hidden must not mean broken
    aw = client.get("/api/keys/me", headers=h).json()["awaiting"]
    assert [m["room_uuid"] for m in aw["mentions"]] == [called]


def test_rest_reading_a_room_clears_it_at_once():
    h, mine, called, quiet = _scene("ben-rest")
    assert client.get(f"/api/rooms/{quiet}/messages?since=999999", headers=h).json()["awaiting"]
    client.get(f"/api/rooms/{mine}/messages", headers=h)      # cache must drop here
    client.get(f"/api/rooms/{called}/messages", headers=h)
    r = client.get(f"/api/rooms/{quiet}/messages?since=999999", headers=h)
    assert "awaiting" not in r.json()


def test_rest_post_and_keys_me_carry_it_and_anon_never_does():
    h, mine, called, quiet = _scene("cid-rest")
    posted = client.post(f"/api/rooms/{quiet}/messages",
                         json={"agent_id": "cid-rest", "text": "x"}, headers=h).json()
    assert {x["uuid"] for x in posted["awaiting"]["rooms"]} == {mine, called}
    assert client.get("/api/keys/me", headers=h).json()["awaiting"]["mentions"]
    anon = client.get(f"/api/rooms/{quiet}/messages").json()
    assert "awaiting" not in anon
    # the messages themselves never grow the field
    assert all("awaiting" not in m for m in anon["messages"])


def test_rest_bad_key_read_still_works_without_it():
    _, _, _, quiet = _scene("dee-rest")
    r = client.get(f"/api/rooms/{quiet}/messages", headers={"Authorization": "Bearer rk_bad"})
    assert r.status_code == 200 and "awaiting" not in r.json()


# ---------- MCP ----------

def _ctx(h):
    req = SimpleNamespace(headers={"authorization": h["Authorization"]},
                          client=SimpleNamespace(host="10.0.0.1"))
    return SimpleNamespace(request_context=SimpleNamespace(request=req))


def _checked(tool, result):
    """Validate like FastMCP does before it answers the client."""
    meta = mcp_server.mcp._tool_manager.get_tool(tool).fn_metadata
    meta.output_model.model_validate(result)
    return result


def test_mcp_tools_carry_awaiting_and_validate():
    h, mine, called, quiet = _scene("eve-mcp")
    ctx = _ctx(h)
    res = _checked("read_messages", mcp_server.read_messages(quiet, ctx, since=999999))
    assert {x["uuid"] for x in res["awaiting"]["rooms"]} == {mine, called}
    assert res["awaiting"]["mentions"][0]["room_uuid"] == called
    assert isinstance(res["awaiting"]["mentions"][0]["at"], str)
    res = _checked("get_room", mcp_server.get_room(quiet, ctx))
    assert res["awaiting"]["mentions"]
    res = _checked("send_message", mcp_server.send_message(quiet, "eve-mcp", "hi", ctx))
    assert {x["uuid"] for x in res["awaiting"]["rooms"]} == {mine, called}


def test_mcp_quiet_results_still_validate():
    h = _key("fay-mcp")
    room = _room(h)
    ctx = _ctx(h)
    res = _checked("get_room", mcp_server.get_room(room, ctx))
    assert "awaiting" not in res
    res = _checked("send_message", mcp_server.send_message(room, "fay-mcp", "x", ctx))
    assert "awaiting" not in res


# ---------- the shared digest ----------

def test_awaiting_cache_never_serves_stale_news():
    h, mine, called, quiet = _scene("gus-core")
    with Session(engine) as s:
        key = s.exec(__import__("sqlmodel").select(models.AgentKey)
                     .where(models.AgentKey.agent_id == "gus-core")).one()
        first = inbox.awaiting(s, key, quiet)
        assert inbox.awaiting(s, key, quiet) is first    # nothing new: cached
        _post(mine, "bob", "more news")                  # new message: fresh at once
        again = inbox.awaiting(s, key, quiet)
        assert again is not first and again["rooms"][0]["new_messages"] == 2
        inbox.advance_seen(s, key.id, mine, 10**9)       # watermark moves: dropped
        s.commit()
        assert [x["uuid"] for x in inbox.awaiting(s, key, quiet)["rooms"]] == [called]
