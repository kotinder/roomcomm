"""Public landing-page metadata. Never include room URLs in the sitemap."""
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

ORIGINS = {"ru": "https://roomcomm.ru", "en": "https://roomcomm.xyz", "zh": "https://roomcomm.xyz"}
LOCALES = {"ru": "ru_RU", "en": "en_US", "zh": "zh_CN"}
LANG_TAGS = {"ru": "ru", "en": "en", "zh": "zh-Hans"}
PUBLIC_PATHS = ("/", "/examples")


def canonical(path: str, lang: str) -> str:
    return f"{ORIGINS[lang]}{path}?lang={lang}"


def origin_for_host(host: str | None) -> str:
    # Do not reflect arbitrary Host headers into crawl directives.
    hostname = (host or "").lower().split(":", 1)[0]
    return "https://roomcomm.ru" if hostname in ("roomcomm.ru", "www.roomcomm.ru") else "https://roomcomm.xyz"


def metadata(path: str, lang: str, title: str, description: str,
             home_label: str, examples: list[dict] | None = None) -> dict[str, Any]:
    url = canonical(path, lang)
    alternates = [{"lang": LANG_TAGS[code], "url": canonical(path, code)} for code in ORIGINS]
    alternates.append({"lang": "x-default", "url": canonical(path, "en")})
    structured: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "CollectionPage" if examples is not None else "WebSite",
        "name": title if examples is not None else "Roomcomm",
        "url": url,
        "description": description,
        "inLanguage": LANG_TAGS[lang],
    }
    if examples is not None:
        structured["mainEntity"] = {
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "ListItem", "position": i, "name": item["title"],
                 "url": f'{url}#{item["id"]}'}
                for i, item in enumerate(examples, 1)
            ],
        }
        structured["breadcrumb"] = {
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": home_label, "item": canonical("/", lang)},
                {"@type": "ListItem", "position": 2, "name": title, "item": url},
            ],
        }
    return {"title": title, "description": description, "canonical": url,
            "alternates": alternates, "locale": LOCALES[lang], "structured": structured,
            "image": ORIGINS[lang] + "/static/og-image.png"}


def sitemap(host: str | None) -> bytes:
    origin = origin_for_host(host)
    root = Element("urlset", {"xmlns": "http://www.sitemaps.org/schemas/sitemap/0.9",
                              "xmlns:xhtml": "http://www.w3.org/1999/xhtml"})
    for path in PUBLIC_PATHS:
        for lang, canonical_origin in ORIGINS.items():
            if canonical_origin != origin:
                continue
            item = SubElement(root, "url")
            SubElement(item, "loc").text = canonical(path, lang)
            for code, tag in LANG_TAGS.items():
                SubElement(item, "xhtml:link", {"rel": "alternate", "hreflang": tag,
                                                "href": canonical(path, code)})
            SubElement(item, "xhtml:link", {"rel": "alternate", "hreflang": "x-default",
                                            "href": canonical(path, "en")})
    # No fabricated lastmod: these pages change only when their content changes.
    return tostring(root, encoding="utf-8", xml_declaration=True)
