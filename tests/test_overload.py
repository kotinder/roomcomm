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
