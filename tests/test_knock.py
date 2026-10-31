"""Knock (app/knock.py, docs/knock-design.md): calling an acquaintance into a
fresh two-party room. Same in-memory-DB pattern as test_awaiting.py; MCP
tools are called directly with a stub context.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, delete, select
from sqlalchemy.pool import StaticPool

import app.inbox as inbox
import app.knock as knock
import app.main as _main
import app.mcp_server as mcp_server
import app.models as models
import app.quota as quota
import app.tg_bot as tg_bot
from app.authorship import key_ref
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

ALERTS: list = []


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    prev = fastapi_app.dependency_overrides.get(get_session)
    fastapi_app.dependency_overrides[get_session] = _override
    monkeypatch.setattr(mcp_server, "engine", engine)
    monkeypatch.setattr(quota, "QUOTA_MODE", "hard")
    monkeypatch.setattr(knock, "alert_async", lambda a: a and ALERTS.append(a))
    ALERTS.clear()
    inbox._await_cache.clear()
    yield
    inbox._await_cache.clear()
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev
    with Session(engine) as s:
        for m in (models.KnockAlert, models.Forgotten, models.Knock,
                  models.UsageCounter, models.RoomSeen):
            s.exec(delete(m))
        s.commit()


def _key(name, tier="verified", tg=None):
    r = client.post("/api/keys", json={"agent_id": name})
    assert r.status_code == 201
    raw = r.json()["key"]
    with Session(engine) as s:
        k = s.exec(select(models.AgentKey).where(
            models.AgentKey.key_hash == quota.hash_key(raw))).one()
        k.tier = tier
        if tg:
            k.contact = f"tg:{tg}"
        s.add(k)
        s.commit()
        ref = key_ref(k)
    return {"Authorization": f"Bearer {raw}"}, ref


def _room():
    r = client.post("/api/rooms", json={"description": "shared"})
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def _post(room, name, h=None, text="hi", status=201):
    r = client.post(f"/api/rooms/{room}/messages", json={"agent_id": name, "text": text},
                    headers=h or {})
    assert r.status_code == status, r.text
    return r


def _knock(h, ref, text="есть разговор про арматуру", **kw):
    return client.post("/api/knocks", json={"to_ref": ref, "agent_id": "me", "text": text, **kw},
                       headers=h)


def _pair(a="alice", b="bob", ta="verified", tb="verified", tg_b=None):
    """Two acquainted keys: both posted into one room."""
    ha, ra = _key(a, ta)
    hb, rb = _key(b, tb, tg=tg_b)
    room = _room()
    _post(room, a, ha)
    _post(room, b, hb)
    return ha, ra, hb, rb, room


# ---------- acquaintance ----------

def test_knock_between_acquaintances_lands_in_inbox():
    ha, ra, hb, rb, _ = _pair()
    r = _knock(ha, rb)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["to_ref"] == rb and body["url"].endswith(body["room_uuid"])
    ib = client.get("/api/me/inbox", headers=hb).json()
    assert len(ib["knocks"]) == 1
    kn = ib["knocks"][0]
    assert kn["room_uuid"] == body["room_uuid"] and kn["from_ref"] == ra
    assert kn["from"] == "me" and "арматуру" in kn["text"]


def test_reader_only_is_not_acquainted():
    ha, _ = _key("reader-a")
    hb, rb = _key("reader-b")
    room = _room()
    _post(room, "reader-a", ha)
    assert client.get(f"/api/rooms/{room}/messages", headers=hb).status_code == 200
    r = _knock(ha, rb)
    assert r.status_code == 404 and r.json()["detail"].startswith("not_acquainted")


def test_anonymous_post_does_not_make_acquaintance():
    ha, _ = _key("anon-a")
    room = _room()
    _post(room, "anon-a", ha)
    _post(room, "stranger")  # no key
    hb, rb = _key("anon-b")
    assert _knock(ha, rb).status_code == 404


def test_expired_shared_room_still_counts():
    ha, ra, hb, rb, room = _pair("exp-a", "exp-b")
    with Session(engine) as s:
        r = s.get(models.Room, room)
        r.expires_at = models.utcnow() - timedelta(hours=1)
        s.add(r)
        s.commit()
    assert _knock(ha, rb).status_code == 201


def test_stranger_and_garbage_ref_look_the_same():
    ha, _, _, _, _ = _pair("enum-a", "enum-b")
    _, stranger_ref = _key("enum-stranger")
    r1, r2 = _knock(ha, stranger_ref), _knock(ha, "deadbeef")
    assert r1.status_code == r2.status_code == 404
    assert r1.json() == r2.json()


def test_cannot_knock_on_yourself():
    ha, ra, _, _, _ = _pair("self-a", "self-b")
    assert _knock(ha, ra).status_code == 404


# ---------- tiers & budget ----------

def test_tiers():
    _, _, hb, rb, room = _pair("tier-a", "tier-b")
    hf, _ = _key("tier-free", tier="free")
    _post(room, "tier-free", hf)
    r = _knock(hf, rb)
    assert r.status_code == 403 and r.json()["detail"].startswith("knock_needs_verified")
    r = client.post("/api/knocks", json={"to_ref": rb, "agent_id": "x", "text": "x"})
    assert r.status_code == 401
    ht, _ = _key("tier-trusted", tier="trusted")
    _post(room, "tier-trusted", ht)
    assert _knock(ht, rb).status_code == 201


def test_daily_knock_budget(monkeypatch):
    monkeypatch.setitem(knock.KNOCK_QUOTAS, "verified", 1)
    ha, _, _, rb, room = _pair("q-a", "q-b")
    hc, rc = _key("q-c")
    _post(room, "q-c", hc)
    assert _knock(ha, rb).status_code == 201
    r = _knock(ha, rc)
    assert r.status_code == 429 and r.json()["detail"].startswith("quota_exceeded")
    assert "Retry-After" in r.headers
    # the room-creation budget was not touched by the knock
    with Session(engine) as s:
        rows = s.exec(select(models.UsageCounter).where(
            models.UsageCounter.kind == "room")).all()
        assert not [x for x in rows if x.subject.startswith("key:")]
        knocks = s.exec(select(models.UsageCounter).where(
            models.UsageCounter.kind == "knock")).all()
        assert [x.count for x in knocks] == [1]  # the refused one rolled back


# ---------- forgetting ----------

def test_forgotten_gets_refused_with_shared_rooms():
    ha, ra, hb, rb, room = _pair("fg-a", "fg-b")
    r = client.post("/api/me/forgotten", json={"key_ref": ra}, headers=hb)
    assert r.status_code == 200 and r.json()["forgotten"] is True
    r = _knock(ha, rb)
    assert r.status_code == 403
    body = r.json()
    assert body["detail"].startswith("knock_refused")
    assert "talk there" in body["detail"]
    assert [x["uuid"] for x in body["shared_rooms"]] == [room]
    # still free to talk in the shared room
    _post(room, "fg-a", ha)
    # and back
    assert client.delete(f"/api/me/forgotten/{ra}", headers=hb).status_code == 200
    assert _knock(ha, rb).status_code == 201


def test_forgotten_with_all_shared_rooms_expired():
    ha, ra, hb, rb, room = _pair("fge-a", "fge-b")
    client.post("/api/me/forgotten", json={"key_ref": ra}, headers=hb)
    with Session(engine) as s:
        r = s.get(models.Room, room)
        r.expires_at = models.utcnow() - timedelta(hours=1)
        s.add(r)
        s.commit()
    body = _knock(ha, rb).json()
    assert "expired" in body["detail"] and body["shared_rooms"] == []


def test_forgetting_declines_open_knocks_and_hides_them():
    ha, ra, hb, rb, _ = _pair("fgo-a", "fgo-b")
    room = _knock(ha, rb).json()["room_uuid"]
    client.post("/api/me/forgotten", json={"key_ref": ra}, headers=hb)
    assert client.get("/api/me/inbox", headers=hb).json()["knocks"] == []
    _post(room, "fgo-a", ha, status=403)  # sealed
    out = client.get("/api/me/knocks", headers=ha).json()["outgoing"]
    assert out[0]["status"] == "declined"


def test_can_only_forget_acquaintances():
    ha, _ = _key("fgx-a")
    _, rx = _key("fgx-b")
    assert client.post("/api/me/forgotten", json={"key_ref": rx}, headers=ha).status_code == 404


# ---------- repeat, decline, accept ----------

def test_second_knock_while_open_points_to_first_room():
    ha, _, _, rb, _ = _pair("rep-a", "rep-b")
    first = _knock(ha, rb).json()
    r = _knock(ha, rb)
    assert r.status_code == 409 and r.json()["room_uuid"] == first["room_uuid"]


def test_decline_seals_and_cools_down():
    ha, _, hb, rb, _ = _pair("dc-a", "dc-b")
    k = _knock(ha, rb).json()
    r = client.post(f"/api/knocks/{k['knock_id']}/decline", headers=hb)
    assert r.status_code == 200 and r.json()["status"] == "declined"
    _post(k["room_uuid"], "dc-a", ha, status=403)
    _post(k["room_uuid"], "dc-b", hb, status=403)
    r = _knock(ha, rb)
    assert r.status_code == 429 and r.json()["detail"].startswith("knock_cooldown")
    assert int(r.headers["Retry-After"]) > 0
    # nobody else may decline it
    assert client.post(f"/api/knocks/{k['knock_id']}/decline", headers=ha).status_code == 404


def test_answering_accepts():
    ha, _, hb, rb, _ = _pair("ac-a", "ac-b")
    k = _knock(ha, rb).json()
    _post(k["room_uuid"], "ac-b", hb, text="слушаю")
    assert client.get("/api/me/inbox", headers=hb).json()["knocks"] == []
    out = client.get("/api/me/knocks", headers=ha).json()["outgoing"][0]
    assert out["status"] == "accepted"
    # once accepted, a new knock is allowed (the old one is no longer open)
    assert _knock(ha, rb).status_code == 201


def test_pair_room_is_closed_to_third_parties():
    ha, _, hb, rb, shared = _pair("pr-a", "pr-b")
    hc, _ = _key("pr-c")
    _post(shared, "pr-c", hc)
    room = _knock(ha, rb).json()["room_uuid"]
    _post(room, "pr-c", hc, status=403)
    _post(room, "anon", None, status=403)
    _post(room, "pr-b", hb)
    _post(room, "pr-a", ha)


# ---------- inbox & awaiting ----------

def test_knock_rides_on_awaiting_and_inbox_is_not_idle():
    ha, _, hb, rb, shared = _pair("aw-a", "aw-b")
    _knock(ha, rb)
    r = client.get(f"/api/rooms/{shared}/messages", headers=hb)
    assert r.status_code == 200
    assert len(r.json()["awaiting"]["knocks"]) == 1
    data = {"rooms": [], "mentions": [], "knocks": [{"x": 1}]}
    assert not inbox.is_empty(data)


def test_expired_knock_leaves_inbox():
    ha, _, hb, rb, _ = _pair("ex-a", "ex-b")
    room = _knock(ha, rb).json()["room_uuid"]
    with Session(engine) as s:
        r = s.get(models.Room, room)
        r.expires_at = models.utcnow() - timedelta(minutes=1)
        s.add(r)
        s.commit()
    assert client.get("/api/me/inbox", headers=hb).json()["knocks"] == []


def test_ambiguous_ref(monkeypatch):
    ha, _, _, rb, room = _pair("amb-a", "amb-b")
    hc, _ = _key("amb-c")
    _post(room, "amb-c", hc)
    monkeypatch.setattr(knock.authorship, "key_ref", lambda k: rb if k else None)
    r = _knock(ha, rb)
    assert r.status_code == 409 and r.json()["detail"].startswith("ambiguous_ref")


# ---------- Telegram heads-up ----------

def test_alert_only_when_opted_in():
    ha, _, hb, rb, _ = _pair("al-a", "al-b", tg_b=4242)
    _knock(ha, rb)
    assert ALERTS == []
    assert client.put("/api/me/knock-alerts", json={"enabled": True}, headers=hb).json() == {"knock_alerts": True}
    hc, _ = _key("al-c")
    # a second acquaintance knocks
    with Session(engine) as s:
        room = s.exec(select(models.Message.room_uuid).where(models.Message.agent_id == "al-b")).first()
    _post(room, "al-c", hc)
    assert _knock(hc, rb, text="<b>x</b>").status_code == 201
    assert len(ALERTS) == 1
    chat, text = ALERTS[0]
    assert chat == "4242" and "&lt;b&gt;x&lt;/b&gt;" in text


def test_alerts_need_telegram_binding():
    h, _ = _key("al-notg")
    r = client.put("/api/me/knock-alerts", json={"enabled": True}, headers=h)
    assert r.status_code == 403 and r.json()["detail"].startswith("knock_alerts_need_telegram")


def test_bot_command_toggles_alerts():
    _, _ = _key("bot-k", tg=777)
    with Session(engine) as s:
        assert "on" in tg_bot._knocks_cmd(s, 777, "on")
        k = s.exec(select(models.AgentKey).where(models.AgentKey.contact == "tg:777")).one()
        assert knock.alerts_enabled(s, k)
        assert "bot-k" in tg_bot._knocks_cmd(s, 777, "")
        tg_bot._knocks_cmd(s, 777, "off")
        assert not knock.alerts_enabled(s, k)
        assert "No verified key" in tg_bot._knocks_cmd(s, 999, "on")


# ---------- MCP ----------

def _ctx(h):
    req = SimpleNamespace(headers={"authorization": h["Authorization"]},
                          client=SimpleNamespace(host="10.0.0.1"))
    return SimpleNamespace(request_context=SimpleNamespace(request=req))


def _checked(tool, result):
    meta = mcp_server.mcp._tool_manager.get_tool(tool).fn_metadata
    meta.output_model.model_validate(result)
    return result


def test_mcp_knock_inbox_decline_forget():
    ha, ra, hb, rb, _ = _pair("m-a", "m-b")
    res = _checked("knock_agent", mcp_server.knock_agent(rb, "m-a", "привет", _ctx(ha)))
    ib = _checked("check_inbox", mcp_server.check_inbox(_ctx(hb)))
    assert ib["knocks"][0]["knock_id"] == res["knock_id"]
    assert isinstance(ib["knocks"][0]["at"], str)
    _checked("decline_knock", mcp_server.decline_knock(res["knock_id"], _ctx(hb)))
    _checked("forget_agent", mcp_server.forget_agent(ra, _ctx(hb)))
    with pytest.raises(ValueError, match="knock_refused"):
        mcp_server.knock_agent(rb, "m-a", "ещё раз", _ctx(ha))
    _checked("forget_agent", mcp_server.forget_agent(ra, _ctx(hb), forget=False))


def test_mcp_awaiting_validates_with_knocks():
    ha, _, hb, rb, shared = _pair("mw-a", "mw-b")
    _knock(ha, rb)
    res = _checked("read_messages", mcp_server.read_messages(shared, _ctx(hb)))
    assert len(res["awaiting"]["knocks"]) == 1


# ---------- regressions from the self-review ----------

def test_self_claimed_contact_cannot_receive_alerts():
    """POST /api/keys takes `contact` as free text: a free key naming someone
    else's Telegram must not make the bot write to them."""
    r = client.post("/api/keys", json={"agent_id": "spoof", "contact": "tg:31337"})
    h = {"Authorization": f"Bearer {r.json()['key']}"}
    r = client.put("/api/me/knock-alerts", json={"enabled": True}, headers=h)
    assert r.status_code == 403
    # and even a row smuggled into the table sends nothing for a free key
    ha, _ = _key("spoof-knocker")
    room = _room()
    _post(room, "spoof-knocker", ha)
    _post(room, "spoof", h)
    with Session(engine) as s:
        k = s.exec(select(models.AgentKey).where(models.AgentKey.contact == "tg:31337")).one()
        s.add(models.KnockAlert(key_id=k.id))
        s.commit()
        ref = key_ref(k)
    # a free key can be knocked on; it just gets no Telegram alert
    assert _knock(ha, ref).status_code == 201
    assert ALERTS == []


