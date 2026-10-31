"""Smoke for the server-rendered pages: the landing, and what a one-segment
path that is not a room gets back."""
import pytest
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


# Scanner paths must not cost us a 13 KB template render each; a truncated room
# link still has to reach the page that explains itself.
@pytest.mark.parametrize("path", [
    "/index.php", "/.env", "/wp-login.php", "/xmlrpc.php", "/config",
    "/.well-known", "/backup.zip", "/feed",
])
def test_scanner_paths_get_a_bare_404(path):
    r = client.get(path)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert len(r.content) < 100


@pytest.mark.parametrize("path", [
    "/0e8f1c2a-4b6d",                           # link cut off mid-UUID
    "/0e8f1c2a4b6d47f98e1a2b3c4d5e6f70",        # dashes stripped by a chat client
    "/0e8f1c2a-4b6d-47f9-8e1a-2b3c4d5e6f7",     # last character lost
])
def test_truncated_room_link_still_gets_the_page(path):
    r = client.get(path)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")


def test_scanner_path_asking_for_markdown_stays_markdown():
    r = client.get("/index.php", headers={"Accept": "text/markdown"})
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/markdown")
