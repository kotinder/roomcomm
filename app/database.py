from pathlib import Path
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy import event

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SKILLS_DIR = DATA_DIR / "skills"
SKILLS_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "commroom.db"

DATABASE_URL = f"sqlite:///{DB_PATH}"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_db() -> None:
    from . import models  # noqa: F401
    SQLModel.metadata.create_all(engine)
    _migrate_sqlite()


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

        # PCIS-style signatures on messages (Phase: per-message non-repudiation).
        msg_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(messages)").fetchall()}
        if msg_cols and "pubkey_hex" not in msg_cols:
            conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN pubkey_hex VARCHAR(64)")
            conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN signature_hex VARCHAR(128)")
            conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN memory_root VARCHAR(128)")
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


def get_session():
    with Session(engine) as session:
        yield session
