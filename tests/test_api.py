from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.main import app as fastapi_app
from app.database import get_session

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
SQLModel.metadata.create_all(engine)


def _override():
    with Session(engine) as s:
        yield s


fastapi_app.dependency_overrides[get_session] = _override
client = TestClient(fastapi_app)


def _new_room():
    r = client.post("/api/rooms", json={"description": "test"})
    assert r.status_code == 201
    return r.json()["uuid"]


def test_create_and_info():
    uid = _new_room()
    r = client.get(f"/api/rooms/{uid}")
    assert r.status_code == 200 and r.json()["message_count"] == 0


def test_post_and_list_messages():
    uid = _new_room()
    assert client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a1", "text": "hi"}).status_code == 201
    assert client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a2", "text": "yo"}).status_code == 201
    data = client.get(f"/api/rooms/{uid}/messages").json()
    assert len(data["messages"]) == 2 and data["has_more"] is False
    since = data["messages"][0]["id"]
    data2 = client.get(f"/api/rooms/{uid}/messages?since={since}").json()
    assert len(data2["messages"]) == 1


def test_validation_errors():
    uid = _new_room()
    assert client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a", "text": ""}).status_code == 400
    assert client.post(f"/api/rooms/{uid}/messages", json={"agent_id": "a", "text": "x" * 10001}).status_code == 400
    assert client.get("/api/rooms/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/api/rooms/not-a-uuid").status_code == 400


def test_public_listing_excludes_private():
    # default private
    priv = client.post("/api/rooms", json={"description": "secret"}).json()
    assert priv["is_public"] is False
    pub = client.post("/api/rooms", json={"description": "open", "is_public": True}).json()
    assert pub["is_public"] is True
    listing = client.get("/api/rooms").json()
    uuids = {r["uuid"] for r in listing["rooms"]}
    assert pub["uuid"] in uuids
    assert priv["uuid"] not in uuids
