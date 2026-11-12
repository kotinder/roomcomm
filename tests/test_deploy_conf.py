"""The nginx site config, checked for the two properties agents depend on.

Not a syntax check — nginx isn't installed in CI, so `nginx -t` on the server
stays the last step before a reload. What this pins down is the shape of the
rate limiting, because both halves of the 02.11.2026 report were config, not
code: five agents on one address sharing one budget, and nginx answering the
limit with its built-in HTML page that no MCP client can parse.

deploy/nginx-commroom.live.conf is the gitignored full copy of what the server
runs; when it is there it has to agree with the published one.
"""
import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
PUBLISHED = DEPLOY / "nginx-commroom.conf"
LIVE = DEPLOY / "nginx-commroom.live.conf"

# The per-path zones: a key of its own for whoever presents one. The write
# zones count POSTs only (via $limit_write_subject); the rest count every call.
WRITE_ZONES = ("room_create_w", "msg_send_w", "skill_up_w")
ALL_CALL_ZONES = ("llm_refresh_k", "mcp_k", "a2a_k")
KEYED_ZONES = WRITE_ZONES + ALL_CALL_ZONES

CONFS = [PUBLISHED] + ([LIVE] if LIVE.exists() else [])


def _locations(text: str) -> list[tuple[str, str]]:
    """(header, body) for every location block, however nested."""
    out = []
    for start in range(len(text)):
        if not text.startswith("location", start):
            continue
        brace = text.find("{", start)
        if brace < 0:
            break
        depth, i = 1, brace + 1
        while i < len(text) and depth:
            depth += (text[i] == "{") - (text[i] == "}")
            i += 1
        out.append((text[start:brace].strip(), text[brace + 1:i - 1]))
    return out


@pytest.mark.parametrize("conf", CONFS, ids=lambda p: p.name)
def test_per_path_zones_are_billed_per_key(conf: Path):
    """A caller presenting a key is counted as that key, not as its address:
    agents sharing an office NAT must not spend each other's budget."""
    text = conf.read_text(encoding="utf-8")
    assert '"~*^Bearer' in text and "$limit_subject" in text
    for zone in ALL_CALL_ZONES:
        assert f"limit_req_zone $limit_subject zone={zone}:" in text, zone
    for zone in WRITE_ZONES:
        assert f"limit_req_zone $limit_write_subject zone={zone}:" in text, zone
    # No per-path zone left on the address: $limit_key keeps only the coarse
    # `general` zone, the api_ip ceiling and the connection limits.
    on_address = [ln.strip() for ln in text.splitlines()
                  if ln.startswith("limit_req_zone $limit_key")]
    assert sorted(ln.split("zone=")[1].split(":")[0] for ln in on_address) \
        == ["api_ip", "general"]


def _map_body(text: str, header: str) -> str:
    start = text.index(header)
    return text[text.index("{", start) + 1:text.index("}", start)]


@pytest.mark.parametrize("conf", CONFS, ids=lambda p: p.name)
def test_write_zones_count_posts_only(conf: Path):
    """Reading a room must not spend the budget for posting to it, nor listing
    rooms the one for creating them (report from Navio, 11.11.2026): the write
    zones see a subject for POST and an empty, uncounted one otherwise."""
    text = conf.read_text(encoding="utf-8")
    body = _map_body(text, "map $request_method $limit_write_subject")
    rows = [ln.split("#")[0].split() for ln in body.splitlines()]
    rows = [r for r in rows if r]
    assert ["POST", "$limit_subject;"] in rows
    assert ['default', '"";'] in rows
    assert len(rows) == 2, rows


# Every zone's rate, and per location the zones it applies with their burst.
# Pinned as values, not derived from the file: a test that reads the rate it
# checks against passes whatever the rate becomes.
RATES = {
    "general": "30r/s", "api_ip": "300r/m",
    "room_create_w": "5r/m", "msg_send_w": "10r/m", "skill_up_w": "10r/m",
    "llm_refresh_k": "6r/m", "mcp_k": "60r/m", "a2a_k": "60r/m",
}
MESSAGES_RE = "^/api/rooms/[^/]+/messages$"
REFRESH_RE = "^/api/rooms/[^/]+/context/refresh$"
LOCATION_LIMITS = {
    "location ~ ^/mcp(/.*)?$": {"mcp_k": 30, "api_ip": 100},
    "location ~ ^/a2a(/.*)?$": {"a2a_k": 30, "api_ip": 100},
    f"location ~ {REFRESH_RE}": {"llm_refresh_k": 2, "api_ip": 100},
    "location = /api/skills": {"skill_up_w": 3, "api_ip": 100},
    "location = /api/rooms": {"room_create_w": 3, "api_ip": 100},
    f"location ~ {MESSAGES_RE}": {"msg_send_w": 5, "api_ip": 100},
    "location /": {"general": 60},
}


def _zone_rates(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(
        r"^limit_req_zone \S+ zone=(\w+):\S+\s+rate=(\S+);", text, re.M)}