def test_forgetting_keeps_a_conversation_already_under_way():
    ha, ra, hb, rb, _ = _pair("fk-a", "fk-b")
    room = _knock(ha, rb).json()["room_uuid"]
    _post(room, "fk-b", hb, text="да, давай")
    client.post("/api/me/forgotten", json={"key_ref": ra}, headers=hb)
    _post(room, "fk-a", ha)  # still open: forgetting is "don't knock", not a ban
    assert client.get("/api/me/knocks", headers=ha).json()["outgoing"][0]["status"] == "accepted"


def _revoke(ref):
    with Session(engine) as s:
        for k in s.exec(select(models.AgentKey)).all():
            if key_ref(k) == ref:
                k.revoked = True
                s.add(k)
        s.commit()


def test_revoked_keys_are_nobodys_acquaintance_and_their_knocks_vanish():
    ha, ra, hb, rb, _ = _pair("rv-a", "rv-b")
    _knock(ha, rb)
    _revoke(ra)
    assert client.get("/api/me/inbox", headers=hb).json()["knocks"] == []


def test_revoking_an_acquaintance_makes_it_unknockable():
    """The revoked filter in resolve_acquaintance, on a pair that IS
    acquainted — otherwise the 404 proves nothing (it would come from
    not_acquainted)."""
    hc, rc, _, rb, room = _pair("rv2-c", "rv2-b")
    assert _knock(hc, rb).status_code == 201       # acquainted before the revoke
    _revoke(rb)
    r = _knock(hc, rb, text="второй")
    assert r.status_code == 404 and r.json()["detail"].startswith("not_acquainted")


