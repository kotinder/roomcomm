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
import os
import threading
import time
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from . import notify, shared
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


def _scan(session: Session, days_back: int) -> tuple[
        dict[str, list[int]], dict[str, list[int]], dict[str, int]]:
    """Today's histogram per route, the earlier days' separately, and today's
    5xx count. Kept apart because a baseline that includes today moves with
    the very thing it is supposed to measure."""
    today = utcnow().strftime("%Y-%m-%d")
    since = (utcnow() - timedelta(days=days_back - 1)).strftime("%Y-%m-%d")
    today_hist: dict[str, list[int]] = {}
    prior_hist: dict[str, list[int]] = {}
    today_err: dict[str, int] = defaultdict(int)
    for row in session.exec(
        select(RouteStat).where(RouteStat.day >= since)
    ).all():
        if row.bucket < 0 or row.bucket >= N_BUCKETS:
            continue  # a row from an older build with different bounds
        into = today_hist if row.day == today else prior_hist
        into.setdefault(row.route, [0] * N_BUCKETS)[row.bucket] += row.count
        if row.day == today:
            today_err[row.route] += row.errors
    return today_hist, prior_hist, today_err


def route_table(session: Session, days_back: int = 7,
                limit: int = 25) -> dict:
    """What /admin shows: today's p50/p95 per route, against the week's p95.

    One number alone says nothing — 300 ms is fine for a page that renders a
    room and alarming for a poll — so every route carries its own recent
    baseline next to today's figure, and a regression reads as the gap
    between the two columns.
    """
    today_hist, prior_hist, today_err = _scan(session, days_back)
    ranked = []
    for route in set(today_hist) | set(prior_hist):
        hist = today_hist.get(route) or [0] * N_BUCKETS
        prior = prior_hist.get(route) or [0] * N_BUCKETS
        week = [a + b for a, b in zip(hist, prior)]
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


# ---------- the alert ----------
# A table nobody opens is not a signal: a regression would sit in /admin until
# somebody happened to look. So the flusher compares today's p95 against the
# days before it and says so, once, in Telegram.
#
# Every number below exists to keep that message worth reading — an alert that
# cries wolf gets muted, and then we are back to having no signal at all.
# A route must have enough traffic today for a p95 to mean anything, enough
# history to have a baseline, and be slow in absolute terms as well as
# relative: three times 4 ms is still 12 ms and nobody's problem.
ALERT_INTERVAL_S = 600
ALERT_MIN_TODAY = 200
ALERT_MIN_BASELINE = 500
ALERT_FLOOR_MS = 150
ALERT_RATIO = 3.0
# Scanner noise and the overflow label are not routes anyone can fix.
ALERT_SKIP = (UNROUTED, OTHER)

PUBLIC_URL = os.environ.get("ROOMCOMM_PUBLIC_URL", "https://roomcomm.xyz").rstrip("/")

# Start the clock at import so a crash-restart loop cannot turn the check into
# a query per boot; the first comparison happens an interval after startup.
_last_check = time.monotonic()
_alerted: set[tuple[str, str]] = set()


def _seconds_to_utc_midnight() -> int:
    now = utcnow()
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
    return max(60, int((midnight - now).total_seconds()))


def regressions(session: Session, days_back: int = 7) -> list[dict]:
    """Routes whose p95 today has pulled away from the days before it."""
    today_hist, prior_hist, today_err = _scan(session, days_back)
    ranked = []
    for route, hist in today_hist.items():
        if route in ALERT_SKIP:
            continue
        # A route first seen today has no baseline at all, which the same
        # check covers: an empty history is not enough history.
        prior = prior_hist.get(route) or [0] * N_BUCKETS
        count, base_count = sum(hist), sum(prior)
        if count < ALERT_MIN_TODAY or base_count < ALERT_MIN_BASELINE:
            continue
        p95 = percentiles(hist, (0.95,))[0]
        baseline = percentiles(prior, (0.95,))[0]
        if p95 is None or baseline is None:
            continue
        if p95 < ALERT_FLOOR_MS or p95 < baseline * ALERT_RATIO:
            continue
        ranked.append((-p95 / baseline, {
            "route": route,
            "p95": p95,
            "baseline": baseline,
            "count": count,
            "errors": today_err[route],
            "days": days_back - 1,
        }))
    return [item for _, item in sorted(ranked, key=lambda pair: pair[0])]


def _claim(route: str) -> bool:
    """True for the one worker that gets to send today's alert for this route.

    Redis both deduplicates across workers and survives a restart, which an
    in-process set does not — without it a container that restarts hourly
    would send the same alert hourly. The fallback is that set anyway, so a
    Redis outage costs at most one repeat per worker per day.
    """
    day = utcnow().strftime("%Y-%m-%d")
    once = shared.first_time(f"latency:{day}:{route}", _seconds_to_utc_midnight())
    if once is not None:
        return once
    if (day, route) in _alerted:
        return False
    _alerted.difference_update({e for e in _alerted if e[0] != day})
    _alerted.add((day, route))
    return True


def alert(session: Session, days_back: int = 7) -> list[str]:
    """Send one Telegram message per regressed route, at most once a day each.
    Returns the routes alerted on. A no-op when Telegram is not configured."""
    sent = []
    for r in regressions(session, days_back):
        if not _claim(r["route"]):
            continue
        notify.send_sync(notify.format_latency_regression(
            admin_url=f"{PUBLIC_URL}/admin", **r))
        log.warning("latency regression on %s: p95 %s ms vs %s ms baseline",
                    r["route"], r["p95"], r["baseline"])
        sent.append(r["route"])
    return sent


def maybe_alert() -> list[str]:
    """Called from the flusher every few seconds; does the real work at most
    once every ALERT_INTERVAL_S, because it costs a query over the window."""
    global _last_check
    if time.monotonic() - _last_check < ALERT_INTERVAL_S:
        return []
    _last_check = time.monotonic()
    with Session(engine) as s:
        return alert(s)
