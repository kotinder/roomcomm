from pathlib import Path
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy import event

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SKILLS_DIR = DATA_DIR / "skills"
SKILLS_DIR.mkdir(exist_ok=True)
FILES_DIR = DATA_DIR / "files"
FILES_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "commroom.db"

DATABASE_URL = f"sqlite:///{DB_PATH}"
# Pool sized to the overload cap (ROOMCOMM_MAX_INFLIGHT, 32 by default in
# main.py) plus background work (arbiter, notify): SQLite connections are cheap.
# A short pool_timeout fails a starved request in 5 s instead of the default 30 s
# — at 30 s, the 05.10.2026 load test piled up requests until the process hung.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_size=10,
    max_overflow=30,
    pool_timeout=5,
)


# How long a blocked writer waits for SQLite's single write lock before giving
# up with "database is locked". This is pysqlite's own default (its connect()
# timeout of 5 s), pinned here so the value is ours rather than the driver's:
# main.py turns the timeout into a 503 + Retry-After, and the number is what
# bounds how long a saturated writer may sit on an in-flight slot.
BUSY_TIMEOUT_MS = 5000
# WAL checkpointing is kept OFF the request path: a checkpoint lands on
# whichever commit crosses the threshold and stalls writers while it runs,
# which is what the p95 spikes in the 05.10.2026 write phase looked like.
# main.py checkpoints from the stats flusher thread instead; the autocheckpoint
# stays armed, far out, as a safety net for when that thread is not running
# (scripts, tests, a crashed flusher).
WAL_AUTOCHECKPOINT_PAGES = 8000  # ~32 MB of WAL at the default 4 KB page


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    # The documented setting for WAL: no corruption risk; a power cut may lose
    # only the last few commits instead of paying an fsync on every one.
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    cursor.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}")
    # Page cache is PER CONNECTION, and the pool can hold 40 of them in each
    # worker — so this is deliberately modest (8 MB, up from the 2 MB default)
    # rather than "as much as the database". The mapping below is what carries
    # the hot pages: mmap'd reads come straight from the OS page cache, shared
    # by every connection and reclaimable under memory pressure, instead of
    # being copied into 80 private caches.
    cursor.execute("PRAGMA cache_size=-8192")
    cursor.execute("PRAGMA mmap_size=268435456")
    cursor.close()


