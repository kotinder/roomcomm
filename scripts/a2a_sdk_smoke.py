"""Official a2a-sdk client against a roomcomm A2A endpoint — wire compatibility.

    pip install "a2a-sdk>=1.2" httpx
    python scripts/a2a_sdk_smoke.py [BASE_URL]      # default http://127.0.0.1:8000

Side effects on the target: issues two free keys (sdk-alice, sdk-bob), creates
one unlisted room and posts four messages into it. Checked on 2026-09-30 with
a2a-sdk 1.2.1 (spec 1.0): 11/11.
"""
import asyncio
import sys
import uuid

import httpx
from google.protobuf import json_format
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types.a2a_pb2 import GetTaskRequest, Message, Part, Role, SendMessageRequest

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
results = []


def check(name, cond, info=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))


def msg(parts, context_id=None, metadata=None):
    d = {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": parts}
    if context_id:
        d["contextId"] = context_id
    if metadata:
        d["metadata"] = metadata
    return SendMessageRequest(message=json_format.ParseDict(d, Message()))


async def send(client, req):
    out = []
    async for ev in client.send_message(req):
        out.append(ev)
    return out


def reply(events):
    ev = events[-1]
    assert ev.HasField("message"), ev
    m = json_format.MessageToDict(ev.message)
    return m, m["parts"][0]["text"], m["parts"][-1].get("data")


async def main():
    async with httpx.AsyncClient(timeout=30) as raw:
        key = (await raw.post(f"{BASE}/api/keys", json={"agent_id": "sdk-alice"})).json()["key"]
        key_b = (await raw.post(f"{BASE}/api/keys", json={"agent_id": "sdk-bob"})).json()["key"]

    hc = httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=30)
    factory = ClientFactory(ClientConfig(httpx_client=hc))  # streaming=True by default
    card = await A2ACardResolver(hc, BASE).get_agent_card()
    check("service card parses", card.name == "Roomcomm", card.supported_interfaces[0].url)
    check("interface is JSONRPC 1.0",
          card.supported_interfaces[0].protocol_binding == "JSONRPC"
          and card.supported_interfaces[0].protocol_version == "1.0")
    check("skills present", len(card.skills) >= 8, ",".join(s.id for s in card.skills))
    client = factory.create(card)

    # 1. create a room through a DataPart op
    m, text, data = reply(await send(client, msg([{"data": {"op": "create_room",
                                                              "description": "SDK e2e room"}}])))
    room = data["uuid"]
    check("create_room via DataPart", m.get("contextId") == room, text[:60])

    # 2. bob posts over the room's own card (text-only client, pointed at the room URL)
    hb = httpx.AsyncClient(headers={"Authorization": f"Bearer {key_b}"}, timeout=30)
    bob = await ClientFactory(ClientConfig(httpx_client=hb)).create_from_url(f"{BASE}/{room}")
    m, text, data = reply(await send(bob, msg([{"text": "hi alice, 100?"}])))
    check("room card + text post (bob)", data["posted"]["agent_id"] == "sdk-bob", text[:60])

    # 3. alice answers by contextId on the service endpoint, gets bob's line back
    m, text, data = reply(await send(client, msg([{"text": "90 and deal"}], context_id=room)))
    check("contextId post + catch-up",
          [x["text"] for x in data["messages"]] == ["hi alice, 100?"], text.replace("\n", " | ")[:90])

    # 4. bob reads with /read — only what is new
    m, text, data = reply(await send(bob, msg([{"text": "/read"}])))
    check("/read from watermark", [x["text"] for x in data["messages"]] == ["90 and deal"], text)

    # 5. inbox
    m, text, data = reply(await send(client, msg([{"data": {"op": "check_inbox"}}])))
    check("check_inbox", data["agent_id"] == "sdk-alice", text[:60])

    # 6. GetTask → TaskNotFoundError
    try:
        await client.get_task(GetTaskRequest(id="nope"))
        check("GetTask -> TaskNotFound", False)
    except Exception as e:  # noqa: BLE001
        check("GetTask -> TaskNotFound", type(e).__name__ == "TaskNotFoundError", type(e).__name__)

    # 7. domain error surfaces with our message
    try:
        await send(client, msg([{"text": "/room"}], metadata={"room": str(uuid.uuid4())}))
        check("missing room -> error", False)
    except Exception as e:  # noqa: BLE001
        check("missing room -> error", "not found" in str(e), f"{type(e).__name__}: {e}"[:120])

    # 8. anonymous client, no Authorization at all
    ha = httpx.AsyncClient(timeout=30)
    anon = await ClientFactory(ClientConfig(httpx_client=ha)).create_from_url(f"{BASE}/{room}")
    m, text, data = reply(await send(anon, msg([{"text": "lurker here"}],
                                               metadata={"agent_id": "anon-sdk"})))
    check("anonymous post with metadata agent_id", data["posted"]["auth"] == "anon")

    # 9. "awaited elsewhere": bob calls alice in the room, alice asks something
    #    unrelated elsewhere and still hears about it
    await send(bob, msg([{"text": "sdk-alice, one more question"}]))
    m, text, data = reply(await send(client, msg([{"text": "/rooms"}])))
    aw = (data or {}).get("awaiting", {})
    check("awaiting rides on an unrelated answer",
          any(x["room_uuid"] == room for x in aw.get("mentions", [])), text.split("\n")[-1][:90])

    for c in (hc, hb, ha):
        await c.aclose()
    print(f"\n{sum(ok for _, ok in results)}/{len(results)} passed")
    sys.exit(0 if all(ok for _, ok in results) else 1)


asyncio.run(main())
