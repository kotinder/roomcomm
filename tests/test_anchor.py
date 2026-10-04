"""External timestamp anchoring.

The property under test is the one the README used to disclaim: after a root is
published, editing a room's history has to also change a value someone else
already holds. So the tests care about two things — a proof that verifies, and
a proof that stops verifying the moment the history is touched.
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app import anchor, quota
from app.database import get_session
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

import app.main as _main  # noqa: E402

_main.ROOM_CREATE_LIMIT = 10_000


@pytest.fixture(autouse=True)
def _anchor_test_env():
    prev = fastapi_app.dependency_overrides.get(get_session)
    prev_mode = quota.QUOTA_MODE
    fastapi_app.dependency_overrides[get_session] = _override
    quota.QUOTA_MODE = "soft"
    yield
    quota.QUOTA_MODE = prev_mode
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev


def _room(description="anchored room") -> str:
    r = client.post("/api/rooms", json={"description": description})
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def _say(uuid: str, agent_id: str, text: str) -> None:
    r = client.post(f"/api/rooms/{uuid}/messages",
                    json={"agent_id": agent_id, "text": text})
    assert r.status_code == 201, r.text


# --- digest -----------------------------------------------------------------

def test_digest_is_stable_for_unchanged_history():
    uuid = _room()
    _say(uuid, "a", "first")
    _say(uuid, "b", "second")
    with Session(engine) as s:
        first = anchor.room_digest(s, uuid)
        second = anchor.room_digest(s, uuid)
    assert first == second


def test_digest_changes_when_a_message_is_edited():
    uuid = _room()
    _say(uuid, "a", "the deadline is the 20th")
    with Session(engine) as s:
        before = anchor.room_digest(s, uuid)
        msg = s.exec(
            __import__("sqlmodel").select(Message).where(Message.room_uuid == uuid)
        ).first()
        msg.text = "the deadline is the 27th"
        s.add(msg)
        s.commit()
        after = anchor.room_digest(s, uuid)
    assert before != after, "editing a message must change the room digest"


def test_digest_changes_when_a_message_is_deleted():
    uuid = _room()
    _say(uuid, "a", "inconvenient statement")
    _say(uuid, "b", "noted")
    with Session(engine) as s:
        before = anchor.room_digest(s, uuid)
        msg = s.exec(
            __import__("sqlmodel").select(Message).where(Message.room_uuid == uuid)
        ).first()
        s.delete(msg)
        s.commit()
        after = anchor.room_digest(s, uuid)
    assert before != after


def test_rooms_do_not_share_a_digest():
    a, b = _room("one"), _room("two")
    _say(a, "x", "same text")
    _say(b, "x", "same text")
    with Session(engine) as s:
        # Identical content, different rooms — the UUID is inside the digest.
        assert anchor.room_digest(s, a) != anchor.room_digest(s, b)


# --- merkle tree ------------------------------------------------------------

def test_proof_verifies_against_the_root():
    for i in range(5):
        uuid = _room(f"room {i}")
        _say(uuid, "a", f"message {i}")
    with Session(engine) as s:
        leaves = anchor.collect_leaves(s)
        root = anchor.merkle_root(leaves)
        for room_uuid, digest in leaves:
            path = anchor.inclusion_proof(leaves, room_uuid)
            assert anchor.verify_proof(room_uuid, digest, path, root), \
                f"inclusion proof failed for {room_uuid}"


@pytest.mark.parametrize("room_count", [1, 2, 3, 4, 7, 8, 9])
def test_proof_verifies_at_any_tree_shape(room_count):
    # Odd counts exercise the carry-up branch, where a node has no sibling.
    leaves = [(f"{i:032x}", f"digest-{i}") for i in range(room_count)]
    leaves = sorted(leaves)
    root = anchor.merkle_root(leaves)
    for room_uuid, digest in leaves:
        path = anchor.inclusion_proof(leaves, room_uuid)
        assert anchor.verify_proof(room_uuid, digest, path, root)


def test_proof_fails_for_a_tampered_digest():
    leaves = sorted((f"{i:032x}", f"digest-{i}") for i in range(6))
    root = anchor.merkle_root(leaves)
    room_uuid, digest = leaves[2]
    path = anchor.inclusion_proof(leaves, room_uuid)
    assert not anchor.verify_proof(room_uuid, "digest-forged", path, root)


def test_root_changes_when_any_room_changes():
    a = _room("first")
    _room("second")
    _say(a, "x", "original")
    with Session(engine) as s:
        before = anchor.merkle_root(anchor.collect_leaves(s))
    _say(a, "x", "appended later")
    with Session(engine) as s:
        after = anchor.merkle_root(anchor.collect_leaves(s))
    assert before != after


def test_unknown_room_has_no_proof():
    leaves = sorted((f"{i:032x}", f"digest-{i}") for i in range(4))
    assert anchor.inclusion_proof(leaves, "not-a-room") is None


# --- the actual claim -------------------------------------------------------

def test_published_root_detects_rewritten_history():
    """The whole point: once a root is out, an edit cannot hide.

    Simulates a server-side rewrite — the kind the hash chain alone cannot
    catch, because whoever owns the box can re-sign the chain over the new
    rows. The published root was computed before the edit and is not theirs
    to re-issue.
    """
    uuid = _room("contract negotiation")
    _say(uuid, "buyer", "we agree to 100 units at 5 each")
    _say(uuid, "seller", "confirmed")

    with Session(engine) as s:
        row = anchor.create(s, receipt="https://t.me/example/1", published_via="telegram")
        published_root = row.root
        leaves_then = anchor.collect_leaves(s)
        digest_then = dict(leaves_then)[uuid]
        proof_then = anchor.inclusion_proof(leaves_then, uuid)

    # Proof checks out before anyone touches anything.
    assert anchor.verify_proof(uuid, digest_then, proof_then, published_root)

    # Now the operator quietly rewrites the agreed price.
    with Session(engine) as s:
        msg = s.exec(
            __import__("sqlmodel").select(Message)
            .where(Message.room_uuid == uuid)
            .order_by(Message.id.asc())
        ).first()
        msg.text = "we agree to 100 units at 50 each"
        s.add(msg)
        s.commit()
        digest_now = anchor.room_digest(s, uuid)
        leaves_now = anchor.collect_leaves(s)
        root_now = anchor.merkle_root(leaves_now)

    assert digest_now != digest_then
    assert root_now != published_root, \
        "a rewritten room must not reproduce the published root"
    assert not anchor.verify_proof(uuid, digest_now, proof_then, published_root)


# --- API --------------------------------------------------------------------

def test_anchor_endpoint_reports_no_anchor_yet():
    uuid = _room("fresh server")
    body = client.get(f"/api/rooms/{uuid}/anchor").json()
    assert body["current_digest"]
    if body["anchor"] is None:
        assert "no anchor" in body["note"]


def test_anchor_endpoints_expose_root_and_proof():
    uuid = _room("published room")
    _say(uuid, "a", "hello")
    with Session(engine) as s:
        anchor.create(s, receipt="https://t.me/example/2", published_via="telegram")

    listing = client.get("/api/anchors").json()
    assert listing["anchors"], "the anchor should be listed"
    newest = listing["anchors"][0]
    assert newest["published"] is True
    assert newest["receipt"] == "https://t.me/example/2"
    assert listing["arbiter_pubkey"]

    body = client.get(f"/api/rooms/{uuid}/anchor").json()
    assert body["anchor"]["root"] == newest["root"]
    assert body["unchanged_since_anchor"] is True
    assert body["anchored_digest"] == body["current_digest"]
    assert anchor.verify_proof(
        uuid, body["anchored_digest"], body["proof"], body["anchor"]["root"]
    )


def test_anchor_endpoint_flags_state_drift_since_the_anchor():
    uuid = _room("drifting room")
    _say(uuid, "a", "before the anchor")
    with Session(engine) as s:
        anchor.create(s, receipt="https://t.me/example/3", published_via="telegram")
    _say(uuid, "a", "after the anchor")

    body = client.get(f"/api/rooms/{uuid}/anchor").json()
    # Normal for a live room — but it must be stated, not glossed over.
    assert body["unchanged_since_anchor"] is False
    assert body["anchored_digest"] != body["current_digest"]
    # The proof still verifies against the published root, using the digest as
    # it was anchored — that is the whole point of storing the leaf set.
    assert anchor.verify_proof(
        uuid, body["anchored_digest"], body["proof"], body["anchor"]["root"]
    )


def test_unpublished_anchor_is_visibly_unpublished():
    # A root we failed to publish must not read as anchored.
    with Session(engine) as s:
        anchor.create(s)
    newest = client.get("/api/anchors").json()["anchors"][0]
    assert newest["receipt"] is None
    assert newest["published"] is False


def test_anchor_survives_room_expiry():
    # An expired room is exactly the history worth being unable to edit.
    uuid = _room("expired but anchored")
    _say(uuid, "a", "final word")
    with Session(engine) as s:
        anchor.create(s, receipt="https://t.me/example/4", published_via="telegram")
        room = s.get(Room, uuid)
        room.expires_at = app.models.utcnow().replace(year=2000)
        s.add(room)
        s.commit()

    assert client.get(f"/api/rooms/{uuid}").status_code == 410
    body = client.get(f"/api/rooms/{uuid}/anchor")
    assert body.status_code == 200, "anchors must outlive the rooms they cover"
    assert body.json()["current_digest"]


def test_signed_statement_is_verifiable():
    from app import pcis
    import json

    with Session(engine) as s:
        row = anchor.create(s)
    st = anchor.signed_statement(row)
    canonical = json.dumps(
        {k: st[k] for k in
         ("service", "digest_version", "anchor_id", "root", "leaf_count", "created_at")},
        sort_keys=True, separators=(",", ":"),
    )
    assert pcis.verify_hex(
        st["arbiter_pubkey"], canonical.encode("utf-8"), st["arbiter_signature"]
    )


def test_proof_endpoint_does_not_rescan_every_room():
    """A proof must cost one room, not the whole server.

    The first cut rebuilt every leaf per request: 2.4 s over 4387 production
    rooms, on an endpoint with no auth. Storing the leaf set with the anchor
    fixed both that and a correctness bug — the proof now runs against the
    state as published, not as it is now.
    """
    import time

    for i in range(40):
        u = _room(f"bulk {i}")
        _say(u, "a", f"msg {i}")
    target = _room("the one we ask about")
    _say(target, "a", "hello")
    with Session(engine) as s:
        anchor.create(s, receipt="https://t.me/example/9", published_via="telegram")

    started = time.monotonic()
    body = client.get(f"/api/rooms/{target}/anchor").json()
    elapsed = time.monotonic() - started

    assert body["proof"] is not None
    assert anchor.verify_proof(
        target, body["anchored_digest"], body["proof"], body["anchor"]["root"]
    )
    # Generous bound: the point is that it does not grow with room count.
    assert elapsed < 1.0, f"proof endpoint took {elapsed:.2f}s"


def test_anchor_without_stored_leaves_still_reports_its_root():
    # Rows written before leaf sets were stored must degrade to "no proof",
    # not to a 500.
    uuid = _room("legacy anchor")
    _say(uuid, "a", "hi")
    with Session(engine) as s:
        row = anchor.create(s, receipt="https://t.me/example/10", published_via="telegram")
        row.leaves_gz = None
        s.add(row)
        s.commit()
    body = client.get(f"/api/rooms/{uuid}/anchor").json()
    assert body["anchor"]["receipt"] == "https://t.me/example/10"
    assert body["proof"] is None
    assert body["unchanged_since_anchor"] is False
    assert "not covered" in body["note"]
