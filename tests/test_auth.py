"""Auth MVP: keys, daily quotas, write_policy, admin key management.

Uses its own in-memory DB (same pattern as test_api.py). Quota mode is
flipped per-test via the quota module attribute; the default in tests is
'hard' so ceilings actually reject — the soft-mode test flips it back.
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

# Burst limiters off — these tests exercise the *daily quota* layer.
_main.ROOM_CREATE_LIMIT = 10_000
_main.KEY_CREATE_LIMIT = 10_000

ADMIN = "test-admin-token"
_main.ADMIN_TOKEN = ADMIN


@pytest.fixture(autouse=True)
def _auth_test_env():
    """Point the app at THIS module's DB only while its tests run —
    an import-time dependency_overrides assignment would clobber
    test_api's override for the whole session (modules share the app)."""
    prev = fastapi_app.dependency_overrides.get(get_session)
    fastapi_app.dependency_overrides[get_session] = _override
    quota.QUOTA_MODE = "hard"
    yield
    quota.QUOTA_MODE = "hard"
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev
    with Session(engine) as s:
        s.exec(__import__("sqlmodel").delete(app.models.UsageCounter))
        s.commit()


def _new_key(agent_id="tester"):
    r = client.post("/api/keys", json={"agent_id": agent_id})
    assert r.status_code == 201
    return r.json()


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def _new_room(headers=None, **kwargs):
    r = client.post("/api/rooms", json={"description": "t", **kwargs}, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()


# ---------- keys ----------

def test_key_issue_and_me():
    data = _new_key("alice")
    assert data["key"].startswith("rk_") and data["tier"] == "free"
    assert data["quota"]["msg"] > 0 and data["verify_code"]
    me = client.get("/api/keys/me", headers=_bearer(data["key"]))
    assert me.status_code == 200
    body = me.json()
    assert body["agent_id"] == "alice" and body["used_today"] == {"msg": 0, "room": 0}
    assert body["verify_code"] == data["verify_code"]


def test_key_me_requires_bearer():
    assert client.get("/api/keys/me").status_code == 401
    assert client.get("/api/keys/me", headers=_bearer("rk_nonexistent")).status_code == 401
    assert client.get("/api/keys/me", headers={"Authorization": "Basic zzz"}).status_code == 401


def test_key_farm_rate_limit():
    old = _main.KEY_CREATE_LIMIT
    _main.KEY_CREATE_LIMIT = 3
    _main._key_buckets.clear()
    try:
        for _ in range(3):
            assert client.post("/api/keys", json={}).status_code == 201
        assert client.post("/api/keys", json={}).status_code == 429
    finally:
        _main.KEY_CREATE_LIMIT = old
        _main._key_buckets.clear()


# ---------- daily quotas ----------

def test_anon_msg_quota_hits_429_with_hint():
    uid = _new_room()["uuid"]
    old = quota.TIER_QUOTAS["anon"]
    quota.TIER_QUOTAS["anon"] = (3, old[1])
    try:
        for i in range(3):
            r = client.post(f"/api/rooms/{uid}/messages",
                            json={"agent_id": "a", "text": f"m{i}"})
            assert r.status_code == 201
        r = client.post(f"/api/rooms/{uid}/messages",
                        json={"agent_id": "a", "text": "over"})
        assert r.status_code == 429
        assert "key" in r.json()["detail"]  # the ladder hint
        assert "Retry-After" in r.headers
    finally:
        quota.TIER_QUOTAS["anon"] = old


def test_key_quota_separate_from_anon_and_capped():
    uid = _new_room()["uuid"]
    key = _new_key("bob")["key"]
    old_anon, old_free = quota.TIER_QUOTAS["anon"], quota.TIER_QUOTAS["free"]
    quota.TIER_QUOTAS["anon"] = (1, old_anon[1])
    quota.TIER_QUOTAS["free"] = (2, old_free[1])
    try:
        # anon burns its single message
        assert client.post(f"/api/rooms/{uid}/messages",
                           json={"agent_id": "a", "text": "x"}).status_code == 201
        assert client.post(f"/api/rooms/{uid}/messages",
                           json={"agent_id": "a", "text": "x"}).status_code == 429
        # key has its own budget — capped too (key != unlimited)
        h = _bearer(key)
        assert client.post(f"/api/rooms/{uid}/messages", headers=h,
                           json={"agent_id": "b", "text": "x"}).status_code == 201
        assert client.post(f"/api/rooms/{uid}/messages", headers=h,
                           json={"agent_id": "b", "text": "x"}).status_code == 201
        r = client.post(f"/api/rooms/{uid}/messages", headers=h,
                        json={"agent_id": "b", "text": "x"})
        assert r.status_code == 429
        assert "key" in r.json()["detail"]
    finally:
        quota.TIER_QUOTAS["anon"], quota.TIER_QUOTAS["free"] = old_anon, old_free


def test_soft_mode_never_rejects():
    quota.QUOTA_MODE = "soft"
    uid = _new_room()["uuid"]
    old = quota.TIER_QUOTAS["anon"]
    quota.TIER_QUOTAS["anon"] = (1, old[1])
    try:
        for i in range(4):
            r = client.post(f"/api/rooms/{uid}/messages",
                            json={"agent_id": "a", "text": f"m{i}"})
            assert r.status_code == 201  # metered but never cut
    finally:
        quota.TIER_QUOTAS["anon"] = old


def test_room_quota_for_anon():
    old = quota.TIER_QUOTAS["anon"]
    quota.TIER_QUOTAS["anon"] = (old[0], 2)
    try:
        _new_room()
        _new_room()
        r = client.post("/api/rooms", json={"description": "t"})
        assert r.status_code == 429
    finally:
        quota.TIER_QUOTAS["anon"] = old


def test_per_key_quota_override():
    key_data = _new_key("vip")
    key = key_data["key"]
    with Session(engine) as s:
        kid = s.exec(__import__("sqlmodel").select(app.models.AgentKey)).all()[-1].id
    r = client.post(f"/admin/keys/{kid}/tier",
                    data={"tier": "free", "daily_msg_quota": "1"},
                    headers={"Authorization": f"Bearer {ADMIN}"},
                    follow_redirects=False)
    assert r.status_code == 303
    uid = _new_room()["uuid"]
    h = _bearer(key)
    assert client.post(f"/api/rooms/{uid}/messages", headers=h,
                       json={"agent_id": "v", "text": "x"}).status_code == 201
    assert client.post(f"/api/rooms/{uid}/messages", headers=h,
                       json={"agent_id": "v", "text": "x"}).status_code == 429


# ---------- revocation ----------

def test_revoked_key_is_rejected_everywhere():
    data = _new_key("dead")
    key = data["key"]
    with Session(engine) as s:
        kid = s.exec(__import__("sqlmodel").select(app.models.AgentKey)).all()[-1].id
    r = client.post(f"/admin/keys/{kid}/revoke",
                    headers={"Authorization": f"Bearer {ADMIN}"},
                    follow_redirects=False)
    assert r.status_code == 303
    # explicit 403, not a silent downgrade to anonymous
    assert client.get("/api/keys/me", headers=_bearer(key)).status_code == 403
    uid = _new_room()["uuid"]
    r = client.post(f"/api/rooms/{uid}/messages", headers=_bearer(key),
                    json={"agent_id": "d", "text": "x"})
    assert r.status_code == 403


# ---------- write_policy ----------

def test_write_policy_key_room():
    owner = _new_key("owner")["key"]
    room = _new_room(headers=_bearer(owner), write_policy="key")
    assert room["write_policy"] == "key"
    wk = room["write_key"]
    assert wk and wk.startswith("wk_")
    uid = room["uuid"]

    # stranger: 403
    r = client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "s", "text": "x"})
    assert r.status_code == 403
    # with the room write-key: 201
    r = client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "s", "text": "x"},
                    headers={"X-Room-Key": wk})
    assert r.status_code == 201
    # wrong write-key: 403
    r = client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "s", "text": "x"},
                    headers={"X-Room-Key": "wk_wrong"})
    assert r.status_code == 403
    # owner via Bearer, no room key: 201
    r = client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "o", "text": "x"},
                    headers=_bearer(owner))
    assert r.status_code == 201


