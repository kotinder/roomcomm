"""Smoke: landing renders in both locales with the public-needs-TG strings."""
from fastapi.testclient import TestClient
from app.main import app as fastapi_app

client = TestClient(fastapi_app)


def test_landing_renders_both_locales_with_tg_hint():
    r = client.get("/")
    assert r.status_code == 200
    assert "Telegram-verified key" in r.text
    r2 = client.get("/", headers={"host": "roomcomm.ru"})
    assert r2.status_code == 200
    assert "Telegram" in r2.text
