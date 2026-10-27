"""Per-route p50/p95, standing, for the price of a dict increment per request.

Until now the only timing numbers the service ever had came from a load test
someone ran by hand: they say what one box does at 160 rps in a lab, and
nothing about what today's real traffic is paying. This is the standing
version — every request's duration lands in an in-memory histogram keyed by
route, and the background flusher that already writes the hit counters folds
the histogram into `route_stats` every few seconds.

A histogram, not samples, because percentiles do not average: a "p95 of this
flush" cannot be combined with the next flush's, or with the other worker's.
Bucket counts can — they simply add — so the stored rows merge correctly
across workers and across the day, and the percentile is computed at read
time from the sum. The price is resolution: a percentile is only known to lie
inside a bucket, and `percentiles()` interpolates across it, so read the
number as "about this, give or take the bucket's width".

Nothing here may cost a request anything measurable, so recording is a
bisect and a dict update under a lock held for a few microseconds — no I/O,
no thread hop, no per-request row (the whole point of the 2026-10 write-path
work was to stop paying a write transaction per request).
"""
import logging
import threading
from bisect import bisect_left
from collections import defaultdict
from datetime import timedelta
from typing import Optional

from sqlmodel import Session, select

from .database import engine
from .models import RouteStat, utcnow

log = logging.getLogger("roomcomm.latency")

# Upper bounds in milliseconds; bucket i holds (BOUNDS_MS[i-1], BOUNDS_MS[i]]
# and the extra last bucket holds everything slower than the final bound.
# Dense where the answers live (a healthy read is 10-60 ms, see the
# 05.10.2026 profile) and coarse out where only the shape matters.
BOUNDS_MS = (5, 10, 15, 25, 40, 60, 100, 150, 250, 400, 600, 1000, 1500,
             2500, 4000, 6000, 10000)
N_BUCKETS = len(BOUNDS_MS) + 1

# Labels are route templates, so their number is bounded by the route table —
# but a 404 carries no template, and a scanner walking /wp-admin/… would
# otherwise invent a label per probe. Those all collapse into UNROUTED, and
# this cap is the backstop for anything that slips past that.
MAX_ROUTES = 60
UNROUTED = "(unrouted)"
OTHER = "(other)"

_lock = threading.Lock()
# (day, route, bucket) -> [count, errors]; day is part of the key so a flush
# that straddles UTC midnight still files each sample under its own day.
_buf: dict[tuple[str, str, int], list[int]] = {}
_routes: set[str] = set()


def route_label(method: str, path: str, path_params: Optional[dict],
                routed: bool) -> str:
    """A bounded label for a request: the route template, not the raw path.

    `path_params` is what Starlette's router wrote into the ASGI scope when it
    matched, so the values can be swapped back for their names — turning one
    label per room into one label per endpoint. `routed` is False when nothing
    matched at all (every 404 path, i.e. all scanner traffic).
    """
    if path.startswith("/static/"):
        return f"{method} /static/*"
    if not routed:
        return UNROUTED
    if path_params:
        names = {str(v): k for k, v in path_params.items()}
        path = "/".join("{" + names[seg] + "}" if seg in names else seg
                        for seg in path.split("/"))
    return f"{method} {path}"[:60]


def bucket_for(ms: float) -> int:
    return bisect_left(BOUNDS_MS, ms)


def record(route: str, ms: float, status: int) -> None:
    day = utcnow().strftime("%Y-%m-%d")
    bucket = bucket_for(ms)
    with _lock:
        if route not in _routes:
            if len(_routes) >= MAX_ROUTES:
                route = OTHER
            else:
                _routes.add(route)
        cell = _buf.setdefault((day, route, bucket), [0, 0])
        cell[0] += 1
        if status >= 500:
            cell[1] += 1


