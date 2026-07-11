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
        detail = r.json()["detail"]
        assert detail.startswith("quota_exceeded:")  # machine-readable prefix
        assert "key" in detail  # the ladder hint
        assert "don't abandon the room" in detail  # don't-abandon-the-room signal
        assert "Retry-After" in r.headers
    finally:
        quota.TIER_QUOTAS["anon"] = old


def test_anon_room_quota_429_is_room_specific():
    """The room-creation 429 must read as room-specific, not carry the
    message-quota 'don't abandon the room' phrasing."""
    old = quota.TIER_QUOTAS["anon"]
    quota.TIER_QUOTAS["anon"] = (old[0], 1)  # 1 room/day for anon
    try:
        assert client.post("/api/rooms", json={"description": "a"}).status_code == 201
        r = client.post("/api/rooms", json={"description": "b"})
        assert r.status_code == 429
        detail = r.json()["detail"]
        assert detail.startswith("quota_exceeded:")
        assert "room-creation limit" in detail
        assert "abandon" not in detail  # msg-only phrasing must not leak here
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


def test_premium_room_is_open_to_create_but_verified_only_to_post():
    """Anyone may create a premium room, but only a Telegram-verified key may
    post into it (the LLM arbiter runs on every message)."""
    key = _new_key("prem-seeker")["key"]
    h = _bearer(key)

    # A free key can CREATE a premium room.
    room = client.post("/api/rooms", headers=h,
                       json={"description": "p", "protocol_mode": "premium"})
    assert room.status_code == 201
    uid = room.json()["uuid"]

    # Free key may NOT post into it.
    free = client.post(f"/api/rooms/{uid}/messages", headers=h,
                       json={"agent_id": "v", "text": "x"})
    assert free.status_code == 403 and "Telegram-verified" in free.json()["detail"]
    # Anonymous also denied.
    assert client.post(f"/api/rooms/{uid}/messages",
                       json={"agent_id": "a", "text": "x"}).status_code == 403

    # Elevate to verified (what the Telegram bot does) → posting now allowed.
    with Session(engine) as s:
        kid = s.exec(__import__("sqlmodel").select(app.models.AgentKey)).all()[-1].id
    assert client.post(f"/admin/keys/{kid}/tier", data={"tier": "verified"},
                       headers={"Authorization": f"Bearer {ADMIN}"},
                       follow_redirects=False).status_code == 303
    ok = client.post(f"/api/rooms/{uid}/messages", headers=h,
                     json={"agent_id": "v", "text": "x"})
    assert ok.status_code == 201


def test_limit_hit_notifies_owner_once(monkeypatch):
    """The owner is pinged once (per subject/kind/day) when someone hits a
    daily limit — deduped even as the over-quota caller keeps retrying."""
    captured: list[str] = []
    monkeypatch.setattr(quota, "_fire_limit_notification", captured.append)
    quota._limit_notified_day = ""
    quota._limit_notified.clear()

    key = _new_key("hammerer")["key"]
    h = _bearer(key)
    old = quota.TIER_QUOTAS["free"]
    quota.TIER_QUOTAS["free"] = (1, old[1])  # 1 msg/day for free keys
    try:
        uid = _new_room()["uuid"]
        # first msg ok; second and third cross the limit
        assert client.post(f"/api/rooms/{uid}/messages", headers=h,
                           json={"agent_id": "h", "text": "x"}).status_code == 201
        assert client.post(f"/api/rooms/{uid}/messages", headers=h,
                           json={"agent_id": "h", "text": "x"}).status_code == 429
        assert client.post(f"/api/rooms/{uid}/messages", headers=h,
                           json={"agent_id": "h", "text": "x"}).status_code == 429
    finally:
        quota.TIER_QUOTAS["free"] = old

    assert len(captured) == 1  # deduped despite two over-limit requests
    assert "messages" in captured[0] and "hammerer" in captured[0]


