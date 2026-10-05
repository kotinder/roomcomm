"""Overload shedding (_ShedMiddleware) and /healthz."""
from fastapi.testclient import TestClient

from app import main
from app.main import app

client = TestClient(app)


def test_healthz_ok():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_sheds_with_503_when_full(monkeypatch):
    monkeypatch.setattr(main, "_inflight", main.MAX_INFLIGHT)
    r = client.get("/api/rooms")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"
    assert r.json()["detail"].startswith("overloaded")


def test_exempt_paths_pass_when_full(monkeypatch):
    monkeypatch.setattr(main, "_inflight", main.MAX_INFLIGHT)
    assert client.get("/healthz").status_code == 200


def test_counter_returns_to_zero():
    before = main._inflight
    client.get("/api/rooms")
    client.get("/no-such-page-404")
    assert main._inflight == before


def test_hits_are_buffered_then_flushed_in_one_batch():
    from sqlmodel import Session, select
    from app.database import engine
    from app.models import Hit
    main._flush_hits()  # start from an empty buffer
    with Session(engine) as s:
        before = len(s.exec(select(Hit).where(Hit.event == "landing")).all())
    for _ in range(3):
        main._record_hit("landing", "/", {"x-real-ip": "198.51.100.7"}, "127.0.0.1")
    with Session(engine) as s:  # not written yet: buffered
        assert len(s.exec(select(Hit).where(Hit.event == "landing")).all()) == before
    main._flush_hits()
    with Session(engine) as s:
        assert len(s.exec(select(Hit).where(Hit.event == "landing")).all()) == before + 3