def _nginx_interval_ms(rate: str) -> int:
    """How long nginx takes to free one slot: it stores the rate as an integer
    of thousandths per second (ngx_http_limit_req_module), 10r/m -> 166."""
    n, per = rate.split("r/")
    milli = int(n) * 1000 // {"s": 1, "m": 60}[per]
    return -(-1000 * 1000 // milli)


def _retry_map(text: str) -> dict[str, int]:
    body = _map_body(text, 'map "$request_method:$uri" $limit_retry')
    out = {}
    for ln in body.splitlines():
        parts = ln.split("#")[0].split()
        if len(parts) == 2:
            out[parts[0].strip('"')] = int(parts[1].rstrip(";"))
    return out


@pytest.mark.parametrize("conf", CONFS, ids=lambda p: p.name)
def test_zones_and_bursts_are_where_they_belong(conf: Path):
    """Each limited location applies exactly its zones with their burst, at
    the pinned rates. Moving msg_send_w off /messages, swapping it for another
    zone or raising a burst must fail here, not in production."""
    text = conf.read_text(encoding="utf-8")
    assert {z: r for z, r in _zone_rates(text).items() if z in RATES} == RATES
    bodies = {}
    for header, body in _locations(text):
        bodies.setdefault(header, body)  # the server-level `location /` first
    for header, want in LOCATION_LIMITS.items():
        got = {m.group(1): int(m.group(2)) for m in re.finditer(
            r"limit_req zone=(\w+) burst=(\d+)", bodies[header])}
        assert got == want, header


@pytest.mark.parametrize("conf", CONFS, ids=lambda p: p.name)
def test_retry_after_matches_the_refusing_zone(conf: Path):
    """Retry-After is when the refusing bucket has room again, not a flat
    minute: a client that slept 60 s turned a 15 s exchange into 65 s (Navio,
    11.11.2026). It must not be shorter than nginx's real interval either —
    a client that sleeps exactly that long would be refused again."""
    text = conf.read_text(encoding="utf-8")
    retry = _retry_map(text)
    rates = _zone_rates(text)

    def ok(seconds: int, zone: str) -> bool:
        need = -(-_nginx_interval_ms(rates[zone]) // 1000)
        return need <= seconds <= need + 1

    assert ok(retry["POST:/api/rooms"], "room_create_w")
    assert ok(retry["POST:/api/skills"], "skill_up_w")
    assert ok(retry[f"~^POST:{MESSAGES_RE[1:]}"], "msg_send_w")
    assert ok(retry[f"~^[A-Z]+:{REFRESH_RE[1:]}"], "llm_refresh_k")
    # Anything else met an all-method zone or a per-address ceiling.
    for zone in ("general", "api_ip", "mcp_k", "a2a_k"):
        assert ok(retry["default"], zone), zone
    assert len(retry) == 5, retry
    # The map's patterns are the locations' own, or a path falls to default.
    headers = {h for h, _ in _locations(text)}
    assert f"location ~ {MESSAGES_RE}" in headers
    assert f"location ~ {REFRESH_RE}" in headers
    limited = dict(_locations(text))["location @rate_limited"]
    assert "add_header Retry-After $limit_retry always;" in limited
    assert '"retry_after":${limit_retry}}' in limited


@pytest.mark.parametrize("conf", CONFS, ids=lambda p: p.name)
def test_keyed_locations_keep_an_address_ceiling(conf: Path):
    """Keys are free to mint, so every location billed per key also carries
    the per-address ceiling — otherwise one address farms keys for budget."""
    for header, body in _locations(conf.read_text(encoding="utf-8")):
        if any(f"limit_req zone={z} " in body for z in KEYED_ZONES):
            assert "limit_req zone=api_ip " in body, header


@pytest.mark.parametrize("conf", CONFS, ids=lambda p: p.name)
def test_nginx_answers_its_own_limits_as_json(conf: Path):
    """429 and 503 generated by nginx itself answer in JSON with Retry-After,
    not the built-in HTML error page."""
    text = conf.read_text(encoding="utf-8")
    bodies = dict(_locations(text))
    for status, name in ((429, "@rate_limited"), (503, "@at_capacity")):
        assert f"error_page {status} = {name};" in text
        body = bodies[f"location {name}"]
        assert "default_type application/json;" in body
        assert f"return {status} '{{\"detail\":" in body
        assert "add_header Retry-After" in body
    # Recursion off (the default, stated): each location answers with the very
    # status it intercepts.
    assert "recursive_error_pages off;" in text


@pytest.mark.skipif(not LIVE.exists(), reason="live copy not present")
def test_live_copy_has_not_drifted_on_the_limits():
    pub = PUBLISHED.read_text(encoding="utf-8")
    live = LIVE.read_text(encoding="utf-8")
    for line in pub.splitlines():
        s = line.strip()
        if s.startswith(("limit_req_zone", "map $http_authorization",
                         "map $limit_exempt", "map $request_method",
                         'map "$request_method:$uri"', "error_page ")):
            assert s in (ln.strip() for ln in live.splitlines()), s
    # The maps that decide who is billed and what a 429 says, row for row.
    for header in ("map $request_method $limit_write_subject",
                   'map "$request_method:$uri" $limit_retry'):
        rows = [_map_body(t, header).splitlines() for t in (pub, live)]
        norm = [[" ".join(r.split("#")[0].split()) for r in body] for body in rows]
        assert [r for r in norm[0] if r] == [r for r in norm[1] if r], header