def wal_checkpoint() -> tuple[int, int]:
    """Fold the WAL back into the database, from a background thread.

    PASSIVE: if a writer holds the lock, the call does nothing rather than
    waiting for it, so checkpointing never blocks a request. Returns
    (busy, pages_moved) — busy=1 means it stepped aside this round.
    """
    with engine.connect() as conn:
        row = conn.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def init_db() -> None:
    """Create tables and run migrations. Every gunicorn worker calls this at
    startup; an exclusive file lock makes them take turns, so two workers never
    race on the same ALTER TABLE (the later ones find everything done)."""
    from . import models  # noqa: F401
    with open(DATA_DIR / ".init.lock", "w") as lock:
        try:
            import fcntl
        except ImportError:  # Windows dev box: single process, nothing to race
            fcntl = None
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            SQLModel.metadata.create_all(engine)
            _migrate_sqlite()
        finally:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _migrate_sqlite() -> None:
    """Forward-only schema migrations for SQLite. Idempotent."""
    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(rooms)").fetchall()}
        if "is_public" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE rooms ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT 0"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_rooms_is_public ON rooms(is_public)"
            )
            conn.commit()
        if "protocol_mode" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE rooms ADD COLUMN protocol_mode VARCHAR(20) NOT NULL DEFAULT 'standard'"
            )
            conn.commit()
        if "last_extracted_msg_id" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE rooms ADD COLUMN last_extracted_msg_id INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()
        if "last_extraction_error" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE rooms ADD COLUMN last_extraction_error VARCHAR(500)"
            )
            conn.commit()
        # Room TTL. Existing rooms are deliberately left at NULL ("never
        # expires"): retroactively expiring live rooms would delete history
        # people are still using. Only rooms created from here on get a date.
        if "expires_at" not in cols:
            conn.exec_driver_sql("ALTER TABLE rooms ADD COLUMN expires_at DATETIME")
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_rooms_expires_at ON rooms(expires_at)"
            )
            conn.commit()

        # Message counter on the room: the 1000-message cap used to cost a
        # COUNT(*) over the room's messages on every post. Backfilled once
        # here — a full scan of messages, seconds on a database this size, and
        # the alternative is a cap that reads as 0 for every existing room.
        if "messages_count" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE rooms ADD COLUMN messages_count INTEGER NOT NULL DEFAULT 0"
            )
            have = {row[1] for row in
                    conn.exec_driver_sql("PRAGMA table_info(messages)").fetchall()}
            if "room_uuid" in have:
                conn.exec_driver_sql(
                    "UPDATE rooms SET messages_count = ("
                    "  SELECT COUNT(*) FROM messages WHERE messages.room_uuid = rooms.uuid)"
                )
            conn.commit()

        # Auth MVP: write policies + key attribution on rooms.
        # (agent_keys / usage_counters are new tables — create_all handles them.)
        if "write_policy" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE rooms ADD COLUMN write_policy VARCHAR(10) NOT NULL DEFAULT 'open'"
            )
            conn.exec_driver_sql(
                "ALTER TABLE rooms ADD COLUMN write_key_hash VARCHAR(64)"
            )
            conn.exec_driver_sql(
                "ALTER TABLE rooms ADD COLUMN owner_key_id INTEGER REFERENCES agent_keys(id)"
            )
            conn.commit()

        # Trusted timestamps on anchors. `anchors` is created by create_all,
        # but only on a database that has never seen it — an existing table is
        # never altered for us, so new columns need this.
        anchor_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(anchors)").fetchall()}
        if anchor_cols and "tsa_token" not in anchor_cols:
            conn.exec_driver_sql("ALTER TABLE anchors ADD COLUMN tsa_token BLOB")
            conn.exec_driver_sql("ALTER TABLE anchors ADD COLUMN tsa_url VARCHAR(200)")
            conn.exec_driver_sql("ALTER TABLE anchors ADD COLUMN tsa_time VARCHAR(40)")
            conn.commit()
        if anchor_cols and "leaves_gz" not in anchor_cols:
            conn.exec_driver_sql("ALTER TABLE anchors ADD COLUMN leaves_gz BLOB")
            conn.commit()

        # PCIS-style signatures on messages (Phase: per-message non-repudiation).
        msg_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(messages)").fetchall()}
        if msg_cols and "pubkey_hex" not in msg_cols:
            conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN pubkey_hex VARCHAR(64)")
            conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN signature_hex VARCHAR(128)")
            conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN memory_root VARCHAR(128)")
            conn.commit()

        # Auth MVP: key attribution on messages.
        if msg_cols and "key_id" not in msg_cols:
            conn.exec_driver_sql(
                "ALTER TABLE messages ADD COLUMN key_id INTEGER REFERENCES agent_keys(id)"
            )
            conn.commit()

        # Arbiter-signed hash chain on revisions.
        rev_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(claim_revisions)").fetchall()}
        if rev_cols and "row_hash" not in rev_cols:
            conn.exec_driver_sql("ALTER TABLE claim_revisions ADD COLUMN prev_hash VARCHAR(64)")
            conn.exec_driver_sql("ALTER TABLE claim_revisions ADD COLUMN row_hash VARCHAR(64)")
            conn.exec_driver_sql("ALTER TABLE claim_revisions ADD COLUMN arbiter_signature_hex VARCHAR(128)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_claim_revisions_row_hash ON claim_revisions(row_hash)")
            conn.commit()
        # Ledger model migration: the old flat `claims` table and `claim_acks`
        # are incompatible with the new thread+revisions schema. Detect and
        # drop them — pre-redesign data was test-only.
        claim_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(claims)").fetchall()}
        if claim_cols and "subject_key" not in claim_cols:
            conn.exec_driver_sql("DROP TABLE IF EXISTS claim_acks")
            conn.exec_driver_sql("DROP TABLE IF EXISTS claims")
            conn.commit()
        # "Rooms this key posted in" (inbox) and "the key's last message in a
        # room" filtered messages by key_id with no index: a full scan of the
        # table, ~60% of every /api/me/inbox (profile 05.10.2026).
        msg_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(messages)").fetchall()}
        if {"key_id", "room_uuid"} <= msg_cols:
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_messages_key_room ON messages(key_id, room_uuid)")
            conn.commit()


def get_session():
    # expire_on_commit=False: handlers commit mid-request (watermarks, metering)
    # and then keep using the rows they loaded. With the default, every later
    # attribute access re-SELECTed its row one by one — the 05.10.2026 profile
    # of a keyed GET /messages spent ~60% in that N+1 reload of the page.
    with Session(engine, expire_on_commit=False) as session:
        yield session