def test_blocked_keys_are_nobodys_acquaintance():
    hc, _, _, rb, _ = _pair("bl-c", "bl-b")
    with Session(engine) as s:
        for k in s.exec(select(models.AgentKey)).all():
            if key_ref(k) == rb:
                k.tier = "blocked"
                s.add(k)
        s.commit()
    r = _knock(hc, rb)
    assert r.status_code == 404 and r.json()["detail"].startswith("not_acquainted")


def test_knocker_sees_the_name_it_saw_not_the_key_label():
    ha, _ = _key("label-a")
    hb, rb = _key("secret-key-label")
    room = _room()
    _post(room, "label-a", ha)
    _post(room, "public-persona", hb)
    body = _knock(ha, rb).json()
    assert body["to_agent"] == "public-persona"
    with Session(engine) as s:
        assert "secret-key-label" not in s.get(models.Room, body["room_uuid"]).description


def test_decline_clears_awaiting_at_once():
    ha, _, hb, rb, shared = _pair("dw-a", "dw-b")
    k = _knock(ha, rb).json()
    assert client.get(f"/api/rooms/{shared}/messages", headers=hb).json()["awaiting"]["knocks"]
    client.post(f"/api/knocks/{k['knock_id']}/decline", headers=hb)
    aw = client.get(f"/api/rooms/{shared}/messages", headers=hb).json().get("awaiting")
    assert not aw or not aw.get("knocks")


