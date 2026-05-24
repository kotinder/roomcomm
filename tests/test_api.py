from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
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


fastapi_app.dependency_overrides[get_session] = _override
client = TestClient(fastapi_app)

# Bypass rate-limiter for tests — the suite creates many rooms in one process.
import app.main as _main
_main.ROOM_CREATE_LIMIT = 10_000
_main.SKILL_UPLOAD_LIMIT = 10_000


def _new_room():
    r = client.post("/api/rooms", json={"description": "test"})
    assert r.status_code == 201
    return r.json()["uuid"]


def test_create_and_info():
    uid = _new_room()
    r = client.get(f"/api/rooms/{uid}")
    assert r.status_code == 200 and r.json()["message_count"] == 0


def test_post_and_list_messages():
    uid = _new_room()
    assert client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a1", "text": "hi"}).status_code == 201
    assert client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a2", "text": "yo"}).status_code == 201
    data = client.get(f"/api/rooms/{uid}/messages").json()
    assert len(data["messages"]) == 2 and data["has_more"] is False
    since = data["messages"][0]["id"]
    data2 = client.get(f"/api/rooms/{uid}/messages?since={since}").json()
    assert len(data2["messages"]) == 1


def test_validation_errors():
    uid = _new_room()
    assert client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a", "text": ""}).status_code == 400
    assert client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a", "text": "x" * 10001}).status_code == 400
    assert client.get("/api/rooms/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/api/rooms/not-a-uuid").status_code == 400


def test_public_listing_excludes_private():
    # default private
    priv = client.post("/api/rooms", json={"description": "secret"}).json()
    assert priv["is_public"] is False
    pub = client.post("/api/rooms", json={"description": "open", "is_public": True}).json()
    assert pub["is_public"] is True
    listing = client.get("/api/rooms").json()
    uuids = {r["uuid"] for r in listing["rooms"]}
    assert pub["uuid"] in uuids
    assert priv["uuid"] not in uuids


# ---------- Skill CDN ----------

import io
import tarfile

def _make_skill_tar(skill_md_text: bytes = b"---\nname: test-skill\n---\n# Test\n") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo("test-skill/SKILL.md")
        ti.size = len(skill_md_text)
        tf.addfile(ti, io.BytesIO(skill_md_text))
    return buf.getvalue()


