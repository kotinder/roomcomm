"""Per-route latency: the histogram, the merge, and the /admin table.

Own in-memory DB plus a throwaway file DB for the flush, so the suite's
shared one keeps the shape the other modules expect.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, delete, select
from sqlalchemy.pool import StaticPool

import app.main as _main
from app import latency
from app.database import get_session
from app.main import app as fastapi_app
from app.models import RouteStat, utcnow

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
client = TestClient(fastapi_app)
ADMIN = "test-admin-token"
_main.ADMIN_TOKEN = ADMIN

# The real flush, kept aside because the fixture below takes the name away.
_flush = latency.flush


@pytest.fixture(autouse=True)
def _own_db_and_empty_buffer(monkeypatch):
    """Claim the session dependency (other modules point it at their own
    engines), start from an empty histogram and an empty table — and silence
    the background flusher.

    Some other module's request has already started the hit flusher, a daemon
    thread that lives for the rest of the session and calls latency.flush()
    every few seconds. Left alone it would drain the buffer under these tests
    at random, so the name is replaced for their duration and every test that
    wants a flush calls _flush() itself.
    """
    def _override():
        with Session(engine) as s:
            yield s
    fastapi_app.dependency_overrides[get_session] = _override
    monkeypatch.setattr(latency, "flush", lambda: 0)
    with Session(engine) as s:
        s.exec(delete(RouteStat))
        s.commit()
    with latency._lock:
        latency._buf.clear()
        latency._routes.clear()
    latency._alerted.clear()
    yield
    fastapi_app.dependency_overrides.pop(get_session, None)
    with latency._lock:
        latency._buf.clear()
        latency._routes.clear()


@pytest.fixture
def flush_db(tmp_path, monkeypatch):
    """Send the flush at a database of its own."""
    eng = create_engine(f"sqlite:///{tmp_path / 'lat.db'}")
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(latency, "engine", eng)
    return eng


@pytest.fixture
def fake_redis(monkeypatch):
    """A Redis the alert's dedupe can actually use. Only SET NX EX is needed
    here, so no Lua and no lupa."""
    fakeredis = pytest.importorskip("fakeredis")
    client_ = fakeredis.FakeRedis()
    monkeypatch.setattr(latency.shared, "REDIS_URL", "redis://fake")
    monkeypatch.setattr(latency.shared, "_client", client_)
    monkeypatch.setattr(latency.shared, "_down_until", 0.0)
    return client_


def _rows(eng) -> list:
    with Session(eng) as s:
        return list(s.exec(select(RouteStat)).all())


def _store(session, day, route, samples, errors=0):
    """Store samples the way a flush would: one row per occupied bucket."""
    hist: dict[int, int] = {}
    for ms in samples:
        b = latency.bucket_for(ms)
        hist[b] = hist.get(b, 0) + 1
    for bucket, n in hist.items():
        session.add(RouteStat(day=day, route=route, bucket=bucket, count=n,
                              errors=errors if bucket == min(hist) else 0))
    session.commit()


# ---------- buckets and percentiles ----------

def test_a_duration_lands_in_the_bucket_whose_upper_bound_it_is_under():
    assert latency.bucket_for(0) == 0
    assert latency.bucket_for(5) == 0          # the bounds are inclusive ends
    assert latency.bucket_for(5.1) == 1
    assert latency.bucket_for(10) == 1
    # Nothing falls off the scale: past the last bound everything shares the
    # overflow bucket, which is the one that means something is wrong.
    assert latency.bucket_for(10_000) == latency.N_BUCKETS - 2
    assert latency.bucket_for(10_001) == latency.N_BUCKETS - 1
    assert latency.bucket_for(10 ** 9) == latency.N_BUCKETS - 1


def test_percentiles_interpolate_inside_the_bucket_that_holds_them():
    hist = [0] * latency.N_BUCKETS
    hist[0] = 90                 # 90 requests at or under 5 ms
    hist[8] = 10                 # 10 between 150 and 250 ms
    p50, p95 = latency.percentiles(hist)
    assert p50 == 3              # a bit over halfway into the 0-5 ms bucket
    assert p95 == 200            # half of the way from 150 to 250


def test_nothing_recorded_reads_as_no_answer_rather_than_zero():
    assert latency.percentiles([0] * latency.N_BUCKETS) == [None, None]


def test_a_percentile_in_the_overflow_bucket_reports_its_lower_bound():
    hist = [0] * latency.N_BUCKETS
    hist[-1] = 5
    # That bucket has no upper bound to interpolate towards, so the honest
    # answer is "at least 10 s", not a number invented above it.
    assert latency.percentiles(hist) == [10_000, 10_000]


# ---------- labels ----------

def test_the_label_is_the_route_template_not_the_room():
    assert latency.route_label(
        "POST", "/api/rooms/9f1-aaa/messages", {"room_uuid": "9f1-aaa"}, True
    ) == "POST /api/rooms/{room_uuid}/messages"


def test_unmatched_paths_share_one_label():
    # Scanner traffic is unbounded in shape; a label per probe would make the
    # table both useless and unbounded.
    assert latency.route_label("GET", "/wp-admin/setup.php", None, False) \
        == latency.UNROUTED
    assert latency.route_label("GET", "/static/style.css", None, False) \
        == "GET /static/*"


def test_a_flood_of_new_routes_collapses_into_one_label(monkeypatch):
    monkeypatch.setattr(latency, "MAX_ROUTES", 3)
    for i in range(10):
        latency.record(f"GET /r{i}", 1.0, 200)
    labels = {route for _, route, _ in latency._buf}
    assert len(labels) == 4 and latency.OTHER in labels


# ---------- the buffer and the merge ----------

def test_records_are_buffered_then_written_as_one_cell(flush_db):
    for _ in range(3):
        latency.record("GET /api/rooms", 7.0, 200)
    assert _rows(flush_db) == []          # buffered, nothing written yet
    assert _flush() == 1
    (row,) = _rows(flush_db)
    assert (row.route, row.bucket, row.count, row.errors) == \
        ("GET /api/rooms", latency.bucket_for(7.0), 3, 0)


def test_a_later_flush_adds_to_the_cell_instead_of_replacing_it(flush_db):
    latency.record("GET /api/rooms", 7.0, 200)
    _flush()
    latency.record("GET /api/rooms", 7.0, 200)
    _flush()
    # The counts add in SQL, which is what lets both workers flush freely.
    (row,) = _rows(flush_db)
    assert row.count == 2


def test_server_errors_are_counted_beside_the_timing(flush_db):
    latency.record("POST /api/rooms", 7.0, 500)
    latency.record("POST /api/rooms", 7.0, 200)
    latency.record("POST /api/rooms", 7.0, 429)  # a quota refusal is not a fault
    _flush()
    (row,) = _rows(flush_db)
    assert (row.count, row.errors) == (3, 1)


def test_an_empty_buffer_writes_nothing(flush_db):
    assert _flush() == 0
    assert _rows(flush_db) == []


# ---------- the read side ----------

def test_route_table_puts_today_next_to_the_week_as_its_baseline():
    today = utcnow().strftime("%Y-%m-%d")
    earlier = (utcnow() - timedelta(days=3)).strftime("%Y-%m-%d")
    with Session(engine) as s:
        _store(s, earlier, "GET /api/rooms", [7.0] * 100)
        _store(s, today, "GET /api/rooms", [7.0] * 90 + [900.0] * 10, errors=2)
        table = latency.route_table(s)

    (row,) = table["rows"]
    assert row["count"] == 100 and row["week_count"] == 200
    assert row["p50"] == 8                      # the bulk of it, 5-10 ms
    assert row["p95"] == 800                    # today's tail, 600-1000 ms
    assert row["week_p95"] < row["p95"]         # the baseline is still clean
    assert row["errors"] == 2
    assert table["total"] == 100 and table["p95"] == 800


def test_route_table_ranks_by_todays_traffic_and_cuts_the_tail():
    today = utcnow().strftime("%Y-%m-%d")
    with Session(engine) as s:
        _store(s, today, "GET /quiet", [7.0])
        _store(s, today, "GET /busy", [7.0] * 50)
        table = latency.route_table(s, limit=1)
    assert [r["route"] for r in table["rows"]] == ["GET /busy"]


def test_a_route_quiet_today_is_listed_without_a_percentile():
    with Session(engine) as s:
        old = (utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
        _store(s, old, "GET /gone", [7.0] * 10)
        table = latency.route_table(s)
    (row,) = table["rows"]
    # Still listed — it is part of the week — but today it has no number.
    assert (row["count"], row["p50"], row["p95"]) == (0, None, None)
    assert row["week_p95"] is not None


def test_prune_drops_days_past_retention():
    with Session(engine) as s:
        _store(s, "2020-01-01", "GET /old", [7.0])
        _store(s, "2099-01-01", "GET /new", [7.0])
        latency.prune(s, "2098-01-01")
        s.commit()
        left = {r.route for r in s.exec(select(RouteStat)).all()}
    assert left == {"GET /new"}


# ---------- end to end ----------

def test_every_request_is_timed_including_the_ones_no_hit_counts(monkeypatch):
    seen = []
    monkeypatch.setattr(latency, "record",
                        lambda route, ms, status: seen.append((route, ms, status)))
    client.get("/api/rooms")              # a read: no hit event is recorded
    client.get("/wp-admin/setup.php")     # scanner noise: a 404
    by_route = {route: (ms, status) for route, ms, status in seen}
    assert by_route["GET /api/rooms"][1] == 200
    assert by_route[latency.UNROUTED][1] == 404
    assert all(ms > 0 for ms, _ in by_route.values())


def test_admin_shows_the_route_table():
    with Session(engine) as s:
        _store(s, utcnow().strftime("%Y-%m-%d"),
               "POST /api/rooms/{room_uuid}/messages", [7.0] * 20)
    _main._admin_fail_buckets.clear()  # another module's token guesses
    r = client.get("/admin", headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200
    assert "Latency · today" in r.text
    assert "POST /api/rooms/{room_uuid}/messages" in r.text


# ---------- the regression alert ----------
# The baseline days and today, in the shape the alert compares them in.
FAST = [7.0] * 1000                      # p95 lands in the 5-10 ms bucket
SLOW = [7.0] * 900 + [900.0] * 100       # the tail moved into 600-1000 ms


def _two_days(route="POST /api/rooms", prior=FAST, today=SLOW, errors=0):
    with Session(engine) as s:
        if prior:
            _store(s, (utcnow() - timedelta(days=2)).strftime("%Y-%m-%d"),
                   route, prior)
        if today:
            _store(s, utcnow().strftime("%Y-%m-%d"), route, today, errors=errors)
        return latency.regressions(s)


def test_a_route_that_slowed_down_is_reported_against_its_own_baseline():
    (r,) = _two_days(errors=4)
    assert r["route"] == "POST /api/rooms"
    assert r["p95"] == 800
    # The baseline is the earlier day alone. Were today folded into it, the
    # number it is compared against would move with the regression itself.
    assert r["baseline"] == 10
    assert (r["count"], r["errors"], r["days"]) == (1000, 4, 6)


def test_a_route_that_is_merely_relatively_slower_is_not_worth_a_message():
    # Eight times the baseline, and still 39 ms: nobody's problem.
    assert _two_days(prior=[1.0] * 1000, today=[30.0] * 1000) == []


def test_a_route_without_enough_traffic_today_has_no_p95_to_trust():
    # The same shape as SLOW, a tenth of the volume: at 100 requests a p95 is
    # five slow ones, which is a hiccup and not a regression.
    assert _two_days(today=[7.0] * 90 + [900.0] * 10) == []


def test_a_route_without_enough_history_has_nothing_to_compare_against():
    assert _two_days(prior=FAST[:100]) == []


def test_a_route_first_seen_today_is_not_a_regression():
    assert _two_days(prior=[]) == []


def test_scanner_noise_is_never_alerted_on():
    assert _two_days(route=latency.UNROUTED) == []


def test_the_same_route_is_not_alerted_on_twice_in_a_day(monkeypatch):
    sent = []
    monkeypatch.setattr(latency.notify, "send_sync", sent.append)
    _two_days()
    with Session(engine) as s:
        assert latency.alert(s) == ["POST /api/rooms"]
        assert latency.alert(s) == []
    assert len(sent) == 1
    assert "POST /api/rooms" in sent[0] and "800" in sent[0] and "10 ms" in sent[0]


def test_redis_keeps_the_second_worker_and_a_restart_quiet(fake_redis, monkeypatch):
    sent = []
    monkeypatch.setattr(latency.notify, "send_sync", sent.append)
    _two_days()
    with Session(engine) as s:
        assert latency.alert(s) == ["POST /api/rooms"]
        # Another worker, or this one after a restart: its in-process memory
        # is empty, and only the Redis key stops the repeat.
        latency._alerted.clear()
        assert latency.alert(s) == []
    assert len(sent) == 1


def test_the_check_runs_at_most_once_an_interval(monkeypatch):
    calls = []
    monkeypatch.setattr(latency, "alert", lambda s, *a, **k: calls.append(1) or [])
    monkeypatch.setattr(latency, "engine", engine)
    assert latency.maybe_alert() == []        # too soon after import
    assert calls == []
    monkeypatch.setattr(latency, "_last_check",
                        latency.time.monotonic() - latency.ALERT_INTERVAL_S - 1)
    latency.maybe_alert()
    assert calls == [1]
