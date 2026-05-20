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
