"""Undo setup_prod.py, run INSIDE the commroom container:
    docker exec -i -e KEY_ID=<id> commroom python - < teardown_prod.py

Deletes every room owned by the load-test key (and their child rows, same
order as admin_delete_room), the key's usage counters, and revokes the key.
"""
import os
import sys

sys.path.insert(0, "/app")
from app.database import engine  # noqa: E402

key_id = int(os.environ["KEY_ID"])
with engine.begin() as c:
    agent = c.exec_driver_sql("SELECT agent_id FROM agent_keys WHERE id=?", (key_id,)).scalar_one()
    assert agent == "loadtest", f"key {key_id} is {agent!r}, not the load-test key"
    rooms = [r[0] for r in c.exec_driver_sql(
        "SELECT uuid FROM rooms WHERE owner_key_id=? AND description LIKE 'loadtest room %'",
        (key_id,))]
    n_msg = 0
    for u in rooms:
        c.exec_driver_sql("DELETE FROM claim_revisions WHERE claim_id IN "
                          "(SELECT id FROM claims WHERE room_uuid=?)", (u,))
        for t in ("claims", "discrepancies", "handshakes", "room_files", "room_seen"):
            c.exec_driver_sql(f"DELETE FROM {t} WHERE room_uuid=?", (u,))
        n_msg += c.exec_driver_sql("DELETE FROM messages WHERE room_uuid=?", (u,)).rowcount
        c.exec_driver_sql("DELETE FROM rooms WHERE uuid=?", (u,))
    c.exec_driver_sql("DELETE FROM room_seen WHERE key_id=?", (key_id,))
    c.exec_driver_sql("DELETE FROM usage_counters WHERE subject=?", (f"key:{key_id}",))
    c.exec_driver_sql("UPDATE agent_keys SET revoked=1 WHERE id=?", (key_id,))
print(f"deleted rooms={len(rooms)} messages={n_msg}; key {key_id} revoked")
