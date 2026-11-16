"""Structured envelopes survive transport/storage and participate in integrity."""
import asyncio
import hashlib
import json
from collections import defaultdict, deque
from datetime import datetime, timezone

from nacl.signing import SigningKey
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from app import anchor, database, mcp_server, pcis, quota
from app.database import get_session
from app.main import app
from app.models import Message
from app.schemas import MessageIn


@pytest.fixture
def env(monkeypatch):
    import app.main as main

    # Isolate the room-create limiter as well as SQLite between test cases.
    monkeypatch.setattr(main, "_create_buckets", defaultdict(deque))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    def session():
        with Session(engine) as s:
            yield s

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = session
    monkeypatch.setattr(mcp_server, "engine", engine)
    monkeypatch.setattr(quota, "QUOTA_MODE", "soft")
    client = TestClient(app)
    created = client.post("/api/rooms", json={})
    assert created.status_code == 201, created.text
    room = created.json()["uuid"]
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        headers={}, client=SimpleNamespace(host="127.0.0.1"))))
    yield client, room, engine, ctx
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    engine.dispose()


def post(client, room, **kwargs):
    return client.post(f"/api/rooms/{room}/messages", json={"agent_id": "tester", **kwargs})


def test_json_only_roundtrip_rest_mcp_and_html(env):
    client, room, engine, ctx = env
    data = {"status": "ready", "items": [1, False, None, {"unsafe": "<script>alert(1)</script>"}]}
    first = post(client, room, data=data, type="document.proposal", thread_id="doc-1")
    assert first.status_code == 201, first.text
    msg = first.json()
    assert msg["text"] == ""
    assert msg["data"] == data
    second = mcp_server.send_message(room, "mcp-agent", ctx=ctx, data={"ok": True},
                                     reply_to=msg["id"], schema_version="2")
    tool = mcp_server.mcp._tool_manager.get_tool("send_message")
    tool.fn_metadata.output_model.model_validate(second)
    assert second["thread_id"] is None  # no implicit routing through reply_to
    rows = client.get(f"/api/rooms/{room}/messages").json()["messages"]
    assert [m["id"] for m in rows] == [msg["id"], second["id"]]
    read = asyncio.run(mcp_server.read_messages(room, ctx))
    mcp_server.mcp._tool_manager.get_tool("read_messages").fn_metadata.output_model.model_validate(read)
    assert read["messages"][0]["data"] == data
    assert rows[1]["reply_to"] == msg["id"]
    html = client.get(f"/{room}", headers={"Accept": "text/html"}).text
    assert 'class="message-data"' in html and "doc-1" in html
    assert f'href="#msg-{msg["id"]}"' in html
    assert "<script>alert(1)</script>" not in html
    with Session(engine) as s:
        assert s.get(Message, msg["id"]).data == data


def test_legacy_text_and_unthreaded_messages_remain_visible(env):
    client, room, _, ctx = env
    assert post(client, room, text="old client").status_code == 201
    mcp_server.send_message(room, "old-mcp", "still works", ctx)
    rows = client.get(f"/api/rooms/{room}/messages").json()["messages"]
    assert [m["text"] for m in rows] == ["old client", "still works"]
    assert all(m["data"] is None and m["type"] == "message" for m in rows)
    assert post(client, room, data={}).status_code == 201


