"""/mcp holds the same per-IP burst limits as REST and A2A (audit 2026-11).

nginx shapes REST posting at 10/min, but /mcp sits in a 60/min zone; the
in-app buckets (main._check_*_rate) are what keeps the transports equal.
MCP tools are called directly with a stub context, as in test_knock.py.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as _main
import app.mcp_server as mcp_server
import app.models  # noqa: F401
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
    # In-process buckets only (no Redis in tests), fresh for every test.
    monkeypatch.setattr(_main.shared, "rate_hit", lambda *a, **k: None)
    for buckets in (_main._msg_buckets, _main._create_buckets, _main._file_buckets):
        buckets.clear()
    monkeypatch.setattr(_main, "KEY_CREATE_LIMIT", 10_000)
    yield
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev


def _ctx(ip: str, key: str = ""):
    headers = {"x-real-ip": ip}
    if key:
        headers["authorization"] = f"Bearer {key}"
    req = SimpleNamespace(headers=headers, client=SimpleNamespace(host="127.0.0.1"))
    return SimpleNamespace(request_context=SimpleNamespace(request=req))


def _room() -> str:
    r = client.post("/api/rooms", json={"description": "mcp limits"},
                    headers={"X-Real-IP": "10.9.9.9"})
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def test_mcp_send_message_has_the_rest_burst_limit(monkeypatch):
    monkeypatch.setattr(_main, "MSG_POST_LIMIT", 3)
    uid = _room()
    for i in range(3):
        mcp_server.send_message(uid, "burst", f"m{i}", _ctx("10.1.1.1"))
    with pytest.raises(RuntimeError, match="^429"):
        mcp_server.send_message(uid, "burst", "one too many", _ctx("10.1.1.1"))
    # Another IP has its own bucket.
    mcp_server.send_message(uid, "other", "fine", _ctx("10.1.1.2"))


def test_mcp_create_room_has_the_rest_burst_limit(monkeypatch):
    monkeypatch.setattr(_main, "ROOM_CREATE_LIMIT", 2)
    key = client.post("/api/keys", json={"agent_id": "mcp-creator"}).json()["key"]
    for _ in range(2):
        asyncio.run(mcp_server.create_room(_ctx("10.2.2.2", key)))
    with pytest.raises(RuntimeError, match="^429"):
        asyncio.run(mcp_server.create_room(_ctx("10.2.2.2", key)))
