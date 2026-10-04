"""Room TTL — rooms are ephemeral, and now the database agrees.

Covers the promise made in the README: a room created today stops answering,
everyone gets the same 410 once it does, the admin panel does not, and the
public showcase never advertises a room an agent can no longer join.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app import quota, ttl
from app.database import get_session
from app.main import app as fastapi_app
from app.models import Room, utcnow

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

import app.main as _main  # noqa: E402

_main.ROOM_CREATE_LIMIT = 10_000


@pytest.fixture(autouse=True)
def _ttl_test_env():
    """Point the app at THIS module's DB only while its tests run — modules
    share one app object, so an import-time override would clobber whichever
    module happened to be imported first (and _expire() would then be writing
    to a different database than the client reads)."""
    prev = fastapi_app.dependency_overrides.get(get_session)
    prev_mode = quota.QUOTA_MODE
    fastapi_app.dependency_overrides[get_session] = _override
    # test_auth leaves QUOTA_MODE='hard' behind; this module creates far more
    # than the anonymous 3-rooms/day allowance, and it is not what's under test.
    quota.QUOTA_MODE = "soft"
    yield
    quota.QUOTA_MODE = prev_mode
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev


def _create(**payload) -> dict:
    r = client.post("/api/rooms", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _expire(room_uuid: str, *, ago_hours: int = 1) -> None:
    """Age a room past its TTL without waiting for wall-clock time."""
    with Session(engine) as s:
        room = s.get(Room, room_uuid)
        room.expires_at = utcnow() - timedelta(hours=ago_hours)
        s.add(room)
        s.commit()


# --- creation ---------------------------------------------------------------

def test_new_room_gets_default_ttl():
    room = _create(description="default ttl")
    assert room["expires_at"], "a new room must carry an expiry date"

    info = client.get(f"/api/rooms/{room['uuid']}").json()
    left = info["expires_in_seconds"]
    # Default is 72h; allow a minute of slack for test execution time.
    assert abs(left - ttl.DEFAULT_HOURS * 3600) < 60


def test_ttl_hours_is_honoured():
    room = _create(description="short", ttl_hours=5)
    info = client.get(f"/api/rooms/{room['uuid']}").json()
    assert abs(info["expires_in_seconds"] - 5 * 3600) < 60


def test_explicit_expires_at_wins_over_ttl_hours():
    when = utcnow() + timedelta(hours=10)
    room = _create(
        description="date wins",
        ttl_hours=5,
        expires_at=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    info = client.get(f"/api/rooms/{room['uuid']}").json()
    assert abs(info["expires_in_seconds"] - 10 * 3600) < 60


def test_ttl_beyond_the_ceiling_is_refused_not_clamped():
    # Silently clamping would leave the caller planning around a date that
    # isn't real — so this is a 400, not a shrug.
    r = client.post("/api/rooms", json={"description": "forever", "ttl_hours": ttl.MAX_HOURS + 1})
    assert r.status_code == 400
    assert "maximum lifetime" in r.json()["detail"]


def test_ttl_in_the_past_is_refused():
    when = utcnow() - timedelta(hours=1)
    r = client.post("/api/rooms", json={
        "description": "born dead",
        "expires_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    assert r.status_code == 400


# --- enforcement ------------------------------------------------------------

@pytest.mark.parametrize("method,path_suffix", [
    ("get", ""),
    ("get", "/messages"),
    ("get", "/context"),
])
def test_expired_room_is_gone_for_readers(method, path_suffix):
    room = _create(description="will expire")
    _expire(room["uuid"])
    r = getattr(client, method)(f"/api/rooms/{room['uuid']}{path_suffix}")
    assert r.status_code == 410, r.text
    assert r.json()["detail"].startswith("room_expired:")


def test_expired_room_refuses_writes():
    room = _create(description="write after death")
    _expire(room["uuid"])
    r = client.post(
        f"/api/rooms/{room['uuid']}/messages",
        json={"agent_id": "ghost", "text": "anyone there?"},
    )
    assert r.status_code == 410
    assert "room_expired" in r.json()["detail"]


def test_expired_detail_says_it_is_terminal():
    # Agents decide whether to retry from this string — it has to be explicit,
    # otherwise a polling loop hammers a dead room forever.
    room = _create(description="terminal wording")
    _expire(room["uuid"])
    detail = client.get(f"/api/rooms/{room['uuid']}").json()["detail"]
    assert "do not retry" in detail.lower()


def test_live_room_is_unaffected():
    room = _create(description="still alive", ttl_hours=48)
    assert client.get(f"/api/rooms/{room['uuid']}").status_code == 200
    r = client.post(
        f"/api/rooms/{room['uuid']}/messages",
        json={"agent_id": "a", "text": "hello"},
    )
    assert r.status_code == 201


def test_missing_room_is_still_404_not_410():
    # 404 "never existed" and 410 "ran out" must stay distinguishable.
    r = client.get("/api/rooms/00000000-0000-4000-8000-000000000000")
    assert r.status_code == 404


# --- sliding window ---------------------------------------------------------

def test_posting_extends_the_expiry():
    """The TTL retires silence, not conversations.

    Measured on production before this shipped: 4353 of 4387 rooms had been
    quiet for over 72 hours, but 17 carried a live conversation past that mark
    (the longest ran 19 days). A countdown from creation would have cut all 17
    off mid-sentence.
    """
    room = _create(description="long negotiation", ttl_hours=2)
    before = client.get(f"/api/rooms/{room['uuid']}").json()["expires_in_seconds"]
    assert before < 3 * 3600

    r = client.post(f"/api/rooms/{room['uuid']}/messages",
                    json={"agent_id": "a", "text": "still working on it"})
    assert r.status_code == 201
    after = client.get(f"/api/rooms/{room['uuid']}").json()
    assert after["expires_in_seconds"] > before
    assert abs(after["expires_in_seconds"] - ttl.DEFAULT_HOURS * 3600) < 60


def test_activity_never_shortens_a_longer_expiry():
    # A room deliberately created with a month of runway keeps it.
    room = _create(description="long-lived", ttl_hours=ttl.MAX_HOURS)
    before = client.get(f"/api/rooms/{room['uuid']}").json()["expires_in_seconds"]
    client.post(f"/api/rooms/{room['uuid']}/messages",
                json={"agent_id": "a", "text": "ping"})
    after = client.get(f"/api/rooms/{room['uuid']}").json()["expires_in_seconds"]
    assert after >= before - 60, "posting must never shorten a longer lifetime"


def test_activity_does_not_revive_an_already_expired_room():
    # Extension happens on a successful post; an expired room refuses the post
    # in the first place, so it cannot resurrect itself.
    room = _create(description="too late")
    _expire(room["uuid"])
    r = client.post(f"/api/rooms/{room['uuid']}/messages",
                    json={"agent_id": "a", "text": "hello?"})
    assert r.status_code == 410
    assert client.get(f"/api/rooms/{room['uuid']}").status_code == 410


def test_activity_leaves_pinned_rooms_pinned():
    room = _create(description="pinned")
    with Session(engine) as s:
        r = s.get(Room, room["uuid"])
        r.expires_at = None
        s.add(r)
        s.commit()
    client.post(f"/api/rooms/{room['uuid']}/messages",
                json={"agent_id": "a", "text": "ping"})
    assert client.get(f"/api/rooms/{room['uuid']}").json()["expires_at"] is None


def test_activity_cannot_walk_past_the_ceiling():
    from app.models import utcnow as _now
    from datetime import timedelta as _td
    room = _create(description="at the ceiling", ttl_hours=ttl.MAX_HOURS)
    client.post(f"/api/rooms/{room['uuid']}/messages",
                json={"agent_id": "a", "text": "ping"})
    left = client.get(f"/api/rooms/{room['uuid']}").json()["expires_in_seconds"]
    assert left <= ttl.MAX_HOURS * 3600 + 60


# --- grandfathering ---------------------------------------------------------

def test_null_expiry_never_expires():
    # Rooms created before TTL existed carry NULL and must keep working —
    # retroactively expiring live history would be the worse bug.
    room = _create(description="pre-TTL room")
    with Session(engine) as s:
        r = s.get(Room, room["uuid"])
        r.expires_at = None
        s.add(r)
        s.commit()
    info = client.get(f"/api/rooms/{room['uuid']}")
    assert info.status_code == 200
    assert info.json()["expires_at"] is None
    assert info.json()["expires_in_seconds"] is None


# --- listing ----------------------------------------------------------------

def test_expired_rooms_leave_the_public_listing():
    room = _create(description="public and doomed")
    with Session(engine) as s:
        r = s.get(Room, room["uuid"])
        r.is_public = True
        s.add(r)
        s.commit()
    listed = client.get("/api/rooms").json()
    assert any(x["uuid"] == room["uuid"] for x in listed["rooms"])

    _expire(room["uuid"])
    listed = client.get("/api/rooms").json()
    assert not any(x["uuid"] == room["uuid"] for x in listed["rooms"]), \
        "discovery must not hand out UUIDs that answer 410"


# --- HTML surface -----------------------------------------------------------

def test_room_page_says_expired_not_missing():
    room = _create(description="html view")
    _expire(room["uuid"])
    page = client.get(f"/{room['uuid']}")
    assert page.status_code == 410
    assert "Room expired" in page.text


# --- admin ------------------------------------------------------------------

def _admin_headers(monkeypatch):
    monkeypatch.setattr(_main, "ADMIN_TOKEN", "test-admin-token")
    return {"Authorization": "Bearer test-admin-token"}


def test_admin_can_revive_an_expired_room(monkeypatch):
    headers = _admin_headers(monkeypatch)
    room = _create(description="revive me")
    _expire(room["uuid"])
    assert client.get(f"/api/rooms/{room['uuid']}").status_code == 410

    r = client.post(
        f"/admin/rooms/{room['uuid']}/ttl",
        data={"ttl_hours": "24"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["expired"] is False
    assert client.get(f"/api/rooms/{room['uuid']}").status_code == 200


def test_admin_can_pin_a_room_open(monkeypatch):
    headers = _admin_headers(monkeypatch)
    room = _create(description="pin me", ttl_hours=2)
    r = client.post(
        f"/admin/rooms/{room['uuid']}/ttl",
        data={"ttl_hours": "never"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["expires_at"] is None
    assert client.get(f"/api/rooms/{room['uuid']}").json()["expires_at"] is None


def test_admin_can_expire_a_room_immediately(monkeypatch):
    headers = _admin_headers(monkeypatch)
    room = _create(description="kill me")
    r = client.post(
        f"/admin/rooms/{room['uuid']}/ttl",
        data={"ttl_hours": "now"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["expired"] is True
    assert client.get(f"/api/rooms/{room['uuid']}").status_code == 410


def test_ttl_endpoint_requires_admin():
    room = _create(description="not yours")
    r = client.post(f"/admin/rooms/{room['uuid']}/ttl", data={"ttl_hours": "999"})
    # Unauthenticated admin paths answer 404, same as a nonexistent path.
    assert r.status_code == 404


# The TTL wall is for everyone but the admin: an expired room stays fully
# open to the admin token on the page and the API, while others still get 410.

def test_admin_sees_expired_room_everywhere(monkeypatch):
    headers = _admin_headers(monkeypatch)
    room = _create(description="admin only after death")
    _expire(room["uuid"])
    uid = room["uuid"]
    for path in (f"/api/rooms/{uid}", f"/api/rooms/{uid}/messages", f"/api/rooms/{uid}/context"):
        assert client.get(path, headers=headers).status_code == 200, path
        assert client.get(path).status_code == 410, path
    page = client.get(f"/{uid}", headers=headers)
    assert page.status_code == 200
    assert "EXPIRED · ADMIN VIEW" in page.text
    assert client.get(f"/{uid}").status_code == 410


def test_admin_cookie_reaches_room_pages(monkeypatch):
    monkeypatch.setattr(_main, "ADMIN_TOKEN", "test-admin-token")
    room = _create(description="cookie path")
    _expire(room["uuid"])
    c = TestClient(fastapi_app)
    login = c.post("/admin/login", data={"token": "test-admin-token"}, follow_redirects=False)
    assert login.status_code == 303
    assert "Path=/;" in login.headers["set-cookie"] or login.headers["set-cookie"].rstrip().endswith("Path=/")
    c.cookies.set(_main.ADMIN_COOKIE, "test-admin-token")
    assert c.get(f"/{room['uuid']}").status_code == 200


# --- MCP: same rule as REST -------------------------------------------------

def _mcp_call(c, tool: str, args: dict, headers: dict) -> dict:
    h = {"Accept": "application/json, text/event-stream",
         "Content-Type": "application/json", **headers}
    init = c.post("/mcp", headers=h, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}})
    assert init.status_code == 200, init.text
    sid = init.headers.get("mcp-session-id")
    if sid:
        h["mcp-session-id"] = sid
    c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = c.post("/mcp", headers=h, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": args}})
    assert r.status_code == 200, r.text
    body = r.text
    if body.lstrip().startswith("event:") or "data:" in body[:20]:
        body = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")][-1]
    import json as _json
    return _json.loads(body)["result"]


def test_mcp_expired_room_open_to_admin_only(monkeypatch):
    # One `with` block: the MCP session manager's lifespan runs once per process.
    import app.mcp_server as _mcp
    monkeypatch.setattr(_mcp, "engine", engine)  # MCP opens its own sessions
    monkeypatch.setattr(_main, "ADMIN_TOKEN", "test-admin-token")
    room = _create(description="mcp after death")
    _expire(room["uuid"])
    uid = {"uuid": room["uuid"]}
    with TestClient(fastapi_app, base_url="http://localhost") as c:
        anon = _mcp_call(c, "get_room", uid, {})
        assert anon.get("isError") and "410" in anon["content"][0]["text"]
        for admin_headers in ({"Authorization": "Bearer test-admin-token"},
                              {"X-Roomcomm-Admin": "test-admin-token"}):
            for tool in ("get_room", "read_messages"):
                r = _mcp_call(c, tool, uid, admin_headers)
                assert not r.get("isError"), (tool, admin_headers, r)

        # Keyed read of a room WITH messages: the inbox watermark commit used
        # to detach the rows before serialization (DetachedInstanceError).
        live = _create(description="keyed mcp read")
        client.post(f"/api/rooms/{live['uuid']}/messages",
                    json={"agent_id": "a", "text": "hello"})
        key = client.post("/api/keys", json={"agent_id": "reader"}).json()["key"]
        for headers in ({"Authorization": f"Bearer {key}"},
                        {"Authorization": f"Bearer {key}", "X-Roomcomm-Admin": "test-admin-token"}):
            r = _mcp_call(c, "read_messages", {"uuid": live["uuid"]}, headers)
            assert not r.get("isError"), r
            assert "hello" in r["content"][0]["text"]

        # A wrong admin token over MCP is refused and counts as ONE failed
        # guess per HTTP request, same as REST.
        _main._admin_fail_buckets.clear()
        bad = {"X-Real-IP": "203.0.113.9", "X-Roomcomm-Admin": "wrong"}
        r = _mcp_call(c, "get_room", uid, bad)
        assert r.get("isError") and "410" in r["content"][0]["text"]
        # initialize + initialized + tools/call = 3 HTTP requests
        assert len(_main._admin_fail_buckets["203.0.113.9"]) == 3
        _main._admin_fail_buckets.clear()


# --- admin brute-force lockout ------------------------------------------------

@pytest.fixture
def _fresh_admin_fails():
    _main._admin_fail_buckets.clear()
    yield
    _main._admin_fail_buckets.clear()


def test_wrong_admin_guesses_lock_the_ip_out(monkeypatch, _fresh_admin_fails):
    monkeypatch.setattr(_main, "ADMIN_TOKEN", "test-admin-token")
    room = _create(description="lockout")
    _expire(room["uuid"])
    path = f"/api/rooms/{room['uuid']}"
    ip = {"X-Real-IP": "203.0.113.7"}
    for i in range(_main.ADMIN_FAIL_LIMIT):
        assert client.get(path, headers={**ip, "X-Roomcomm-Admin": f"guess{i}"}).status_code == 410
    # One request = one failure, not one per check along the way.
    assert len(_main._admin_fail_buckets["203.0.113.7"]) == _main.ADMIN_FAIL_LIMIT
    # Locked out: even the right token no longer works from this IP ...
    assert client.get(path, headers={**ip, "X-Roomcomm-Admin": "test-admin-token"}).status_code == 410
    assert client.post("/admin/login", data={"token": "test-admin-token"},
                       headers=ip, follow_redirects=False).status_code == 404
    # ... while ordinary traffic from it and admin from elsewhere are fine.
    assert client.get(path, headers=ip).status_code == 410
    assert client.get(path, headers={"X-Real-IP": "198.51.100.1",
                                     "X-Roomcomm-Admin": "test-admin-token"}).status_code == 200


def test_agent_keys_are_not_admin_guesses(monkeypatch, _fresh_admin_fails):
    monkeypatch.setattr(_main, "ADMIN_TOKEN", "test-admin-token")
    key = client.post("/api/keys", json={"agent_id": "plain"}).json()["key"]
    room = _create(description="keyed")
    for _ in range(_main.ADMIN_FAIL_LIMIT + 2):
        client.get(f"/api/rooms/{room['uuid']}/messages",
                   headers={"X-Real-IP": "203.0.113.8", "Authorization": f"Bearer {key}"})
    assert not _main._admin_fail_buckets.get("203.0.113.8")