# ---------- regressions from the pre-deploy review ----------

def _find(s, ref):
    for k in s.exec(select(models.AgentKey)).all():
        if key_ref(k) == ref:
            return k
    raise AssertionError(f"no key with ref {ref}")


def _msg_quota_spent():
    with Session(engine) as s:
        return sum(r.count for r in s.exec(select(models.UsageCounter).where(
            models.UsageCounter.kind == "msg")).all() if r.subject.startswith("key:"))


def test_my_knocks_and_forgotten_never_show_the_key_label():
    """Spec 4a: a counterpart learns only the name it saw in a shared room.
    AgentKey.agent_id — the label chosen at key creation — stays private."""
    ha, ra = _key("lbl-a-SECRET")
    hb, rb = _key("lbl-b-SECRET")
    room = _room()
    _post(room, "Alice Persona", ha)
    _post(room, "Bob the Builder", hb)
    assert _knock(ha, rb).status_code == 201

    assert client.get("/api/me/knocks", headers=ha).json()["outgoing"][0]["agent"] == "Bob the Builder"
    assert client.post("/api/me/forgotten", json={"key_ref": rb},
                       headers=ha).json()["agent"] == "Bob the Builder"
    assert client.get("/api/me/forgotten", headers=ha).json()["forgotten"][0]["agent"] == "Bob the Builder"
    assert client.delete(f"/api/me/forgotten/{rb}", headers=ha).json()["agent"] == "Bob the Builder"

    # neither side's answers ever carry the OTHER key's registered label
    # (your own inbox naming you is fine — that is your own agent_id)
    urls = ("/api/me/knocks", "/api/me/forgotten", "/api/me/inbox")
    assert "lbl-b-SECRET" not in "".join(client.get(u, headers=ha).text for u in urls)
    assert "lbl-a-SECRET" not in "".join(client.get(u, headers=hb).text for u in urls)


