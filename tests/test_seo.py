"""Check the HTML and sitemap a crawler receives, including mirror URLs."""
import json
from html.parser import HTMLParser
from urllib.parse import urlsplit
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from app.main import app


class Head(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.meta = {}
        self.json_text = ""
        self.in_json = False
        self.feed(html.split("</head>")[0])

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link":
            self.links.append(attrs)
        if tag == "meta":
            self.meta[attrs.get("name") or attrs.get("property")] = attrs.get("content")
        if tag == "script":
            self.in_json = attrs.get("type") == "application/ld+json"

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_json = False

    def handle_data(self, data):
        if self.in_json:
            self.json_text += data

    @property
    def canonical(self):
        values = [link["href"] for link in self.links if link.get("rel") == "canonical"]
        assert len(values) == 1
        return values[0]


@pytest.mark.parametrize("path", ["/", "/examples"])
@pytest.mark.parametrize("host", ["roomcomm.xyz", "roomcomm.ru"])
@pytest.mark.parametrize("lang,target", [("ru", "roomcomm.ru"), ("en", "roomcomm.xyz"), ("zh", "roomcomm.xyz")])
def test_localized_marketing_metadata(path, host, lang, target):
    client = TestClient(app, base_url=f"https://{host}")
    # Explicit language must win over both the cookie and the domain default.
    client.cookies.set("lang", "en" if lang != "en" else "ru")
    response = client.get(f"{path}?lang={lang}&utm_source=test")
    assert response.status_code == 200
    head = Head(response.text)
    assert head.canonical == f"https://{target}{path}?lang={lang}"
    assert head.meta["og:url"] == head.canonical
    assert head.meta["description"] == head.meta["og:description"]
    assert len(head.meta["description"]) > 40
    assert head.meta["twitter:card"] == "summary_large_image"
    assert head.meta["og:image"] == f"https://{target}/static/og-image.png"
    alternates = {link["hreflang"]: link["href"] for link in head.links if "hreflang" in link}
    assert set(alternates) == {"ru", "en", "zh-Hans", "x-default"}
    assert alternates["ru"] == f"https://roomcomm.ru{path}?lang=ru"
    assert alternates["x-default"] == alternates["en"]
    structured = json.loads(head.json_text)
    assert structured["url"] == head.canonical
    assert structured["inLanguage"] == ("zh-Hans" if lang == "zh" else lang)
    if path == "/examples":
        items = structured["mainEntity"]["itemListElement"]
        assert len(items) == 7
        for item in items:
            anchor = urlsplit(item["url"]).fragment
            assert f'id="{anchor}"' in response.text
            assert item["name"] in response.text
        assert structured["breadcrumb"]["itemListElement"][0]["item"] == f"https://{target}/?lang={lang}"
    else:
        expected = {"ru": "Объедините AI-агентов", "en": "Connect AI agents", "zh": "让 AI 智能体"}
        assert expected[lang] in head.meta["description"]


@pytest.mark.parametrize("host,count", [("roomcomm.ru", 2), ("roomcomm.xyz", 4)])
def test_sitemap_contains_only_own_canonical_pages_with_reciprocal_languages(host, count):
    client = TestClient(app, base_url=f"https://{host}")
    response = client.get("/sitemap.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    entries = ElementTree.fromstring(response.content).findall("s:url", ns)
    assert len(entries) == count
    for entry in entries:
        url = entry.findtext("s:loc", namespaces=ns)
        assert urlsplit(url).netloc == host
        assert urlsplit(url).path in ("/", "/examples")
        head = Head(client.get(url).text)
        assert head.canonical == url
        alternates = {node.attrib["hreflang"]: node.attrib["href"] for node in entry.findall("x:link", ns)}
        assert alternates == {link["hreflang"]: link["href"] for link in head.links if "hreflang" in link}
        # Every alternate points back to the same complete language set.
        for alternate in alternates.values():
            target_head = Head(client.get(alternate).text)
            assert {link["hreflang"]: link["href"] for link in target_head.links if "hreflang" in link} == alternates
    robots = client.get("/robots.txt").text
    assert f"Sitemap: https://{host}/sitemap.xml" in robots
    assert "Disallow: /api/" in robots and "Disallow: /admin" in robots


def test_untrusted_host_cannot_poison_canonical_or_sitemap():
    client = TestClient(app, base_url="https://untrusted.example")
    assert "untrusted.example" not in Head(client.get("/examples?lang=ru").text).canonical
    for path in ("/robots.txt", "/sitemap.xml"):
        assert "untrusted.example" not in client.get(path).text
