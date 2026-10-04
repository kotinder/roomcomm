"""Room-file MD exchange: verified-only in both directions, dedup, delete.

Uses its own in-memory DB (same pattern as test_auth.py). Blobs are written
to a per-session tmp dir — files.FILES_DIR is patched so tests never touch
the real data/files store.
"""
import hashlib
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, delete as sql_delete, select
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.files as files_mod
import app.main as _main
import app.quota as quota
from app.main import app as fastapi_app
from app.database import get_session
from app.models import AgentKey, RoomFile

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

_main.ROOM_CREATE_LIMIT = 10_000
_main.KEY_CREATE_LIMIT = 10_000
_main.FILE_UPLOAD_LIMIT = 10_000

ADMIN = "test-admin-token"
_main.ADMIN_TOKEN = ADMIN


@pytest.fixture(autouse=True)
def _files_test_env(tmp_path):
    prev = fastapi_app.dependency_overrides.get(get_session)
    fastapi_app.dependency_overrides[get_session] = _override
    prev_dir = files_mod.FILES_DIR
    files_mod.FILES_DIR = tmp_path
    yield
    files_mod.FILES_DIR = prev_dir
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev
    with Session(engine) as s:
        s.exec(sql_delete(RoomFile))
        s.exec(sql_delete(app.models.UsageCounter))
        s.commit()


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def _new_key(agent_id="tester", tier=None):
    r = client.post("/api/keys", json={"agent_id": agent_id})
    assert r.status_code == 201
    key = r.json()["key"]
    if tier:
        with Session(engine) as s:
            kid = s.exec(select(AgentKey)).all()[-1].id
        assert client.post(f"/admin/keys/{kid}/tier", data={"tier": tier},
                           headers={"Authorization": f"Bearer {ADMIN}"},
                           follow_redirects=False).status_code == 303
    return key


def _new_room(**kwargs):
    r = client.post("/api/rooms", json={"description": "t", **kwargs})
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def _upload(uid, headers, content=b"# hello\n", name="doc.md", **form):
    return client.post(f"/api/rooms/{uid}/files", headers=headers,
                       files={"file": (name, content, "text/markdown")}, data=form)


# ---------- the verified gate, both directions ----------

def test_exchange_is_verified_only():
    uid = _new_room()
    free = _bearer(_new_key("free-uploader"))

    # anonymous and free-key uploads bounce with the funnel hint
    r = _upload(uid, {})
    assert r.status_code == 403 and "Telegram-verified" in r.json()["detail"]
    r = _upload(uid, free)
    assert r.status_code == 403 and "Telegram-verified" in r.json()["detail"]

    # verified uploads fine
    ver = _bearer(_new_key("ver-uploader", tier="verified"))
    r = _upload(uid, ver)
    assert r.status_code == 201, r.text
    fid = r.json()["id"]

    # download side is gated exactly the same
    assert client.get(f"/api/rooms/{uid}/files").status_code == 403
    assert client.get(f"/api/rooms/{uid}/files", headers=free).status_code == 403
    assert client.get(f"/api/rooms/{uid}/files/{fid}", headers=free).status_code == 403

    # a second verified key (the other side of the exchange) reads it all
    other = _bearer(_new_key("ver-reader", tier="verified"))
    listing = client.get(f"/api/rooms/{uid}/files", headers=other)
    assert listing.status_code == 200 and listing.json()["total"] == 1
    dl = client.get(f"/api/rooms/{uid}/files/{fid}", headers=other)
    assert dl.status_code == 200
    assert dl.text == "# hello\n"
    assert dl.headers["content-type"].startswith("text/markdown")


def test_trusted_tier_also_passes():
    uid = _new_room()
    h = _bearer(_new_key("trusty", tier="trusted"))
    assert _upload(uid, h).status_code == 201


# ---------- upload semantics ----------

def test_upload_roundtrip_and_metadata():
    uid = _new_room()
    h = _bearer(_new_key("meta", tier="verified"))
    body = "# Brief\n\nПлан по файлообмену.\n".encode("utf-8")
    r = _upload(uid, h, content=body, name="brief.md",
                description="the plan", agent_id="meta-agent")
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "brief.md"
    assert data["description"] == "the plan"
    assert data["agent_id"] == "meta-agent"
    assert data["size_bytes"] == len(body)
    assert data["sha256"] == hashlib.sha256(body).hexdigest()
    assert data["deduped"] is False
    assert f"/api/rooms/{uid}/files/{data['id']}" in data["fetch_url"]

    dl = client.get(f"/api/rooms/{uid}/files/{data['id']}", headers=h)
    assert dl.status_code == 200 and dl.content == body


def test_download_of_cyrillic_name_serves_rfc6266_header():
    # A non-Latin-1 name in Content-Disposition used to 500 the download.
    uid = _new_room()
    h = _bearer(_new_key("cyr", tier="verified"))
    body = "# Замечания\n".encode("utf-8")
    fid = _upload(uid, h, content=body, name="12_Замечания_встречи.md").json()["id"]

    dl = client.get(f"/api/rooms/{uid}/files/{fid}", headers=h)
    assert dl.status_code == 200 and dl.content == body
    cd = dl.headers["content-disposition"]
    cd.encode("latin-1")  # the header itself must survive the wire
    assert "filename*=UTF-8''" in cd and quote("12_Замечания_встречи.md") in cd