def flush() -> int:
    """Merge the buffer into route_stats. Returns the number of cells written.

    Called from the hit flusher's thread. One statement per touched cell in
    one transaction — tens of them, every few seconds, against SQLite's single
    writer; the counts add in SQL so two workers flushing at once cannot lose
    each other's. A failed flush drops its buffer, same as the hit buffer:
    this is a signal, not an account.
    """
    with _lock:
        batch = [(day, route, bucket, count, errors)
                 for (day, route, bucket), (count, errors) in _buf.items()]
        _buf.clear()
    if not batch:
        return 0
    try:
        with Session(engine) as s:
            s.connection().exec_driver_sql(
                "INSERT INTO route_stats (day, route, bucket, count, errors) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(day, route, bucket) DO UPDATE SET "
                "count = count + excluded.count, errors = errors + excluded.errors",
                batch,
            )
            s.commit()
    except Exception:
        log.exception("failed to record %d latency cells", len(batch))
        return 0
    return len(batch)


def percentiles(hist: list[int], qs=(0.5, 0.95)) -> list[Optional[int]]:
    """Percentiles in ms from bucket counts, interpolated inside the bucket.

    Returns None per quantile when there is nothing to compute. The overflow
    bucket has no upper bound, so a percentile that lands there reports its
    lower bound — i.e. "at least this slow".
    """
    total = sum(hist)
    if total == 0:
        return [None for _ in qs]
    out: list[Optional[int]] = []
    for q in qs:
        target = q * total
        seen = 0
        value = 0.0
        for i, n in enumerate(hist):
            if n == 0:
                continue
            if seen + n >= target:
                lo = 0.0 if i == 0 else float(BOUNDS_MS[i - 1])
                hi = float(BOUNDS_MS[i]) if i < len(BOUNDS_MS) else lo
                frac = (target - seen) / n
                value = lo + (hi - lo) * frac
                break
            seen += n
        out.append(max(1, round(value)))
    return out


def route_table(session: Session, days_back: int = 7,
                limit: int = 25) -> dict:
    """What /admin shows: today's p50/p95 per route, against the week's p95.

    One number alone says nothing — 300 ms is fine for a page that renders a
    room and alarming for a poll — so every route carries its own recent
    baseline next to today's figure, and a regression reads as the gap
    between the two columns.
    """
    today = utcnow().strftime("%Y-%m-%d")
    since = (utcnow() - timedelta(days=days_back - 1)).strftime("%Y-%m-%d")
    today_hist: dict[str, list[int]] = {}
    week_hist: dict[str, list[int]] = {}
    today_err: dict[str, int] = defaultdict(int)
    for row in session.exec(
        select(RouteStat).where(RouteStat.day >= since)
    ).all():
        if row.bucket < 0 or row.bucket >= N_BUCKETS:
            continue  # a row from an older build with different bounds
        week_hist.setdefault(row.route, [0] * N_BUCKETS)[row.bucket] += row.count
        if row.day == today:
            today_hist.setdefault(row.route, [0] * N_BUCKETS)[row.bucket] += row.count
            today_err[row.route] += row.errors
    ranked = []
    for route, week in week_hist.items():
        hist = today_hist.get(route) or [0] * N_BUCKETS
        p50, p95 = percentiles(hist)
        count = sum(hist)
        ranked.append(((-count, -sum(week), route), {
            "route": route,
            "count": count,
            "p50": p50,
            "p95": p95,
            "errors": today_err[route],
            "week_count": sum(week),
            "week_p95": percentiles(week, (0.95,))[0],
        }))
    rows = [item for _, item in sorted(ranked, key=lambda pair: pair[0])]
    overall = [0] * N_BUCKETS
    for hist in today_hist.values():
        for i, n in enumerate(hist):
            overall[i] += n
    o50, o95 = percentiles(overall)
    return {
        "rows": rows[:limit],
        "days_back": days_back,
        "total": sum(overall),
        "p50": o50,
        "p95": o95,
        "errors": sum(today_err.values()),
    }


def prune(session: Session, cutoff_day: str) -> None:
    session.connection().exec_driver_sql(
        "DELETE FROM route_stats WHERE day < ?", (cutoff_day,))
