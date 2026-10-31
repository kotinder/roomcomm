"""Prepare a load test on the live server, run INSIDE the commroom container:
    docker exec -i commroom python - < setup_prod.py > setup.json

Creates, straight through the ORM (no HTTP, so no Telegram ping per room):
  - one 'trusted' key (no daily quotas), agent_id 'loadtest';
  - ROOMS private standard rooms described 'loadtest …' with a 24h TTL;
  - SEED_MSGS messages in each of the first SEED_ROOMS rooms, so reads return data;
  - KNOCK_KEYS more trusted keys, all mutually acquainted through one shared
    room, for the knock phase (a knock only reaches a key you share a room
    with). KNOCK_KEYS * (KNOCK_KEYS - 1) ordered pairs is the phase's budget,
    one knock each.

The knock phase also needs the generator's IP in RATE_EXEMPT_IPS on the
server, not just in nginx's geo whitelist: /api/knocks goes through main's own
per-IP bucket for room creation (30/hour), which would otherwise be what the
phase measures.

Prints JSON {key, key_id, rooms[], seeded[], knock[], knock_key_ids[]} to
stdout. teardown_prod.py removes all of it.
"""
import json
import os
import sys
import uuid
from datetime import timedelta

sys.path.insert(0, "/app")
from sqlmodel import Session  # noqa: E402

from app import authorship, quota  # noqa: E402
from app.database import engine  # noqa: E402
from app.models import AgentKey, Message, Room, utcnow  # noqa: E402

# Sized for a full ladder against prod; a rehearsal (or the test that keeps
# this script honest) shrinks them through the environment.
ROOMS = int(os.environ.get("ROOMS", 40))
SEED_ROOMS = int(os.environ.get("SEED_ROOMS", 20))
SEED_MSGS = int(os.environ.get("SEED_MSGS", 300))
KNOCK_KEYS = int(os.environ.get("KNOCK_KEYS", 180))
NOTE = os.environ.get("NOTE", "load test 2026-10-27, remove with teardown_prod.py")

raw = quota.generate_key()
with Session(engine) as s:
    key = AgentKey(key_hash=quota.hash_key(raw), agent_id="loadtest", tier="trusted",
                   note=NOTE)
    s.add(key)
    s.commit()
    s.refresh(key)
    rooms = []
    exp = utcnow() + timedelta(hours=24)
    for i in range(ROOMS):
        r = Room(uuid=str(uuid.uuid4()), description=f"loadtest room {i}",
                 owner_key_id=key.id, expires_at=exp)
        s.add(r)
        rooms.append(r.uuid)
    s.commit()
    body = "load test filler message. " * 12
    for u in rooms[:SEED_ROOMS]:
        s.add_all(Message(room_uuid=u, agent_id=f"loadtest-seed-{j % 4}",
                          text=f"#{j} {body}", key_id=key.id) for j in range(SEED_MSGS))
    # rooms.messages_count is what the 1000-message cap is checked against
    # (app/posting.py); a bulk insert like the one above bypasses it, so a
    # seeded room would otherwise look empty to the cap.
    for u in rooms[:SEED_ROOMS]:
        s.get(Room, u).messages_count = SEED_MSGS
    s.commit()

    # Knock phase: keys that know each other. Acquaintance = "posted, with its
    # own key, into a room I also posted into", so one room with one message
    # from every key makes all pairs reachable. Deliberately a worst case for
    # the acquaintance lookup: each key has KNOCK_KEYS - 1 of them, far more
    # than a real agent.
    knockers = []
    for i in range(KNOCK_KEYS):
        k_raw = quota.generate_key()
        k = AgentKey(key_hash=quota.hash_key(k_raw), agent_id=f"loadtest-knock-{i}",
                     tier="trusted", note=NOTE)
        s.add(k)
        knockers.append((k_raw, k))
    s.commit()
    meet = Room(uuid=str(uuid.uuid4()), description="loadtest knock acquaintances",
                owner_key_id=key.id, expires_at=exp)
    s.add(meet)
    s.commit()
    for k_raw, k in knockers:
        s.refresh(k)
        s.add(Message(room_uuid=meet.uuid, agent_id=k.agent_id,
                      text="load test: here so knocks can reach me.", key_id=k.id))
    s.get(Room, meet.uuid).messages_count = len(knockers)
    s.commit()

    print(json.dumps({
        "key": raw, "key_id": key.id, "rooms": rooms, "seeded": rooms[:SEED_ROOMS],
        "knock": [{"key": k_raw, "ref": authorship.key_ref(k)} for k_raw, k in knockers],
        "knock_key_ids": [k.id for _, k in knockers],
        "meet_room": meet.uuid,
    }))