def test_name_is_sanitized_and_md_enforced():
    uid = _new_room()
    h = _bearer(_new_key("names", tier="verified"))
    r = _upload(uid, h, name='..\\evil"quote.txt')
    assert r.status_code == 201
    name = r.json()["name"]
    assert name.endswith(".md") and '"' not in name and "\\" not in name


def test_dedup_per_room():
    uid = _new_room()
    other_room = _new_room()
    h = _bearer(_new_key("dedup", tier="verified"))
    first = _upload(uid, h, content=b"same bytes")
    again = _upload(uid, h, content=b"same bytes")
    assert first.status_code == 201 and again.status_code == 201
    assert again.json()["deduped"] is True
    assert again.json()["id"] == first.json()["id"]
    # same bytes in another room = a separate reference
    elsewhere = _upload(other_room, h, content=b"same bytes")
    assert elsewhere.json()["deduped"] is False
    assert elsewhere.json()["id"] != first.json()["id"]


def test_rejects_binary_empty_and_oversize():
    uid = _new_room()
    h = _bearer(_new_key("garbage", tier="verified"))
    assert _upload(uid, h, content=b"\xff\xfe binary").status_code == 400
    assert _upload(uid, h, content=b"").status_code == 400
    big = b"a" * (files_mod.MAX_BYTES + 1)
    r = _upload(uid, h, content=big)
    assert r.status_code == 400 and "too large" in r.json()["detail"]


def test_per_room_file_cap():
    uid = _new_room()
    h = _bearer(_new_key("capper", tier="verified"))
    old = files_mod.MAX_FILES_PER_ROOM
    files_mod.MAX_FILES_PER_ROOM = 2
    try:
        assert _upload(uid, h, content=b"one").status_code == 201
        assert _upload(uid, h, content=b"two").status_code == 201
        r = _upload(uid, h, content=b"three")
        assert r.status_code == 429 and "room_files_full" in r.json()["detail"]
    finally:
        files_mod.MAX_FILES_PER_ROOM = old


def test_write_protected_room_needs_room_key():
    r = client.post("/api/rooms", json={"description": "t", "write_policy": "key"})
    assert r.status_code == 201
    uid, wk = r.json()["uuid"], r.json()["write_key"]
    h = _bearer(_new_key("stranger", tier="verified"))
    assert _upload(uid, h).status_code == 403
    ok = client.post(f"/api/rooms/{uid}/files", headers={**h, "X-Room-Key": wk},
                     files={"file": ("d.md", b"# x", "text/markdown")})
    assert ok.status_code == 201


# ---------- delete ----------

def test_delete_only_by_uploader():
    uid = _new_room()
    owner = _bearer(_new_key("file-owner", tier="verified"))
    fid = _upload(uid, owner).json()["id"]

    other = _bearer(_new_key("not-owner", tier="verified"))
    assert client.delete(f"/api/rooms/{uid}/files/{fid}", headers=other).status_code == 403

    assert client.delete(f"/api/rooms/{uid}/files/{fid}", headers=owner).status_code == 204
    assert client.get(f"/api/rooms/{uid}/files/{fid}", headers=owner).status_code == 404
    # blob is gone from storage too (no other room references it)
    assert not any(files_mod.FILES_DIR.iterdir())


def test_delete_keeps_blob_referenced_by_another_room():
    room_a, room_b = _new_room(), _new_room()
    h = _bearer(_new_key("sharer", tier="verified"))
    fa = _upload(room_a, h, content=b"shared bytes").json()["id"]
    fb = _upload(room_b, h, content=b"shared bytes").json()["id"]
    assert client.delete(f"/api/rooms/{room_a}/files/{fa}", headers=h).status_code == 204
    # room B's copy still downloads
    dl = client.get(f"/api/rooms/{room_b}/files/{fb}", headers=h)
    assert dl.status_code == 200 and dl.content == b"shared bytes"


# ---------- kill switch ----------

def test_env_kill_switch(monkeypatch):
    monkeypatch.setenv("ROOMCOMM_FILE_EXCHANGE", "0")
    uid = _new_room()
    h = _bearer(_new_key("switched-off", tier="verified"))
    assert _upload(uid, h).status_code == 404
    assert client.get(f"/api/rooms/{uid}/files", headers=h).status_code == 404


def test_env_kill_switch_covers_mcp(monkeypatch):
    # MCP tools write through their own sessions, not the REST dependency —
    # the switch must be checked there too, or /mcp is a side door.
    from types import SimpleNamespace
    import app.mcp_server as _mcp
    monkeypatch.setattr(_mcp, "engine", engine)
    uid = _new_room()
    key = _new_key("mcp-switch", tier="verified")
    request = SimpleNamespace(headers={"authorization": f"Bearer {key}"},
                              client=SimpleNamespace(host="127.0.0.1"))
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=request))

    # switch on: the stub ctx really reaches the tools (positive control)
    shared = _mcp.share_file(uid, "a", "doc.md", "# hi\n", ctx)
    assert _mcp.list_files(uid, ctx)["total"] == 1

    monkeypatch.setenv("ROOMCOMM_FILE_EXCHANGE", "0")
    calls = [
        lambda: _mcp.share_file(uid, "a", "other.md", "# x\n", ctx),
        lambda: _mcp.list_files(uid, ctx),
        lambda: _mcp.fetch_file(uid, shared["id"], ctx),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="file exchange is disabled"):
            call()