def test_skill_upload_basic():
    data = _make_skill_tar()
    r = client.post(
        "/api/skills",
        data={"name": "test-skill", "version": "0.1",
              "description": "test", "agent_id": "tester"},
        files={"file": ("test-skill.tar.gz", data, "application/gzip")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["deduped"] is False
    assert body["size_bytes"] == len(data)
    assert len(body["sha256"]) == 64
    assert "/api/skills/" in body["fetch_url"]

    # manifest
    m = client.get(f"/api/skills/{body['id']}").json()
    assert m["sha256"] == body["sha256"]
    assert m["author_sig"] is None  # not requested

    # download — follows redirect to /skills-cdn/, which is served by nginx in prod.
    # in the TestClient there is no nginx, so /skills-cdn/ returns 404 from FastAPI.
    # we just verify FastAPI itself issues a 307 to the right location.
    r2 = client.get(f"/api/skills/{body['id']}/test-skill-0.1.tar.gz",
                    follow_redirects=False)
    assert r2.status_code == 307
    assert r2.headers["location"] == f"/skills-cdn/{body['sha256']}.tar.gz"


def test_skill_dedup():
    data = _make_skill_tar(b"---\nname: dedup\n---\n# Dedup\n")
    r1 = client.post(
        "/api/skills",
        data={"name": "dedup", "version": "1", "description": "", "agent_id": "tester"},
        files={"file": ("a.tar.gz", data, "application/gzip")},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/skills",
        data={"name": "dedup", "version": "1", "description": "", "agent_id": "tester"},
        files={"file": ("a.tar.gz", data, "application/gzip")},
    )
    assert r2.status_code in (200, 201)  # FastAPI emits 201 from the decorator; JSON body is what matters
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["deduped"] is True


def test_skill_size_limit():
    import os as _os
    # use random bytes — repeated bytes compress to ~nothing, defeating the test
    big = _make_skill_tar(_os.urandom(520 * 1024))
    assert len(big) > 512 * 1024
    r = client.post(
        "/api/skills",
        data={"name": "big", "version": "1", "description": "", "agent_id": "tester"},
        files={"file": ("big.tar.gz", big, "application/gzip")},
    )
    assert r.status_code == 400, r.text
    assert "too large" in r.json()["detail"].lower()


def test_skill_not_tar():
    # plain text, not a tar.gz
    r = client.post(
        "/api/skills",
        data={"name": "bad", "version": "1", "description": "", "agent_id": "tester"},
        files={"file": ("bad.tar.gz", b"not a tarball", "application/gzip")},
    )
    assert r.status_code == 400


def test_skill_missing_skill_md():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo("noskill/other.txt")
        ti.size = 5
        tf.addfile(ti, io.BytesIO(b"hello"))
    r = client.post(
        "/api/skills",
        data={"name": "noskill", "version": "1", "description": "", "agent_id": "tester"},
        files={"file": ("a.tar.gz", buf.getvalue(), "application/gzip")},
    )
    assert r.status_code == 400
    assert "SKILL.md" in r.json()["detail"]


def test_skill_signature_valid_and_invalid():
    import hashlib
    try:
        from nacl.encoding import HexEncoder
        from nacl.signing import SigningKey
    except ImportError:
        import pytest
        pytest.skip("pynacl not installed")
    data = _make_skill_tar(b"---\nname: signed\n---\n# Signed\n")
    digest = hashlib.sha256(data).hexdigest()
    sk = SigningKey.generate()
    pub = sk.verify_key.encode(encoder=HexEncoder).decode()
    sig = sk.sign(digest.encode("ascii")).signature.hex()

    # valid sig
    r = client.post(
        "/api/skills",
        data={"name": "signed", "version": "1", "description": "",
              "agent_id": "tester", "author_pubkey": pub, "author_sig": sig},
        files={"file": ("a.tar.gz", data, "application/gzip")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["author_pubkey"] == pub

    # tampered sig (flip last hex char)
    bad_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    r2 = client.post(
        "/api/skills",
        data={"name": "signed-2", "version": "1", "description": "",
              "agent_id": "tester", "author_pubkey": pub, "author_sig": bad_sig},
        files={"file": ("b.tar.gz", _make_skill_tar(b"---\nname: sx\n---\n"),
                        "application/gzip")},
    )
    assert r2.status_code == 400


# ---------- Protocol / ledger (threads + revisions) ----------


def test_room_protocol_mode_default_and_premium():
    r = client.post("/api/rooms", json={"description": "x"})
    assert r.json()["protocol_mode"] == "standard"
    r2 = client.post("/api/rooms", json={"description": "y", "protocol_mode": "premium"})
    assert r2.status_code == 201 and r2.json()["protocol_mode"] == "premium"


def test_open_thread_and_confirm_promotes_to_agreed():
    uid = _new_room()
    # alice opens a thread
    r = client.post(f"/api/rooms/{uid}/claims", json={
        "subject": "Project deadline", "value": "deliver by Friday", "opened_by": "alice",
    })
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["status"] == "proposed"
    assert r.json()["revisions_count"] == 1

    # bob confirms — still proposed (need 2 distinct confirmers)
    r2 = client.post(f"/api/rooms/{uid}/claims/{cid}/revisions", json={
        "agent_id": "bob", "value": "deliver by Friday", "kind": "confirm",
    })
    assert r2.status_code == 201
    assert r2.json()["status"] == "proposed"

    # carol confirms — now ≥ 2 distinct non-owner confirmers → agreed
    r3 = client.post(f"/api/rooms/{uid}/claims/{cid}/revisions", json={
        "agent_id": "carol", "value": "deliver by Friday", "kind": "confirm",
    })
    assert r3.json()["status"] == "agreed"
    assert r3.json()["revisions_count"] == 3


def test_owner_cannot_confirm_own_thread():
    uid = _new_room()
    r = client.post(f"/api/rooms/{uid}/claims", json={
        "subject": "Topic", "value": "value", "opened_by": "alice",
    })
    cid = r.json()["id"]
    r2 = client.post(f"/api/rooms/{uid}/claims/{cid}/revisions", json={
        "agent_id": "alice", "value": "value", "kind": "confirm",
    })
    assert r2.status_code == 400


def test_owner_update_demotes_agreed_back_to_proposed():
    uid = _new_room()
    r = client.post(f"/api/rooms/{uid}/claims", json={
        "subject": "Deadline", "value": "Friday", "opened_by": "alice",
    })
    cid = r.json()["id"]
    for ag in ["bob", "carol"]:
        client.post(f"/api/rooms/{uid}/claims/{cid}/revisions",
                    json={"agent_id": ag, "value": "Friday", "kind": "confirm"})
    assert client.get(f"/api/rooms/{uid}/claims/{cid}").json()["status"] == "agreed"

    # alice changes her mind
    r2 = client.post(f"/api/rooms/{uid}/claims/{cid}/revisions", json={
        "agent_id": "alice", "value": "Monday", "kind": "update",
    })
    body = r2.json()
    assert body["status"] == "proposed"
    assert body["current_value"] == "Monday"


def test_retract_cancels_thread():
    uid = _new_room()
    cid = client.post(f"/api/rooms/{uid}/claims", json={
        "subject": "T", "value": "v", "opened_by": "alice",
    }).json()["id"]
    r = client.post(f"/api/rooms/{uid}/claims/{cid}/revisions", json={
        "agent_id": "alice", "value": "withdrawn", "kind": "retract",
    })
    assert r.json()["status"] == "cancelled"


def test_only_owner_can_update_or_retract():
    uid = _new_room()
    cid = client.post(f"/api/rooms/{uid}/claims", json={
        "subject": "T", "value": "v", "opened_by": "alice",
    }).json()["id"]
    r = client.post(f"/api/rooms/{uid}/claims/{cid}/revisions", json={
        "agent_id": "mallory", "value": "hacked", "kind": "update",
    })
    assert r.status_code == 403


def test_context_shape_and_hash_threads():
    uid = _new_room()
    body = client.get(f"/api/rooms/{uid}/context").json()
    assert set(body) >= {"room_uuid", "threads", "discrepancies", "context_hash", "last_extracted_msg_id"}
    h1 = body["context_hash"]
    # adding a thread changes the hash
    client.post(f"/api/rooms/{uid}/claims", json={
        "subject": "X", "value": "v", "opened_by": "alice",
    })
    h2 = client.get(f"/api/rooms/{uid}/context").json()["context_hash"]
    assert h1 != h2 and len(h2) == 64


def test_handshake_requires_current_hash():
    uid = _new_room()
    ctx_hash = client.get(f"/api/rooms/{uid}/context").json()["context_hash"]
    # bogus hash → 409
    bogus = "0" * 64
    if bogus != ctx_hash:
        assert client.post(f"/api/rooms/{uid}/handshake", json={
            "agent_id": "alice", "context_hash": bogus,
        }).status_code == 409
    # correct → 201
    assert client.post(f"/api/rooms/{uid}/handshake", json={
        "agent_id": "alice", "context_hash": ctx_hash,
    }).status_code == 201


def test_refresh_disabled_without_llm_key(monkeypatch):
    monkeypatch.setattr("app.llm.NVIDIA_API_KEY", "")
    monkeypatch.setattr("app.llm.DEEPSEEK_API_KEY", "")
    uid = _new_room()
    r = client.post(f"/api/rooms/{uid}/context/refresh")
    assert r.status_code == 503


def test_refresh_creates_thread_from_first_message(monkeypatch):
    monkeypatch.setattr("app.llm.NVIDIA_API_KEY", "fake")

    async def fake_process(new_message, tail, threads):
        return {
            "new_claims": [{
                "subject": "Concrete delivery to site #2",
                "subject_key": "concrete-delivery-site-2",
                "value": "delivery on 2026-05-20",
                "kind": "propose",
                "quote": "delivery on May 20",
            }],
            "updates": [],
            "discrepancies": [],
        }, "fake:model"

    monkeypatch.setattr("app.llm.process_message", fake_process)
    uid = _new_room()
    client.post(f"/api/rooms/{uid}/messages",
                json={"agent_id": "alice", "text": "I'll deliver concrete on May 20"})
    r = client.post(f"/api/rooms/{uid}/context/refresh")
    assert r.status_code == 200, r.text
    assert r.json()["extracted"] == 1
    ctx = client.get(f"/api/rooms/{uid}/context").json()
    assert len(ctx["threads"]) == 1
    t = ctx["threads"][0]
    assert t["subject"] == "Concrete delivery to site #2"
    assert t["current_value"] == "delivery on 2026-05-20"
    assert t["status"] == "proposed"
    assert t["opened_by"] == "arbiter"
    assert t["revisions_count"] == 1


def test_refresh_routes_update_to_existing_thread(monkeypatch):
    """Two messages: first opens a thread, second updates it via the arbiter."""
    monkeypatch.setattr("app.llm.NVIDIA_API_KEY", "fake")
    uid = _new_room()
    m1 = client.post(f"/api/rooms/{uid}/messages",
                     json={"agent_id": "alice", "text": "deliver concrete May 20"}).json()["id"]
    m2 = client.post(f"/api/rooms/{uid}/messages",
                     json={"agent_id": "alice", "text": "moved to May 22"}).json()["id"]

    # The mock returns different output depending on whether existing threads exist.
    async def fake_process(new_message, tail, threads):
        if not threads:
            return {
                "new_claims": [{
                    "subject": "Concrete delivery", "subject_key": "concrete-delivery",
                    "value": "delivery on 2026-05-20", "kind": "propose",
                    "quote": "May 20",
                }],
                "updates": [], "discrepancies": [],
            }, "fake:model"
        return {
            "new_claims": [],
            "updates": [{
                "thread_id": threads[0]["id"],
                "value": "delivery on 2026-05-22",
                "kind": "update",
                "quote": "moved to May 22",
            }],
            "discrepancies": [],
        }, "fake:model"

    monkeypatch.setattr("app.llm.process_message", fake_process)
    r = client.post(f"/api/rooms/{uid}/context/refresh")
    assert r.status_code == 200, r.text
    ctx = client.get(f"/api/rooms/{uid}/context").json()
    assert len(ctx["threads"]) == 1
    t = ctx["threads"][0]
    assert t["current_value"] == "delivery on 2026-05-22"
    assert t["revisions_count"] == 2
    # verify ledger
    revs = client.get(f"/api/rooms/{uid}/claims/{t['id']}/revisions").json()
    assert revs[0]["kind"] == "propose" and revs[0]["source_msg_id"] == m1
    assert revs[1]["kind"] == "update" and revs[1]["source_msg_id"] == m2
    assert revs[1]["author_agent_id"] == "alice"


def test_refresh_is_incremental(monkeypatch):
    """Second refresh with no new messages should not re-process old ones."""
    monkeypatch.setattr("app.llm.NVIDIA_API_KEY", "fake")
    calls = []

    async def fake_process(new_message, tail, threads):
        calls.append(new_message["id"])
        return {"new_claims": [], "updates": [], "discrepancies": []}, "fake:model"

    monkeypatch.setattr("app.llm.process_message", fake_process)
    uid = _new_room()
    client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a", "text": "one"})
    client.post(f"/api/rooms/{uid}/context/refresh")
    assert len(calls) == 1
    # Second refresh with no new messages → no additional LLM calls.
    client.post(f"/api/rooms/{uid}/context/refresh")
    assert len(calls) == 1
    # Add a new message → exactly one new call.
    client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a", "text": "two"})
    client.post(f"/api/rooms/{uid}/context/refresh")
    assert len(calls) == 2


def test_refresh_full_reprocesses_all(monkeypatch):
    monkeypatch.setattr("app.llm.NVIDIA_API_KEY", "fake")
    calls = []

    async def fake_process(new_message, tail, threads):
        calls.append(new_message["id"])
        return {"new_claims": [], "updates": [], "discrepancies": []}, "fake:model"

    monkeypatch.setattr("app.llm.process_message", fake_process)
    uid = _new_room()
    client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a", "text": "one"})
    client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a", "text": "two"})
    client.post(f"/api/rooms/{uid}/context/refresh")
    assert len(calls) == 2
    # ?full=true should reset watermark and reprocess everything
    client.post(f"/api/rooms/{uid}/context/refresh?full=true")
    assert len(calls) == 4


# ---------- PCIS: per-message signatures + arbiter-signed chain + /verify ----------

def _ed_keys():
    try:
        from nacl.encoding import HexEncoder
        from nacl.signing import SigningKey
    except ImportError:
        import pytest
        pytest.skip("pynacl not installed")
    sk = SigningKey.generate()
    return sk, sk.verify_key.encode(encoder=HexEncoder).decode()


def test_arbiter_pubkey_endpoint():
    r = client.get("/api/arbiter/pubkey")
    assert r.status_code == 200
    body = r.json()
    assert body["alg"] == "ed25519"
    assert len(body["pubkey_hex"]) == 64
    int(body["pubkey_hex"], 16)  # is valid hex


def test_message_signed_intake_accepted_and_verified():
    from app import pcis as app_pcis
    from nacl.encoding import HexEncoder
    sk, pub = _ed_keys()
    uid = _new_room()
    from datetime import datetime, timezone
    ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    text = "I commit to deliver by Friday."
    surface = app_pcis.message_surface(text, ts_iso, uid, None)
    sig_hex = sk.sign(surface).signature.hex()
    r = client.post(f"/api/rooms/{uid}/messages", json={
        "agent_id": "alice", "text": text, "ts_iso": ts_iso,
        "pubkey_hex": pub, "signature_hex": sig_hex,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pubkey_hex"] == pub
    assert body["signature_hex"] == sig_hex
    # /verify should return CLEAN
    v = client.post(f"/api/rooms/{uid}/verify").json()
    assert v["verdict"] == "CLEAN", v


def test_message_tampered_signature_rejected():
    from app import pcis as app_pcis
    sk, pub = _ed_keys()
    uid = _new_room()
    from datetime import datetime, timezone
    ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    text = "I commit to deliver by Friday."
    surface = app_pcis.message_surface(text, ts_iso, uid, None)
    sig_hex = sk.sign(surface).signature.hex()
    # Flip last hex char.
    bad_sig = sig_hex[:-1] + ("0" if sig_hex[-1] != "0" else "1")
    r = client.post(f"/api/rooms/{uid}/messages", json={
        "agent_id": "alice", "text": text, "ts_iso": ts_iso,
        "pubkey_hex": pub, "signature_hex": bad_sig,
    })
    assert r.status_code == 400
    # And no row was created.
    msgs = client.get(f"/api/rooms/{uid}/messages").json()["messages"]
    assert len(msgs) == 0


def test_ts_too_far_rejected():
    from app import pcis as app_pcis
    sk, pub = _ed_keys()
    uid = _new_room()
    ts_iso = "2020-01-01T00:00:00.000000Z"  # ancient
    text = "old"
    surface = app_pcis.message_surface(text, ts_iso, uid, None)
    sig_hex = sk.sign(surface).signature.hex()
    r = client.post(f"/api/rooms/{uid}/messages", json={
        "agent_id": "alice", "text": text, "ts_iso": ts_iso,
        "pubkey_hex": pub, "signature_hex": sig_hex,
    })
    assert r.status_code == 400
    assert "5 minutes" in r.json()["detail"]


def test_manual_thread_has_arbiter_chained_revision():
    """Opening a thread manually should produce a propose-revision with a
    valid row_hash and arbiter signature."""
    uid = _new_room()
    r = client.post(f"/api/rooms/{uid}/claims", json={
        "subject": "Deadline", "value": "Friday", "opened_by": "alice",
    })
    cid = r.json()["id"]
    # Fetch the thread with revisions.
    full = client.get(f"/api/rooms/{uid}/claims/{cid}").json()
    assert full["revisions_count"] == 1
    # /verify should be CLEAN.
    v = client.post(f"/api/rooms/{uid}/verify").json()
    assert v["verdict"] == "CLEAN", v


def test_verify_detects_tampered_revision_value():
    """Directly mutate a revision in the DB to simulate operator tampering;
    /verify must REFUTE."""
    uid = _new_room()
    cid = client.post(f"/api/rooms/{uid}/claims", json={
        "subject": "Topic", "value": "original", "opened_by": "alice",
    }).json()["id"]
    # Mutate via direct DB access — simulate the operator editing SQLite.
    from app.models import Claim as ClaimM, ClaimRevision as RevM
    from sqlmodel import select as _select
    with Session(engine) as s:
        rev = s.exec(_select(RevM).where(RevM.claim_id == cid)).first()
        rev.value = "tampered"
        s.add(rev)
        s.commit()
    v = client.post(f"/api/rooms/{uid}/verify").json()
    assert v["verdict"] == "REFUTED"
    assert "tampered_revision_payload" in v["details"].get("type", "") or \
           "broken_chain" in v["details"].get("type", "") or \
           "invalid_arbiter_signature" in v["details"].get("type", "")


def test_verify_detects_broken_chain():
    """Insert a revision out of order (mutate prev_hash) → broken_chain."""
    uid = _new_room()
    cid = client.post(f"/api/rooms/{uid}/claims", json={
        "subject": "A", "value": "v1", "opened_by": "alice",
    }).json()["id"]
    # Add a confirm revision normally so we have ≥ 2 revisions to chain.
    client.post(f"/api/rooms/{uid}/claims/{cid}/revisions", json={
        "agent_id": "bob", "value": "v1", "kind": "confirm",
    })
    # Now corrupt the second revision's prev_hash.
    from app.models import ClaimRevision as RevM
    from sqlmodel import select as _select
    with Session(engine) as s:
        revs = s.exec(_select(RevM).where(RevM.claim_id == cid).order_by(RevM.id)).all()
        assert len(revs) == 2
        revs[1].prev_hash = "0" * 64
        s.add(revs[1])
        s.commit()
    v = client.post(f"/api/rooms/{uid}/verify").json()
    assert v["verdict"] == "REFUTED"


def test_verify_empty_room_clean():
    uid = _new_room()
    v = client.post(f"/api/rooms/{uid}/verify").json()
    assert v["verdict"] == "CLEAN"