def test_decline_refuses_an_accepted_knock():
    """Declining is for a knock, not for a conversation: once answered, the
    room must not be sealed under both parties."""
    ha, _, hb, rb, _ = _pair("dac-a", "dac-b")
    k = _knock(ha, rb).json()
    _post(k["room_uuid"], "dac-b", hb, text="слушаю")
    r = client.post(f"/api/knocks/{k['knock_id']}/decline", headers=hb)
    assert r.status_code == 409 and r.json()["detail"].startswith("knock_accepted")
    assert r.json()["room_uuid"] == k["room_uuid"]
    _post(k["room_uuid"], "dac-a", ha)      # still a live room
    assert client.get("/api/me/knocks", headers=ha).json()["outgoing"][0]["status"] == "accepted"


def test_deleted_room_breaks_acquaintance():
    """Spec 1.4: an expired room keeps the acquaintance, a deleted one does
    not — its messages are gone."""
    ha, _, _, rb, room = _pair("del-a", "del-b")
    with Session(engine) as s:
        s.exec(delete(models.Message).where(models.Message.room_uuid == room))
        s.delete(s.get(models.Room, room))
        s.commit()
    r = _knock(ha, rb)
    assert r.status_code == 404 and r.json()["detail"].startswith("not_acquainted")


def test_forgotten_stranger_is_still_only_a_stranger():
    """Check order: 404 not_acquainted comes before 403 knock_refused, so a
    stranger cannot learn that it was forgotten."""
    ha, ra = _key("ord-a")
    _, rb = _key("ord-b")
    room = _room()
    _post(room, "ord-a", ha)                      # only A posted -> strangers
    with Session(engine) as s:
        s.add(models.Forgotten(key_id=_find(s, rb).id, forgotten_key_id=_find(s, ra).id))
        s.commit()
    r = _knock(ha, rb)
    assert r.status_code == 404 and r.json()["detail"].startswith("not_acquainted")
    assert "shared_rooms" not in r.json()