@pytest.mark.parametrize("fields", [
    {}, {"text": ""}, {"data": None}, {"data": []}, {"data": {"x": "a" * 16384}},
    {"data": {"x": float("nan")}}, {"data": {"x": float("inf")}},
    {"data": {}, "type": ""}, {"data": {}, "thread_id": "two words"},
    {"data": {"bad": "\ud800"}}, {"data": {"\ud800": "bad key"}},
    {"data": {}, "schema_version": ""}, {"data": {}, "reply_to": 0},
])
def test_invalid_payload_is_rejected_without_inserting(env, fields):
    client, room, _, ctx = env
    r = client.post(f"/api/rooms/{room}/messages",
                    content=json.dumps({"agent_id": "tester", **fields}),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text
    with pytest.raises(ValueError):
        mcp_server.send_message(room, "tester", ctx=ctx, **fields)
    assert client.get(f"/api/rooms/{room}/messages").json()["messages"] == []


def test_deep_data_rejected():
    value = {}
    for _ in range(34):
        value = {"nested": value}
    with pytest.raises(ValueError, match="nesting"):
        MessageIn(agent_id="a", data=value)


def test_reply_must_be_in_same_room(env):
    client, room, _, ctx = env
    other = client.post("/api/rooms", json={}).json()["uuid"]
    msg = post(client, other, text="other room").json()
    for parent in (msg["id"], 999999):
        assert post(client, room, data={}, reply_to=parent).status_code == 422
        with pytest.raises(ValueError, match="this room"):
            mcp_server.send_message(room, "a", ctx=ctx, data={}, reply_to=parent)


@pytest.mark.parametrize("field,value", [
    ("data", {"changed": True}), ("type", "result"), ("schema_version", "2"),
    ("reply_to", 999), ("thread_id", "another"),
])
def test_every_envelope_field_changes_anchor(env, field, value):
    client, room, engine, _ = env
    msg = post(client, room, data={"original": True}).json()
    with Session(engine) as s:
        before = anchor.room_digest(s, room)
        row = s.get(Message, msg["id"])
        setattr(row, field, value)
        s.add(row)
        s.commit()
        assert anchor.room_digest(s, room) != before


def test_legacy_signature_cannot_claim_to_cover_structured_fields(env):
    client, room, engine, _ = env
    seed = bytes(range(32))
    ts = pcis.iso_canonical(datetime.now(timezone.utc))
    signed = dict(text="hello", ts_iso=ts, pubkey_hex=bytes(SigningKey(seed).verify_key).hex(),
                  signature_hex=pcis.sign(seed, pcis.message_surface("hello", ts, room, None)).hex())
    assert post(client, room, **signed, data={}).status_code == 400
    legacy = post(client, room, **signed)
    assert legacy.status_code == 201, legacy.text
    with Session(engine) as s:
        row = s.get(Message, legacy.json()["id"])
        row.data = {"added_after_signing": True}
        s.add(row)
        s.commit()
    result = client.post(f"/api/rooms/{room}/verify").json()
    assert result["verdict"] == "REFUTED"


def test_v1_digest_still_reproducible_and_versions_not_confused(env):
    client, room, engine, _ = env
    msg = post(client, room, text="legacy").json()
    line = "|".join([str(msg["id"]), "tester", msg["timestamp_canonical"],
                     hashlib.sha256(b"legacy").hexdigest(), "", "", ""])
    expected = hashlib.sha256("\n".join(
        ["rc-anchor-1", room, "1", line, "chain_head="]).encode()).hexdigest()
    with Session(engine) as s:
        assert anchor.room_digest(s, room, "rc-anchor-1") == expected
        from app.models import Anchor
        row = Anchor(root=anchor.merkle_root([(room, expected)]), leaf_count=1,
                     digest_version="rc-anchor-1", leaves_gz=anchor.pack_leaves([(room, expected)]))
        s.add(row)
        s.commit()
    result = client.get(f"/api/rooms/{room}/anchor").json()
    assert result["digest_version"] == "rc-anchor-2"
    assert result["anchored_digest_version"] == "rc-anchor-1"
    assert anchor.verify_proof(room, expected, result["proof"], result["anchor"]["root"])


def test_migration_preserves_old_rows_and_runs_twice(tmp_path, monkeypatch):
    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE rooms (uuid TEXT PRIMARY KEY)")
        conn.exec_driver_sql("CREATE TABLE messages (id INTEGER PRIMARY KEY, room_uuid TEXT, text TEXT)")
        conn.exec_driver_sql("INSERT INTO messages VALUES (1, 'room', 'old')")
    monkeypatch.setattr(database, "engine", old)
    database._migrate_sqlite()
    database._migrate_sqlite()
    with old.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT text, type, data, schema_version, reply_to, thread_id FROM messages").one()
        assert tuple(row) == ("old", "message", None, "1", None, None)
    old.dispose()


def test_v2_digest_can_be_rebuilt_from_public_message_fields(env):
    client, room, engine, _ = env
    msg = post(client, room, data={"z": [False, 1.25], "a": "Привет"},
               type="proposal", thread_id="doc-1").json()
    envelope = {k: msg[k] for k in ("type", "data", "schema_version", "reply_to", "thread_id")}
    line = json.dumps([msg["id"], msg["agent_id"], msg["timestamp_canonical"],
                       hashlib.sha256(msg["text"].encode()).hexdigest(),
                       msg["pubkey_hex"], msg["signature_hex"], msg["memory_root"], envelope],
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    digest = hashlib.sha256("\n".join(
        ["rc-anchor-2", room, "1", line, "chain_head="]).encode()).hexdigest()
    with Session(engine) as s:
        assert anchor.room_digest(s, room) == digest
        row = s.get(Message, msg["id"])
        row.data = {"a": "Привет", "z": [False, 1.25]}
        s.add(row)
        s.commit()
        assert anchor.room_digest(s, room) == digest


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_live_keys_in_json_are_refused_and_revoked(env, transport):
    client, room, _, ctx = env
    raw = client.post("/api/keys", json={"agent_id": "leak-test"}).json()["key"]
    if transport == "rest":
        assert post(client, room, data={"nested": [{"secret": raw}]}).status_code == 422
    else:
        with pytest.raises(ValueError, match="revoked"):
            mcp_server.send_message(room, "a", ctx=ctx, data={"nested": [{"secret": raw}]})
    assert client.get("/api/keys/me", headers={"Authorization": f"Bearer {raw}"}).status_code in (401, 403)
    assert client.get(f"/api/rooms/{room}/messages").json()["messages"] == []


def test_hosted_mcp_tool_input_allows_omitted_text(env):
    _, room, _, ctx = env
    tool = mcp_server.mcp._tool_manager.get_tool("send_message")
    result = asyncio.run(tool.run({"uuid": room, "agent_id": "a", "data": {"ok": True}}, context=ctx))
    assert result["text"] == "" and result["data"] == {"ok": True}


@pytest.mark.parametrize("parent", [2**63, 10**100])
def test_reply_id_outside_sqlite_range_is_client_error(env, parent):
    client, room, _, ctx = env
    response = post(client, room, data={}, reply_to=parent)
    assert response.status_code == 400, response.text
    with pytest.raises(ValueError):
        mcp_server.send_message(room, "a", ctx=ctx, data={}, reply_to=parent)
    assert client.get(f"/api/rooms/{room}/messages").json()["messages"] == []


def test_max_sqlite_reply_id_is_valid_but_missing(env):
    client, room, _, ctx = env
    assert post(client, room, data={}, reply_to=2**63 - 1).status_code == 422
    with pytest.raises(ValueError, match="this room"):
        mcp_server.send_message(room, "a", ctx=ctx, data={}, reply_to=2**63 - 1)


def test_arbiter_nested_json_does_not_amplify_prompt():
    from app import llm
    from app.message_content import MessageContent, canonical_json

    nested = [0] * 7000
    for _ in range(29):
        nested = [nested]
    data = {"values": nested}
    MessageContent(data=data)  # Valid input within both depth and byte limits.
    message = {"id": 1, "agent_id": "agent", "text": "", "data": data}
    payload = llm._build_user_payload(message, [message] * 4, [])
    assert len(payload.encode("utf-8")) < 5 * len(canonical_json(data)) + 2000
    decoded = json.loads(payload)
    assert decoded["new_message"]["data"] == data
    assert all(m["data"] == data for m in decoded["preceding_messages"])


def test_arbiter_receives_structured_current_and_preceding_messages(env, monkeypatch):
    from app import llm
    from app.main import _process_new_messages_for_room
    from app.models import Room

    client, room, engine, _ = env
    original = post(client, room, data={"decision": "approve", "amount": 42},
                    type="decision", schema_version="2", thread_id="purchase-1").json()
    reply = post(client, room, data={"done": True}, type="result",
                 thread_id="purchase-1", reply_to=original["id"]).json()
    received = []

    async def provider(url, key, model, payload, *args, **kwargs):
        received.append(json.loads(payload))
        return {"new_claims": [], "updates": [], "discrepancies": []}

    monkeypatch.setattr(llm, "_call_openai_compat", provider)
    monkeypatch.setattr(llm, "NVIDIA_API_KEY", "test-only")
    with Session(engine) as session:
        result = asyncio.run(_process_new_messages_for_room(session, room))
        assert result["processed_msgs"] == 2
        assert session.get(Room, room).last_extracted_msg_id == reply["id"]
        assert asyncio.run(_process_new_messages_for_room(session, room))["processed_msgs"] == 0
    assert len(received) == 2
    for sent, actual in ((original, received[0]["new_message"]),
                         (original, received[1]["preceding_messages"][0]),
                         (reply, received[1]["new_message"])):
        for field in ("id", "agent_id", "text", "type", "data", "schema_version", "thread_id", "reply_to"):
            assert actual[field] == sent[field], field


@pytest.mark.parametrize("data", [
    {"ticket_id": 9007199254740993, "large": 10**100, "nested": [False, None, 1.25]},
    {"unsafe": "</pre><script>alert(1)</script>", "unicode": "Привет"},
    {},
])
def test_initial_and_live_json_display_preserve_values(env, data):
    import re
    import shutil
    import subprocess
    from html import unescape

    client, room, _, _ = env
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the live message renderer")
    response = post(client, room, data=data, data_json="forged display")
    assert response.status_code == 201, response.text
    page = client.get(f"/{room}", headers={"Accept": "text/html"}).text
    initial = unescape(re.search(r'<pre class="message-data">(.*?)</pre>', page, re.S)[1])
    assert json.loads(initial) == data
    renderer = page[page.index("  function renderMsg("):page.index("  function setIdle(")]
    # Run the actual browser renderMsg with a minimal DOM, parsing the real
    # REST JSON as browsers do. This catches loss after JSON.parse, omitted
    # data nodes and replacement of the rendered value, without a browser dep.
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
function element() {
  return {
    children: [], dataset: {}, textNode: {},
    appendChild(child) { this.children.push(child); },
    querySelector() { return this.textNode; }
  };
}
const thread = element();
const context = {
  document: {createElement: element, createTextNode: s => ({textContent: s})},
  thread, escapeHtml: String, fmtTime: String, paintAgents() {}
};
vm.createContext(context);
vm.runInContext(input.renderer, context);
context.renderMsg(JSON.parse(input.response), true);
const pre = thread.children[0].children.find(c => c.className === 'message-data');
if (!pre) throw new Error('JSON element missing');
process.stdout.write(pre.textContent);
"""
    result = subprocess.run([node, "-e", harness],
                            input=json.dumps({"renderer": renderer, "response": response.text}),
                            text=True, encoding="utf-8", capture_output=True, timeout=15, check=True)
    assert result.stdout == initial
    assert json.loads(result.stdout) == data
    assert "<script>alert(1)</script>" not in page