def test_write_policy_signed_rejected_for_now():
    r = client.post("/api/rooms", json={"description": "t", "write_policy": "signed"})
    assert r.status_code == 400


def test_open_room_regression():
    """Pre-auth behavior intact: anonymous post into an open room just works."""
    uid = _new_room()["uuid"]
    r = client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a", "text": "hi"})
    assert r.status_code == 201
    body = client.get(f"/api/rooms/{uid}/messages").json()
    assert body["messages"][0]["text"] == "hi"


# ---------- admin auth ----------

def test_admin_login_flow_and_legacy_redirect():
    # Fresh client — the module-level one may carry the admin cookie from
    # earlier tests (TestClient persists cookies).
    fresh = TestClient(fastapi_app)
    # no cookie → login form, not the dashboard
    r = fresh.get("/admin")
    assert r.status_code == 200 and "Sign in" in r.text and "Agent keys" not in r.text
    # wrong token → 404
    assert fresh.post("/admin/login", data={"token": "nope"}).status_code == 404
    # correct token → cookie → dashboard
    r = fresh.post("/admin/login", data={"token": ADMIN}, follow_redirects=False)
    assert r.status_code == 303 and "admin_token" in r.headers.get("set-cookie", "")
    r = fresh.get("/admin", headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200 and "Agent keys" in r.text
    # legacy bookmark logs in and redirects (token leaves the URL)
    r = fresh.get(f"/admin/{ADMIN}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"


def test_admin_actions_require_auth():
    fresh = TestClient(fastapi_app)
    assert fresh.post("/admin/keys/1/revoke").status_code == 404
    assert fresh.post("/admin/keys/1/tier", data={"tier": "trusted"}).status_code == 404