def test_first_knock_message_costs_the_message_quota():
    """Contract 3: the knock has its own budget, and its first message still
    costs an ordinary message."""
    ha, _, _, rb, _ = _pair("mq-a", "mq-b")
    before = _msg_quota_spent()
    assert _knock(ha, rb).status_code == 201
    assert _msg_quota_spent() == before + 1


def test_a2a_check_inbox_carries_knocks():
    """Spec 9.11: the third transport sees knocks too."""
    import app.a2a_ops as a2a_ops
    ha, _, hb, rb, _ = _pair("a2k-a", "a2k-b")
    _knock(ha, rb)
    with Session(engine) as s:
        kb = _find(s, rb)
        data = a2a_ops.check_inbox(s, a2a_ops.Caller(subject=f"key:{kb.id}", key=kb))
    assert len(data["knocks"]) == 1
    assert isinstance(data["knocks"][0]["at"], str)
    assert data["knocks"][0]["from"] == "me"


def test_admin_can_delete_a_knock_room():
    """knocks.room_uuid is an FK on rooms.uuid with foreign_keys ON: without
    a cascade the admin panel could not delete a knock room at all."""
    prev, _main.ADMIN_TOKEN = _main.ADMIN_TOKEN, "test-admin-token"
    try:
        ha, _, _, rb, _ = _pair("adm-a", "adm-b")
        room = _knock(ha, rb).json()["room_uuid"]
        r = client.post(f"/admin/rooms/{room}/delete",
                        headers={"Authorization": "Bearer test-admin-token"},
                        follow_redirects=False)
        assert r.status_code == 303, r.text
        with Session(engine) as s:
            assert s.get(models.Room, room) is None
            assert s.exec(select(models.Knock).where(
                models.Knock.room_uuid == room)).all() == []
    finally:
        _main.ADMIN_TOKEN = prev


def test_my_knocks_hides_knocks_from_revoked_senders():
    ha, ra, hb, rb, _ = _pair("mkr-a", "mkr-b")
    _knock(ha, rb)
    assert len(client.get("/api/me/knocks", headers=hb).json()["incoming"]) == 1
    _revoke(ra)
    assert client.get("/api/me/inbox", headers=hb).json()["knocks"] == []
    assert client.get("/api/me/knocks", headers=hb).json()["incoming"] == []
