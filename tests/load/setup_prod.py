"""Prepare a load test on the live server, run INSIDE the commroom container:
    docker exec -i commroom python - < setup_prod.py > setup.json

Creates, straight through the ORM (no HTTP, so no Telegram ping per room):
  - one 'trusted' key (no daily quotas), agent_id 'loadtest';
  - ROOMS private standard rooms described 'loadtest …' with a 24h TTL;
  - SEED_MSGS messages in each of the first SEED_ROOMS rooms, so reads return data.
Prints JSON {key, key_id, rooms[]} to stdout. teardown_prod.py removes it all.
"""
import json
import sys
import uuid
from datetime import timedelta

sys.path.insert(0, "/app")
from sqlmodel import Session  # noqa: E402

from app import quota  # noqa: E402
from app.database import engine  # noqa: E402
from app.models import AgentKey, Message, Room, utcnow  # noqa: E402

ROOMS = 40
SEED_ROOMS = 20
SEED_MSGS = 300

raw = quota.generate_key()
with Session(engine) as s:
    key = AgentKey(key_hash=quota.hash_key(raw), agent_id="loadtest", tier="trusted",
                   note="load test 2026-10-05, remove with teardown_prod.py")
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
    s.commit()
    print(json.dumps({"key": raw, "key_id": key.id, "rooms": rooms,
                      "seeded": rooms[:SEED_ROOMS]}))