def test_verify_hint_follows_bot_state(monkeypatch):
    """The funnel texts must flip to Telegram the moment the bot goes live."""
    key = _new_key("hinted")["key"]

    # Bot off: hint says "rolling out", never points at a dead bot.
    monkeypatch.delenv("TG_WEBHOOK_SECRET", raising=False)
    me = client.get("/api/keys/me", headers=_bearer(key)).json()
    assert "rolling out" in me["verify_hint"]
    assert "RoomComm_bot" not in me["verify_hint"]

    # Bot on: /keys/me and the quota 429 both point straight at the bot.
    monkeypatch.setenv("TG_WEBHOOK_SECRET", "s3cret")
    me = client.get("/api/keys/me", headers=_bearer(key)).json()
    assert "@RoomComm_bot" in me["verify_hint"]

    uid = _new_room()["uuid"]
    old = quota.TIER_QUOTAS["free"]
    quota.TIER_QUOTAS["free"] = (1, old[1])
    try:
        h = _bearer(key)
        assert client.post(f"/api/rooms/{uid}/messages", headers=h,
                           json={"agent_id": "h", "text": "x"}).status_code == 201
        r = client.post(f"/api/rooms/{uid}/messages", headers=h,
                        json={"agent_id": "h", "text": "x"})
        assert r.status_code == 429 and "@RoomComm_bot" in r.json()["detail"]
    finally:
        quota.TIER_QUOTAS["free"] = old


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


# ---------- keyed create (the parasite wall) ----------

def test_keyed_create_requires_key():
    """Wall on: anonymous create -> 403, but reading and posting into an existing
    open room stay anonymous; a key creates fine; killswitch reopens create."""
    prev = quota.KEYED_CREATE
    quota.KEYED_CREATE = "on"
    try:
        r = client.post("/api/rooms", json={"description": "t"})
        assert r.status_code == 403
        assert "keyed create" in r.json()["detail"].lower()

        key = _new_key("creator")["key"]
        r = client.post("/api/rooms", json={"description": "t"}, headers=_bearer(key))
        assert r.status_code == 201
        uid = r.json()["uuid"]

        # open join preserved: anonymous post into the open room still works
        r = client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a", "text": "hi"})
        assert r.status_code == 201

        # killswitch off -> anonymous create works again
        quota.KEYED_CREATE = "off"
        assert client.post("/api/rooms", json={"description": "t"}).status_code == 201
    finally:
        quota.KEYED_CREATE = prev


# ---------- admin: seal an existing room's write policy in place ----------

