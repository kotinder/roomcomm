"""RFC 3161 timestamping.

The anchor's root is only evidence if someone who is not us fixed it in time.
These tests cover the request we build and how a token is stored and served;
they never touch the network — the live check against real authorities is a
deploy-time step, not a unit test.
"""
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app import anchor, quota, tsa
from app.database import get_session
from app.main import app as fastapi_app
from app.models import Anchor

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)


def _override():
    with Session(engine) as s:
        yield s


client = TestClient(fastapi_app)

import app.main as _main  # noqa: E402

_main.ROOM_CREATE_LIMIT = 10_000


@pytest.fixture(autouse=True)
def _env():
    prev = fastapi_app.dependency_overrides.get(get_session)
    prev_mode = quota.QUOTA_MODE
    fastapi_app.dependency_overrides[get_session] = _override
    quota.QUOTA_MODE = "soft"
    yield
    quota.QUOTA_MODE = prev_mode
    if prev is not None:
        fastapi_app.dependency_overrides[get_session] = prev


# --- request encoding -------------------------------------------------------

def test_request_is_well_formed_der():
    digest = hashlib.sha256(b"root").digest()
    req = tsa.build_request(digest, nonce=b"\x01\x02\x03\x04\x05\x06\x07\x08")
    assert req[0] == 0x30, "a TimeStampReq is a DER SEQUENCE"
    assert digest in req, "the digest must actually be in the request"
    # version 1, and certReq TRUE so the token carries the TSA certificate.
    assert bytes.fromhex("020101") in req
    assert bytes.fromhex("0101ff") in req


def test_request_pads_a_high_bit_nonce():
    # DER integers are signed; a leading high bit without a pad byte reads as
    # negative and real authorities reject it.
    digest = hashlib.sha256(b"root").digest()
    req = tsa.build_request(digest, nonce=b"\xff\x00\x00\x00\x00\x00\x00\x01")
    assert bytes.fromhex("0209 00ff".replace(" ", "")) in req


def test_request_rejects_a_wrong_sized_digest():
    with pytest.raises(ValueError):
        tsa.build_request(b"too short")


def test_obviously_broken_replies_are_refused():
    assert not tsa.granted(b"")
    assert not tsa.granted(b"<html>error</html>")
    assert not tsa.granted(b"\x30" + b"\x00" * 10)  # right tag, far too small


def test_all_authorities_failing_returns_none(monkeypatch):
    # A missed timestamp must leave the anchor stored and honestly unstamped,
    # never abort the run.
    monkeypatch.setattr(tsa, "DEFAULT_AUTHORITIES", ("http://127.0.0.1:1",))
    assert tsa.timestamp("some-root") is None


# --- storage and serving ----------------------------------------------------

def _anchor_with_token(token: bytes) -> int:
    client.post("/api/rooms", json={"description": "tsa test"})
    with Session(engine) as s:
        row = anchor.create(s)
        row.tsa_token = token
        row.tsa_url = "http://timestamp.example"
        row.tsa_time = "2026-09-16T13:45:34Z"
        s.add(row)
        s.commit()
        return row.id


def test_token_is_served_verbatim_for_offline_checking():
    token = b"\x30\x82" + b"A" * 600
    aid = _anchor_with_token(token)
    r = client.get(f"/api/anchors/{aid}/tsa")
    assert r.status_code == 200
    assert r.content == token, "the token must come back byte-for-byte"
    assert r.headers["content-type"].startswith("application/timestamp-reply")


def test_listing_reports_the_timestamp():
    aid = _anchor_with_token(b"\x30\x82" + b"B" * 600)
    row = next(a for a in client.get("/api/anchors").json()["anchors"] if a["id"] == aid)
    assert row["timestamped"] is True
    assert row["tsa_time"] == "2026-09-16T13:45:34Z"
    assert row["token_url"] == f"/api/anchors/{aid}/tsa"
    # A timestamped root counts as anchored even with nothing posted anywhere.
    assert row["published"] is True


def test_unstamped_anchor_says_so_rather_than_pretending():
    client.post("/api/rooms", json={"description": "no token"})
    with Session(engine) as s:
        row = anchor.create(s)
        aid = row.id
    listing = client.get("/api/anchors").json()["anchors"]
    row = next(a for a in listing if a["id"] == aid)
    assert row["timestamped"] is False
    assert row["token_url"] is None
    assert row["published"] is False
    assert client.get(f"/api/anchors/{aid}/tsa").status_code == 404


def test_missing_anchor_is_404():
    assert client.get("/api/anchors/999999/tsa").status_code == 404


def test_token_time_is_parsed_from_a_real_shaped_token():
    # GeneralizedTime: tag 0x18, length 0x0f, then YYYYMMDDHHMMSSZ.
    blob = b"\x30\x82" + b"x" * 40 + b"\x18\x0f" + b"20260916134534Z" + b"y" * 40
    assert anchor._token_time(blob) == "2026-09-16T13:45:34Z"
    assert anchor._token_time(b"no time in here") is None
