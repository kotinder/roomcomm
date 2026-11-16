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


@pytest.mark.parametrize("lang,heading", [
    ("ru", "Что агенты могут делать вместе"),
    ("en", "What agents can do together"),
    ("zh", "智能体可以一起做什么"),
])
def test_examples_page_is_localized_and_prompts_are_escaped(lang, heading):
    r = client.get(f"/examples?lang={lang}")
    assert r.status_code == 200
    assert heading in r.text
    assert r.text.count("<article ") == 7
    assert r.text.count('class="prompt-text"') == 7
    assert "built-in method" not in r.text
    # Placeholder prompts remain visible text, not HTML elements.
    assert "&lt;" in r.text
    assert "http://testserver/agents.md" in r.text
    assert "<URL" not in r.text
    for anchor in ("second-opinion", "research", "handoff", "negotiation",
                   "knock", "simulation", "coordination", "start"):
        assert f'href="#{anchor}"' in r.text
        assert f'id="{anchor}"' in r.text
    assert f'href="/examples?lang={lang}"' in client.get(f"/?lang={lang}").text


def test_examples_page_uses_domain_language_and_serves_styles():
    # HTML needs no lifespan; the shared MCP manager can start only once.
    fresh = TestClient(fastapi_app)
    response = fresh.get("/examples", headers={"host": "roomcomm.ru"})
    assert response.status_code == 200
    assert '<html lang="ru">' in response.text
    assert fresh.get("/static/examples.css").status_code == 200


# Scanner paths must not cost us a 13 KB template render each; a truncated room
# link still has to reach the page that explains itself.
@pytest.mark.parametrize("path", [
    "/index.php", "/.env", "/wp-login.php", "/xmlrpc.php", "/config",
    "/.well-known", "/backup.zip", "/feed",
    "/--------",                                 # dashes alone are no UUID
    "/0e8f1c2",                                  # seven hex digits: too short to tell
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
    "/0E8F1C2A-4B6D",                           # upper case
    "/0e8f1c2a-4b6d-47f9-8e1a-2b3c4d5e6f70.",   # sentence punctuation stuck on
    "/0e8f1c2a-4b6d-47f9-8e1a-2b3c4d5e6f70)",   # closing bracket from the chat
    "/%200e8f1c2a-4b6d-47f9-8e1a-2b3c4d5e6f70", # leading space
])
def test_truncated_room_link_still_gets_the_page(path):
    r = client.get(path)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")


def test_scanner_path_asking_for_markdown_stays_markdown():
    r = client.get("/index.php", headers={"Accept": "text/markdown"})
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/markdown")
