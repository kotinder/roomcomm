"""Stepped open-loop load generator for roomcomm. Runs on a machine OTHER than
the server (sg), needs aiohttp. Config: setup.json from setup_prod.py.

    python3 loadgen.py setup.json results.jsonl [phase ...]

Every phase climbs its rate steps; a step lasts STEP_S seconds at a fixed
arrival rate (open loop: a slow server does not slow the arrivals, which is
what real agents do). A phase stops at its knee — the first step whose error
rate > 1% or p95 > knee threshold — and the run moves on after a cooldown.
Whole-run abort (touch ABORT file, or the canary fails/crawls) stops at once.
"""
import asyncio
import json
import os
import random
import sys
import time

import aiohttp

BASE = os.environ.get("BASE", "https://roomcomm.xyz")
STEP_S = int(os.environ.get("STEP_S", "120"))
COOLDOWN_S = int(os.environ.get("COOLDOWN_S", "90"))
MAX_INFLIGHT = 600
TIMEOUT = aiohttp.ClientTimeout(total=15)
ABORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ABORT")

PHASES = {
    "read":  {"rates": [10, 20, 40, 60, 80, 120, 160, 240], "write_share": 0.0},
    "mixed": {"rates": [10, 20, 40, 60, 80, 120, 160], "write_share": 0.2},
    "write": {"rates": [5, 10, 20, 40, 60, 80, 120], "write_share": 1.0},
    "a2a":   {"rates": [5, 10, 20, 40, 60, 80], "a2a": True},
}
# Several generators split one nominal rate: each runs RATE_MULT of it
# (two machines -> 0.5), so the step labels stay the combined load.
RATE_MULT = float(os.environ.get("RATE_MULT", "1"))
# Override a phase's steps, e.g. RATES_READ=40,60,80,120
for _name, _p in PHASES.items():
    _env = os.environ.get(f"RATES_{_name.upper()}")
    if _env:
        _p["rates"] = [int(x) for x in _env.split(",")]

cfg = json.load(open(sys.argv[1]))
OUT = open(sys.argv[2], "a", buffering=1)
KEY = cfg["key"]
AUTH = {"Authorization": f"Bearer {KEY}"}
SEEDED = cfg["seeded"]
WRITE_ROOMS = [r for r in cfg["rooms"] if r not in SEEDED]
full_rooms: set = set()
BODY = ("load test message, measuring where roomcomm degrades. " * 6)[:320]


def log(**kw):
    kw["t"] = round(time.time(), 1)
    OUT.write(json.dumps(kw) + "\n")
    print(json.dumps(kw), flush=True)


def pick_read():
    r = random.choice(SEEDED)
    x = random.random()
    hdr = AUTH if random.random() < 0.5 else {}
    if x < 0.50:
        return "GET", f"/api/rooms/{r}/messages?since={random.randint(250, 300)}", hdr, None, "msgs_since"
    if x < 0.70:
        return "GET", f"/api/rooms/{r}/messages?limit=100", hdr, None, "msgs_page"
    if x < 0.85:
        return "GET", f"/{r}", {"Accept": "text/html"}, None, "room_html"
    if x < 0.95:
        return "GET", "/api/me/inbox", AUTH, None, "inbox"
    return "GET", "/", {"Accept": "text/html"}, None, "landing"


def pick_write():
    live = [r for r in WRITE_ROOMS if r not in full_rooms] or WRITE_ROOMS
    r = random.choice(live[:8])  # concentrate: 8 hot rooms, like busy negotiations
    body = {"agent_id": f"loadtest-{random.randint(1, 30)}", "text": BODY}
    return "POST", f"/api/rooms/{r}/messages", AUTH, body, "post"


def pick_a2a():
    r = random.choice(SEEDED)
    msg = {"messageId": os.urandom(8).hex(), "role": "ROLE_USER",
           "parts": [{"data": {"op": "read", "room": r, "since": random.randint(250, 300)}}]}
    body = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": msg}}
    return "POST", f"/a2a/{r}", {**AUTH, "A2A-Version": "1.0"}, body, "a2a_read"


class Step:
    def __init__(self):
        self.lat = []          # seconds, successful
        self.codes = {}
        self.kinds = {}
        self.err = 0
        self.dropped = 0
        self.inflight = 0
        self.peak_inflight = 0


async def one(sess, st, req):
    method, path, hdr, body, kind = req
    st.inflight += 1
    st.peak_inflight = max(st.peak_inflight, st.inflight)
    t0 = time.perf_counter()
    try:
        async with sess.request(method, BASE + path, headers=hdr, json=body) as resp:
            data = await resp.read()
            code = resp.status
            if code in (410, 429) and b"room_full" in data:  # roomcomm sends 429
                full_rooms.add(path.split("/")[3])
                code = "410full"
    except asyncio.TimeoutError:
        code = "timeout"
    except aiohttp.ClientError as e:
        code = type(e).__name__
    dt = time.perf_counter() - t0
    st.inflight -= 1
    st.codes[code] = st.codes.get(code, 0) + 1
    k = st.kinds.setdefault(kind, [0, 0])
    k[0] += 1
    if isinstance(code, int) and 200 <= code < 300:
        st.lat.append(dt)
    elif code != "410full":
        st.err += 1
        k[1] += 1


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))] * 1000)


