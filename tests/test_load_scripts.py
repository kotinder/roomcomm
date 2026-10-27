"""The load test's setup and teardown scripts, run against a throwaway DB.

They are ops scripts (`docker exec -i commroom python - < setup_prod.py`), so
nothing else exercises their raw SQL — and a teardown that silently misses a
table leaves thousands of rooms on the live server. This runs both at a small
size and checks the database is back where it started.
"""
import json
import runpy
from pathlib import Path

import pytest
from sqlmodel import SQLModel, Session, create_engine, select, func

import app.database as database
from app.models import AgentKey, Knock, Message, Room

LOAD = Path(__file__).resolve().parent / "load"
SMALL = {"ROOMS": "4", "SEED_ROOMS": "2", "SEED_MSGS": "5", "KNOCK_KEYS": "3",
         "NOTE": "load test (pytest)"}


@pytest.fixture
def db(tmp_path, monkeypatch, capsys):
    engine = create_engine(f"sqlite:///{tmp_path / 'load.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)
    for k, v in SMALL.items():
        monkeypatch.setenv(k, v)
    capsys.readouterr()
    return engine


def _run(script: str) -> str:
    runpy.run_path(str(LOAD / script), run_name="__main__")


def _counts(engine) -> dict:
    with Session(engine) as s:
        return {m.__name__: s.exec(select(func.count()).select_from(m)).one()
                for m in (AgentKey, Room, Message, Knock)}


def test_setup_then_teardown_leaves_nothing_behind(db, monkeypatch, capsys):
    _run("setup_prod.py")
    cfg = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    after_setup = _counts(db)
    assert after_setup["AgentKey"] == 1 + int(SMALL["KNOCK_KEYS"])
    assert after_setup["Room"] == int(SMALL["ROOMS"]) + 1  # + the acquaintance room
    assert after_setup["Message"] == (int(SMALL["SEED_ROOMS"]) * int(SMALL["SEED_MSGS"])
                                      + int(SMALL["KNOCK_KEYS"]))
    assert len(cfg["knock"]) == int(SMALL["KNOCK_KEYS"])
    assert all(k["key"].startswith("rk_") and len(k["ref"]) == 8 for k in cfg["knock"])

    # The cap counts the stored counter, so a bulk-seeded room must carry it.
    with Session(db) as s:
        for uuid_ in cfg["seeded"]:
            assert s.get(Room, uuid_).messages_count == int(SMALL["SEED_MSGS"])
        assert s.get(Room, cfg["meet_room"]).messages_count == int(SMALL["KNOCK_KEYS"])

    monkeypatch.setenv("KEY_ID", str(cfg["key_id"]))
    _run("teardown_prod.py")
    assert _counts(db) == {"AgentKey": after_setup["AgentKey"],  # revoked, not deleted
                           "Room": 0, "Message": 0, "Knock": 0}
    with Session(db) as s:
        assert all(k.revoked for k in s.exec(select(AgentKey)).all())


def test_teardown_refuses_a_key_that_is_not_the_load_test_key(db, monkeypatch, capsys):
    _run("setup_prod.py")
    capsys.readouterr()
    with Session(db) as s:
        stranger = AgentKey(key_hash="f" * 64, agent_id="someone-real", tier="free")
        s.add(stranger)
        s.commit()
        s.refresh(stranger)
        stranger_id = stranger.id

    monkeypatch.setenv("KEY_ID", str(stranger_id))
    with pytest.raises(AssertionError):
        _run("teardown_prod.py")
    assert _counts(db)["Room"] > 0  # nothing was deleted
