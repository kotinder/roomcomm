"""Undo setup_prod.py, run INSIDE the commroom container:
    docker exec -i -e KEY_ID=<id> commroom python - < teardown_prod.py

Deletes every room owned by any load-test key — the seeded rooms, the
acquaintance room, and the two-party rooms the knock phase created — with
their child rows in the same order as admin_delete_room, then the knock rows,
the usage counters, and revokes the keys.

The load-test keys are the ones setup_prod.py wrote its NOTE on; KEY_ID is the
main one and is checked by name, so a wrong id stops before deleting anything.
"""
import os
import sys

sys.path.insert(0, "/app")
from app.database import engine  # noqa: E402

key_id = int(os.environ["KEY_ID"])
with engine.begin() as c:
    agent = c.exec_driver_sql("SELECT agent_id FROM agent_keys WHERE id=?", (key_id,)).scalar_one()
    assert agent == "loadtest", f"key {key_id} is {agent!r}, not the load-test key"
    note = c.exec_driver_sql("SELECT note FROM agent_keys WHERE id=?", (key_id,)).scalar_one()
    key_ids = [r[0] for r in c.exec_driver_sql(
        "SELECT id FROM agent_keys WHERE note=? AND (agent_id='loadtest' "
        "OR agent_id LIKE 'loadtest-knock-%')", (note,))]
    assert key_id in key_ids, "the main key does not carry its own note"
    marks = ",".join("?" * len(key_ids))
    ids = tuple(key_ids)  # a list would be read as executemany parameters

    rooms = [r[0] for r in c.exec_driver_sql(
        f"SELECT uuid FROM rooms WHERE owner_key_id IN ({marks})", ids)]
    n_msg = 0
    for u in rooms:
        c.exec_driver_sql("DELETE FROM claim_revisions WHERE claim_id IN "
                          "(SELECT id FROM claims WHERE room_uuid=?)", (u,))
        for t in ("claims", "discrepancies", "handshakes", "room_files", "room_seen",
                  "knocks"):
            c.exec_driver_sql(f"DELETE FROM {t} WHERE room_uuid=?", (u,))
        n_msg += c.exec_driver_sql("DELETE FROM messages WHERE room_uuid=?", (u,)).rowcount
        c.exec_driver_sql("DELETE FROM rooms WHERE uuid=?", (u,))
    for t, col in (("room_seen", "key_id"), ("knock_alerts", "key_id"),
                   ("forgotten", "key_id"), ("forgotten", "forgotten_key_id")):
        c.exec_driver_sql(f"DELETE FROM {t} WHERE {col} IN ({marks})", ids)
    # Knocks whose room is already gone (expired and swept) still point at the keys.
    for col in ("from_key_id", "to_key_id"):
        c.exec_driver_sql(f"DELETE FROM knocks WHERE {col} IN ({marks})", ids)
    for kid in key_ids:
        c.exec_driver_sql("DELETE FROM usage_counters WHERE subject=?", (f"key:{kid}",))
    c.exec_driver_sql(f"UPDATE agent_keys SET revoked=1 WHERE id IN ({marks})", ids)
print(f"deleted rooms={len(rooms)} messages={n_msg}; revoked keys={len(key_ids)}")