class Canary:
    """Light probe of the landing page every second, outside the load mix."""
    def __init__(self):
        self.recent = []
        self.fails = 0
        self.base = None

    async def run(self, sess):
        while True:
            t0 = time.perf_counter()
            try:
                async with sess.get(BASE + "/", headers={"Accept": "text/html"}) as r:
                    await r.read()
                    ok = r.status == 200
            except Exception:
                ok = False
            dt = time.perf_counter() - t0
            self.fails = 0 if ok else self.fails + 1
            self.recent = (self.recent + [dt if ok else 99])[-10:]
            await asyncio.sleep(1)

    def severe(self):
        if self.fails >= 3:
            return "canary: 3 failures in a row"
        slow = sorted(self.recent)[len(self.recent) // 2] if len(self.recent) >= 5 else 0
        if slow > 5:
            return f"canary median {slow:.1f}s over last 10s"
        return None


async def run_step(sess, canary, phase, rate):
    p = PHASES[phase]
    st = Step()
    tasks = set()
    t_end = time.monotonic() + STEP_S
    interval = 1.0 / (rate * RATE_MULT)
    nxt = time.monotonic()
    abort = None
    while time.monotonic() < t_end:
        if p.get("a2a"):
            req = pick_a2a()
        elif random.random() < p["write_share"]:
            req = pick_write()
        else:
            req = pick_read()
        if st.inflight >= MAX_INFLIGHT:
            st.dropped += 1
        else:
            t = asyncio.create_task(one(sess, st, req))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
        nxt += random.expovariate(1.0 / interval)
        delay = nxt - time.monotonic()
        await asyncio.sleep(max(delay, 0))
        abort = canary.severe() or (os.path.exists(ABORT_FILE) and "ABORT file")
        if abort:
            break
    if tasks:
        await asyncio.wait(tasks, timeout=20)
    sent = sum(st.codes.values())
    res = dict(phase=phase, rate=rate, sent=sent, dropped=st.dropped,
               achieved_rps=round(sent / STEP_S, 1), rate_mult=RATE_MULT,
               p50=pct(st.lat, .5), p95=pct(st.lat, .95), p99=pct(st.lat, .99),
               max=pct(st.lat, 1.0), err=st.err,
               err_rate=round(st.err / sent, 4) if sent else None,
               codes={str(k): v for k, v in st.codes.items()},
               kinds=st.kinds, peak_inflight=st.peak_inflight,
               canary_med_ms=round(sorted(canary.recent)[len(canary.recent) // 2] * 1000)
               if canary.recent else None,
               full_rooms=len(full_rooms), abort=abort or None)
    log(**res)
    return res


async def main():
    phases = sys.argv[3:] or list(PHASES)
    conn = aiohttp.TCPConnector(limit=MAX_INFLIGHT, ttl_dns_cache=300, family=2)  # IPv4
    async with aiohttp.ClientSession(connector=conn, timeout=TIMEOUT) as sess, \
            aiohttp.ClientSession(timeout=TIMEOUT,
                                  connector=aiohttp.TCPConnector(family=2)) as csess:
        canary = Canary()
        ctask = asyncio.create_task(canary.run(csess))
        log(event="baseline_start")
        await asyncio.sleep(int(os.environ.get("BASELINE_S", "60")))
        canary.base = sorted(canary.recent)[len(canary.recent) // 2]
        log(event="baseline", canary_med_ms=round(canary.base * 1000))
        base_p95 = None
        for phase in phases:
            log(event="phase_start", phase=phase)
            for rate in PHASES[phase]["rates"]:
                r = await run_step(sess, canary, phase, rate)
                if r["abort"]:
                    log(event="ABORT", reason=r["abort"])
                    ctask.cancel()
                    return
                if base_p95 is None or rate == PHASES[phase]["rates"][0]:
                    base_p95 = r["p95"] or 300
                knee_ms = max(1000, 4 * base_p95)
                reasons = []
                if r["err_rate"] and r["err_rate"] > 0.01:
                    reasons.append(f"err_rate {r['err_rate']}")
                if r["p95"] and r["p95"] > knee_ms:
                    reasons.append(f"p95 {r['p95']}ms > {knee_ms}")
                if r["dropped"] > 0.01 * (r["sent"] + r["dropped"]):
                    reasons.append("client saturated (MAX_INFLIGHT)")
                if reasons:
                    log(event="knee", phase=phase, rate=rate, reasons=reasons)
                    break
            log(event="cooldown", phase=phase)
            await asyncio.sleep(COOLDOWN_S)
        ctask.cancel()
        log(event="done")


asyncio.run(main())
