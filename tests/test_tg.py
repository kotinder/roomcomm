"""Telegram verification bridge: webhook auth, code binding, re-bind demotion."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, select
from sqlalchemy.pool import StaticPool

import app.models as models
import app.main as _main
import app.notify as notify
import app.quota as quota
import app.tg_bot as tg_bot
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
_main.KEY_CREATE_LIMIT = 10_000

SECRET = "test-hook-secret"


@pytest.fixture(autouse=True)
def _tg_env(monkeypatch):
    prev = fastapi_app.dependency_overrides.get(get_session)
    fastapi_app.dependency_overrides[get_session] = _override
    monkeypatch.setattr(_main, "TG_WEBHOOK_SECRET", SECRET)
    # capture outbound bot traffic instead of hitting Telegram
    sent: list[tuple] = []

    async def _fake_send_to(chat_id, text):
        sent.append(("reply", chat_id, text))

    async def _fake_send(text):
        sent.append(("owner", None, text))

    monkeypatch.setattr(notify, "send_to", _fake_send_to)
    monkeypatch.setattr(notify, "send", _fake_send)
    yield sent
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev


def _hook(update, secret=SECRET):
    return client.post(
        "/tg/webhook", json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
    )


def _tg_msg(text, tg_id=777):
    return {"message": {"chat": {"id": tg_id}, "from": {"id": tg_id}, "text": text}}


def _new_key(agent_id="tg-tester"):
    r = client.post("/api/keys", json={"agent_id": agent_id})
    assert r.status_code == 201
    return r.json()


def test_webhook_requires_secret():
    assert _hook(_tg_msg("/start"), secret="wrong").status_code == 404
    assert client.post("/tg/webhook", json=_tg_msg("/start")).status_code == 404


def test_webhook_off_when_unconfigured(monkeypatch):
    monkeypatch.setattr(_main, "TG_WEBHOOK_SECRET", "")
    assert _hook(_tg_msg("/start"), secret="").status_code == 404


def test_start_and_garbage(_tg_env):
    assert _hook(_tg_msg("/start")).status_code == 200
    assert _hook(_tg_msg("what is this")).status_code == 200
    kinds = [s[0] for s in _tg_env]
    assert kinds == ["reply", "reply"]
    assert "verify" in _tg_env[0][2].lower()
    assert "doesn't look like" in _tg_env[1][2]


def test_verify_flow_and_rebind(_tg_env):
    data = _new_key("first")
    r = _hook(_tg_msg(data["verify_code"], tg_id=42))
    assert r.status_code == 200
    # reply to user + owner notification
    assert [s[0] for s in _tg_env] == ["owner", "reply"] or \
           [s[0] for s in _tg_env] == ["reply", "owner"]
    reply = next(s[2] for s in _tg_env if s[0] == "reply")
    assert "verified" in reply
    with Session(engine) as s:
        k = s.exec(select(models.AgentKey).where(models.AgentKey.agent_id == "first")).one()
        assert k.tier == "verified" and k.contact == "tg:42"
        assert k.verify_code != data["verify_code"]  # code burned

    # same code again → unknown (single-use)
    _tg_env.clear()
    _hook(_tg_msg(data["verify_code"], tg_id=42))
    assert "Unknown code" in _tg_env[0][2]

    # second key on the same TG account → first demoted to free
    data2 = _new_key("second")
    _tg_env.clear()
    _hook(_tg_msg(data2["verify_code"], tg_id=42))
    with Session(engine) as s:
        k1 = s.exec(select(models.AgentKey).where(models.AgentKey.agent_id == "first")).one()
        k2 = s.exec(select(models.AgentKey).where(models.AgentKey.agent_id == "second")).one()
        assert k1.tier == "free" and "re-bound" in (k1.note or "")
        assert k2.tier == "verified" and k2.contact == "tg:42"


def test_start_deeplink_binds(_tg_env):
    """t.me/RoomComm_bot?start=<code> arrives as '/start <code>' — one-tap
    bind instead of the how-to; junk payloads still get the how-to."""
    data = _new_key("deeplink")
    r = _hook(_tg_msg(f"/start {data['verify_code']}", tg_id=55))
    assert r.status_code == 200
    reply = next(s[2] for s in _tg_env if s[0] == "reply")
    assert "verified" in reply
    with Session(engine) as s:
        k = s.exec(select(models.AgentKey).where(models.AgentKey.agent_id == "deeplink")).one()
        assert k.tier == "verified" and k.contact == "tg:55"
    _tg_env.clear()
    _hook(_tg_msg("/start notacode", tg_id=56))
    assert "verify" in _tg_env[0][2].lower()


def test_status_command(_tg_env):
    data = _new_key("stat-key")
    _hook(_tg_msg(data["verify_code"], tg_id=99))
    _tg_env.clear()
    _hook(_tg_msg("/status", tg_id=99))
    reply = _tg_env[0][2]
    assert "stat-key" in reply and "verified" in reply
    q = quota.TIER_QUOTAS["verified"]
    assert f"0/{q[0]}" in reply and f"0/{q[1]}" in reply

    _tg_env.clear()
    _hook(_tg_msg("/status", tg_id=12345))
    assert "No key is bound" in _tg_env[0][2]


def test_revoked_key_cannot_verify(_tg_env):
    data = _new_key("dead-key")
    with Session(engine) as s:
        k = s.exec(select(models.AgentKey).where(models.AgentKey.agent_id == "dead-key")).one()
        k.revoked = True
        s.add(k)
        s.commit()
    _hook(_tg_msg(data["verify_code"], tg_id=7))
    assert "revoked" in _tg_env[0][2]
    with Session(engine) as s:
        k = s.exec(select(models.AgentKey).where(models.AgentKey.agent_id == "dead-key")).one()
        assert k.tier == "free" and k.contact is None
