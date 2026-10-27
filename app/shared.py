"""State shared across worker processes, kept in Redis.

The app runs as several gunicorn workers (Dockerfile). Anything that must be
counted or held once for the whole service — per-IP rate limits, the admin
lockout, "notify the owner once a day", the per-room LLM refresh lock — lives
here. Without REDIS_URL, or while Redis is unreachable, every helper returns
None and the caller falls back to its old in-process state: limits then hold
per worker (N times softer), never fail closed. A failed call marks Redis down
for DOWN_FOR seconds so a dead Redis costs one short timeout, not one per request.
"""
import logging
import os
import time
from typing import Optional

log = logging.getLogger("roomcomm.shared")

REDIS_URL = os.environ.get("REDIS_URL", "").strip()
PREFIX = "rc:"
DOWN_FOR = 10.0
_client = None
_down_until = 0.0

# Sliding window over a sorted set of event timestamps. Atomic, so concurrent
# workers cannot both squeeze in the last slot. Returns 0 when the event was
# admitted (and recorded), else the seconds until the oldest event expires.
_HIT_LUA = """
local now = tonumber(ARGV[1]); local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3]); local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local n = redis.call('ZCARD', KEYS[1])
if n >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')[2]
  return math.floor(tonumber(oldest) + window - now) + 1
end
redis.call('ZADD', KEYS[1], now, member)
redis.call('EXPIRE', KEYS[1], math.ceil(window))
return 0
"""
_hit_script = None


def _r():
    """The client, or None if not configured / recently failed."""
    global _client, _hit_script
    if not REDIS_URL or time.monotonic() < _down_until:
        return None
    if _client is None:
        import redis
        _client = redis.Redis.from_url(REDIS_URL, socket_timeout=0.25,
                                       socket_connect_timeout=0.25,
                                       health_check_interval=30)
        _hit_script = _client.register_script(_HIT_LUA)
    return _client


def _failed(e: Exception) -> None:
    global _down_until
    if time.monotonic() >= _down_until:
        log.warning("redis unavailable, falling back to per-process state for %.0fs: %r",
                    DOWN_FOR, e)
    _down_until = time.monotonic() + DOWN_FOR


def available() -> bool:
    return _r() is not None


def rate_hit(name: str, subject: str, limit: int, window: int) -> Optional[int]:
    """Record one event for (name, subject) if under `limit` per `window` s.
    0 = admitted, >0 = refused with that Retry-After, None = Redis unavailable."""
    r = _r()
    if r is None:
        return None
    now = time.time()
    try:
        return int(_hit_script(keys=[f"{PREFIX}rl:{name}:{subject}"],
                               args=[now, window, limit, f"{now:.6f}:{os.getpid()}"]))
    except Exception as e:
        _failed(e)
        return None


def rate_count(name: str, subject: str, window: int) -> Optional[int]:
    """Events for (name, subject) in the last `window` s, or None."""
    r = _r()
    if r is None:
        return None
    key = f"{PREFIX}rl:{name}:{subject}"
    try:
        p = r.pipeline()
        p.zremrangebyscore(key, "-inf", time.time() - window)
        p.zcard(key)
        return int(p.execute()[1])
    except Exception as e:
        _failed(e)
        return None


def rate_add(name: str, subject: str, window: int) -> bool:
    """Record an event without a limit check. False if Redis unavailable."""
    r = _r()
    if r is None:
        return False
    key = f"{PREFIX}rl:{name}:{subject}"
    now = time.time()
    try:
        p = r.pipeline()
        p.zadd(key, {f"{now:.6f}:{os.getpid()}": now})
        p.expire(key, int(window) + 1)
        p.execute()
        return True
    except Exception as e:
        _failed(e)
        return False


def first_time(key: str, ttl: int) -> Optional[bool]:
    """True the first time `key` is seen within `ttl` s across all workers,
    False after that, None if Redis unavailable."""
    r = _r()
    if r is None:
        return None
    try:
        return bool(r.set(f"{PREFIX}once:{key}", 1, nx=True, ex=max(1, int(ttl))))
    except Exception as e:
        _failed(e)
        return None


def try_lock(name: str, ttl: int) -> Optional[str]:
    """Acquire a cross-worker lock. A token on success, "" if another worker
    holds it, None if Redis unavailable. Expires after `ttl` s regardless."""
    r = _r()
    if r is None:
        return None
    token = f"{os.getpid()}:{time.time():.6f}"
    try:
        return token if r.set(f"{PREFIX}lock:{name}", token, nx=True, ex=ttl) else ""
    except Exception as e:
        _failed(e)
        return None


_UNLOCK_LUA = ("if redis.call('GET', KEYS[1]) == ARGV[1] then "
               "return redis.call('DEL', KEYS[1]) else return 0 end")


def unlock(name: str, token: str) -> None:
    r = _r()
    if r is None or not token:
        return
    try:
        r.eval(_UNLOCK_LUA, 1, f"{PREFIX}lock:{name}", token)
    except Exception as e:
        _failed(e)


# Daily usage counters (quota.py). One INCR instead of a SQLite write
# transaction per metered request; the SQLite table is caught up in one batch
# every few seconds (quota.flush_counters). A counter absent from Redis is
# seeded from its stored value first, so a Redis restart does not hand
# everyone a fresh budget — see counter_incr / counter_seed_incr below.
_INCR_IF_EXISTS_LUA = ("if redis.call('EXISTS', KEYS[1]) == 1 then "
                       "return redis.call('INCR', KEYS[1]) else return -1 end")
# SET NX then INCR, so two workers seeding the same counter at once cannot
# lose a count: whoever loses the SET still increments the winner's value.
_SEED_INCR_LUA = ("redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX') "
                  "return redis.call('INCR', KEYS[1])")


def counter_incr(key: str) -> Optional[int]:
    """Increment counter `key` and return its new value. -1 when the counter
    does not exist yet (the caller seeds it with counter_seed_incr), None when
    Redis is unavailable."""
    r = _r()
    if r is None:
        return None
    try:
        return int(r.eval(_INCR_IF_EXISTS_LUA, 1, f"{PREFIX}q:{key}"))
    except Exception as e:
        _failed(e)
        return None


def counter_seed_incr(key: str, base: int, ttl: int) -> Optional[int]:
    """Create counter `key` at `base` (expiring in `ttl` s) unless another
    worker just did, then increment. Returns the new value, or None."""
    r = _r()
    if r is None:
        return None
    try:
        return int(r.eval(_SEED_INCR_LUA, 1, f"{PREFIX}q:{key}",
                          base, max(1, int(ttl))))
    except Exception as e:
        _failed(e)
        return None


def counter_get(keys: list[str]) -> Optional[list[Optional[int]]]:
    """Current values of several counters, None per counter that does not
    exist, or None for the whole call when Redis is unavailable."""
    r = _r()
    if r is None or not keys:
        return None if r is None else []
    try:
        raw = r.mget([f"{PREFIX}q:{k}" for k in keys])
        return [None if v is None else int(v) for v in raw]
    except Exception as e:
        _failed(e)
        return None