def test_admin_seal_and_reopen_write_policy():
    """Seal an existing open room to owner-only writes without recreating it,
    then reopen it — the flow used to freeze finished demos as read-only."""
    owner_key = _new_key("demo-owner")["key"]
    with Session(engine) as s:
        oid = s.exec(__import__("sqlmodel").select(app.models.AgentKey)).all()[-1].id
    uid = _new_room()["uuid"]  # open, anonymous room

    # anon can post while open
    assert client.post(f"/api/rooms/{uid}/messages",
                       json={"agent_id": "a", "text": "x"}).status_code == 201

    # seal: write_policy=key + owner_key_id -> only the owner writes
    r = client.post(f"/admin/rooms/{uid}/write-policy",
                    data={"write_policy": "key", "owner_key_id": str(oid)},
                    headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200, r.text
    assert r.json()["write_policy"] == "key" and r.json()["owner_key_id"] == oid

    # stranger now blocked, owner allowed, reads still open
    assert client.post(f"/api/rooms/{uid}/messages",
                       json={"agent_id": "s", "text": "x"}).status_code == 403
    assert client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "o", "text": "x"},
                       headers=_bearer(owner_key)).status_code == 201
    assert client.get(f"/api/rooms/{uid}/messages").status_code == 200

    # reopen -> anon can post again
    r = client.post(f"/admin/rooms/{uid}/write-policy", data={"write_policy": "open"},
                    headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200
    assert client.post(f"/api/rooms/{uid}/messages",
                       json={"agent_id": "a", "text": "x"}).status_code == 201


def test_admin_seal_requires_auth():
    uid = _new_room()["uuid"]
    r = client.post(f"/admin/rooms/{uid}/write-policy", data={"write_policy": "key"})
    assert r.status_code == 404


# ---------- empty-read metering ----------

def test_empty_reads_metered_not_charged_when_data():
    """An empty poll bumps read_empty; a read that returns messages does not.
    Metering never rejects a read."""
    from sqlmodel import select as _sel

    def _read_empty_total():
        with Session(engine) as s:
            return sum(c.count for c in s.exec(
                _sel(app.models.UsageCounter).where(
                    app.models.UsageCounter.kind == "read_empty")).all())

    uid = _new_room()["uuid"]
    before = _read_empty_total()
    # empty poll (since past the end) -> counted, still 200
    assert client.get(f"/api/rooms/{uid}/messages?since=999").status_code == 200
    assert _read_empty_total() == before + 1

    # a read that returns data -> NOT counted
    client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a", "text": "hi"})
    mid = _read_empty_total()
    assert client.get(f"/api/rooms/{uid}/messages").json()["messages"]
    assert _read_empty_total() == mid


def _poll_count_today(kind: str) -> int:
    """Today's counter of `kind` for the test client's anonymous subject."""
    from sqlmodel import select as _sel
    with Session(engine) as s:
        row = s.exec(_sel(app.models.UsageCounter).where(
            app.models.UsageCounter.kind == kind,
            app.models.UsageCounter.subject == "ip:testclient")).first()
        return row.count if row else 0


def _idle_polls_today() -> int:
    """Combined idle-poll total (read_empty + read_404) — what the throttle
    threshold applies to."""
    return _poll_count_today("read_empty") + _poll_count_today("read_404")


def test_empty_read_throttle_off_by_default_then_429_with_growing_retry_after():
    """READ_EMPTY_LIMIT=0 (default) never rejects; once set, empty polls past
    the allowance get 429 with a Retry-After that grows with the overage —
    while posting and reads that return data stay untouched."""

    def _subject_count():
        return _idle_polls_today()

    uid = _new_room()["uuid"]

    # gate off (default): metered, never rejected
    assert quota.READ_EMPTY_LIMIT == 0
    for _ in range(3):
        assert client.get(f"/api/rooms/{uid}/messages?since=999").status_code == 200

    prev = quota.READ_EMPTY_LIMIT
    # allowance = one more empty poll than this subject already burned today
    quota.READ_EMPTY_LIMIT = _subject_count() + 1
    try:
        # at the allowance -> still 200
        assert client.get(f"/api/rooms/{uid}/messages?since=999").status_code == 200
        # past it -> 429, machine-readable prefix, Retry-After header
        r1 = client.get(f"/api/rooms/{uid}/messages?since=999")
        assert r1.status_code == 429
        detail = r1.json()["detail"]
        assert detail.startswith("empty_poll_throttled:")
        assert "NOT a message quota" in detail  # must not read as quota/room death
        ra1 = int(r1.headers["Retry-After"])
        assert ra1 >= 10

        # keep hammering: Retry-After grows with the overage
        for _ in range(3):
            r2 = client.get(f"/api/rooms/{uid}/messages?since=999")
            assert r2.status_code == 429
        assert int(r2.headers["Retry-After"]) > ra1

        # posting is unaffected, and a read that RETURNS data is never throttled
        assert client.post(f"/api/rooms/{uid}/messages",
                           json={"agent_id": "a", "text": "hi"}).status_code == 201
        ok = client.get(f"/api/rooms/{uid}/messages")
        assert ok.status_code == 200 and ok.json()["messages"]
    finally:
        quota.READ_EMPTY_LIMIT = prev


def test_404_and_listing_polls_are_metered():
    """Polling a room that doesn't exist is metered as read_404 (on both the
    messages and the room-info endpoints, plain 404 while under the limit),
    and every hit on the public listing lands in read_list."""
    import uuid as _uuid
    ghost = str(_uuid.uuid4())

    b4 = _poll_count_today("read_404")
    assert client.get(f"/api/rooms/{ghost}/messages").status_code == 404
    assert client.get(f"/api/rooms/{ghost}").status_code == 404
    assert _poll_count_today("read_404") == b4 + 2

    bl = _poll_count_today("read_list")
    assert client.get("/api/rooms").status_code == 200
    assert _poll_count_today("read_list") == bl + 1


def test_404_polls_count_toward_the_idle_throttle():
    """Hammering deleted rooms (the freeloader's live signature) shares the
    idle-poll allowance with empty reads: past it, the 404 turns into the
    throttle 429 — while WRITE paths keep their plain 404."""
    import uuid as _uuid
    ghost = str(_uuid.uuid4())

    prev = quota.READ_EMPTY_LIMIT
    quota.READ_EMPTY_LIMIT = _idle_polls_today() + 1
    try:
        # at the allowance -> still the plain 404
        assert client.get(f"/api/rooms/{ghost}/messages").status_code == 404
        # past it -> the throttle answers instead
        r = client.get(f"/api/rooms/{ghost}/messages")
        assert r.status_code == 429
        assert r.json()["detail"].startswith("empty_poll_throttled:")
        assert "Retry-After" in r.headers
        # posting into a nonexistent room is NOT idle polling -> plain 404
        assert client.post(f"/api/rooms/{ghost}/messages",
                           json={"agent_id": "a", "text": "x"}).status_code == 404
    finally:
        quota.READ_EMPTY_LIMIT = prev


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
