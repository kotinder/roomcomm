"""Migration guard for the anchors table.

Caught in production: `anchors` shipped in one deploy, the timestamp columns
in the next. create_all() only ever creates missing *tables*, never adds
columns to an existing one, so the second deploy wrote fine in tests (fresh
DB every run) and failed on the real server. This test starts from the old
shape on purpose.
"""
import sqlite3

from app.database import _migrate_sqlite


def test_adds_timestamp_columns_to_an_existing_anchors_table(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    # The anchors table exactly as the first deploy created it.
    con.execute("""CREATE TABLE anchors (
        id INTEGER PRIMARY KEY,
        created_at DATETIME,
        root VARCHAR(64),
        leaf_count INTEGER,
        digest_version VARCHAR(32),
        receipt VARCHAR(500),
        published_via VARCHAR(50)
    )""")
    con.execute("CREATE TABLE rooms (uuid TEXT PRIMARY KEY, is_public BOOLEAN, "
                "protocol_mode TEXT, last_extracted_msg_id INTEGER, "
                "last_extraction_error TEXT, expires_at DATETIME, write_policy TEXT, "
                "write_key_hash TEXT, owner_key_id INTEGER)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, pubkey_hex TEXT, key_id INTEGER)")
    con.execute("CREATE TABLE claim_revisions (id INTEGER PRIMARY KEY, row_hash TEXT)")
    con.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY, subject_key TEXT)")
    con.execute("INSERT INTO anchors (root, leaf_count) VALUES ('abc', 3)")
    con.commit()
    con.close()

    from sqlmodel import create_engine
    import app.database as database
    monkeypatch.setattr(database, "engine", create_engine(f"sqlite:///{db}"))
    _migrate_sqlite()

    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(anchors)").fetchall()}
    assert {"tsa_token", "tsa_url", "tsa_time", "leaves_gz"} <= cols
    # The existing row survives with its root intact.
    assert con.execute("SELECT root FROM anchors").fetchone()[0] == "abc"
    con.close()


def test_migration_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "twice.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE anchors (id INTEGER PRIMARY KEY, root VARCHAR(64))")
    con.execute("CREATE TABLE rooms (uuid TEXT PRIMARY KEY, is_public BOOLEAN, "
                "protocol_mode TEXT, last_extracted_msg_id INTEGER, "
                "last_extraction_error TEXT, expires_at DATETIME, write_policy TEXT)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, pubkey_hex TEXT, key_id INTEGER)")
    con.execute("CREATE TABLE claim_revisions (id INTEGER PRIMARY KEY, row_hash TEXT)")
    con.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY, subject_key TEXT)")
    con.commit()
    con.close()

    from sqlmodel import create_engine
    import app.database as database
    monkeypatch.setattr(database, "engine", create_engine(f"sqlite:///{db}"))
    _migrate_sqlite()
    _migrate_sqlite()  # a second deploy must not blow up on existing columns
