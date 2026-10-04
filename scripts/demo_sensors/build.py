"""Render the sensors demo room from D0 (its creation day) and, optionally, publish it.

Dates in the text are generated, never typed: the room's timestamps are the
story's "today", so every date must follow from D0.

    python build.py                      # next valid D0 from today: check + print
    python build.py --d0 2026-10-09      # render for a given D0
    python build.py --publish            # D0 = today (UTC+8); refuses an invalid day
        env ROOMCOMM_KEY      Telegram-verified key (public rooms need one)
        env ROOMCOMM_ADMIN    admin token, to pin the room (ttl never)

Room is created public with write_policy='key': only the returned room key can
post, and it is used for the script only, then discarded.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://roomcomm.xyz"
CN = dt.timezone(dt.timedelta(hours=8))

# Mainland calendar 2026, 国办发明电〔2025〕7 号. Extend yearly.
def _days(m, a, b, y=2026):
    return {dt.date(y, m, d) for d in range(a, b + 1)}


HOLIDAYS = (_days(1, 1, 3) | _days(2, 15, 23) | _days(4, 4, 6) | _days(5, 1, 5)
            | _days(6, 19, 21) | _days(9, 25, 27) | _days(10, 1, 7))
MAKEUP = {dt.date(2026, m, d) for m, d in ((1, 4), (2, 14), (2, 28), (5, 9), (9, 20), (10, 10))}  # 调休上班


def load():
    with open(os.path.join(HERE, "scenario.json"), encoding="utf-8") as f:
        return json.load(f)


def cn_date(d):
    return f"{d.month} 月 {d.day} 日"  # CJK/number spacing, as in the rest of the copy


def workday(d):
    return (d.weekday() < 5 and d not in HOLIDAYS) or d in MAKEUP


def check(d0, off):
    """The story's own arithmetic plus the calendar."""
    dl, air, t200 = (d0 + dt.timedelta(days=off[k]) for k in ("DL", "AIR", "T200"))
    problems = []
    # air: pickup tomorrow + next-day delivery, and "two days ahead of the deadline"
    if air != d0 + dt.timedelta(days=2):
        problems.append("air must be D0+2 (明天提货 + 次日达)")
    if (dl - air).days != 2:
        problems.append("air must land exactly two days before the deadline (提前两天)")
    # road: loads today, 4–6 days → best case lands exactly on the deadline (卡着, 没有余量)
    if d0 + dt.timedelta(days=4) != dl:
        problems.append("road best case (D0+4) must equal the deadline")
    # T200 remainder: '交期约 12 天' and it must miss the deadline
    if (t200 - d0).days != 12 or t200 <= dl:
        problems.append("T200 remainder must be D0+12 and after the deadline")
    for name, d in (("D0 (negotiation)", d0), ("deadline/验收", dl), ("T200 补发", t200)):
        if not workday(d):
            problems.append(f"{name} {d} is a weekend or holiday")
    return problems, {"DL": dl, "AIR": air, "T200": t200}


def render(d0):
    sc = load()
    problems, dates = check(d0, sc["offsets"])
    sub = {k: cn_date(v) for k, v in dates.items()}

    def fill(s):
        out = s.format(**sub)
        assert not re.search(r"\{\w+\}", out), out
        return out

    return problems, dates, fill(sc["description"]), [(a, fill(t)) for a, t in sc["messages"]]


def call(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method, headers=headers or {},
                                 data=None if body is None else body)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def publish(desc, msgs):
    key = os.environ["ROOMCOMM_KEY"]
    admin = os.environ.get("ROOMCOMM_ADMIN")
    auth = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    room = call("POST", "/api/rooms", json.dumps(
        {"description": desc, "is_public": True, "write_policy": "key"}, ensure_ascii=False).encode(), auth)
    uuid, room_key = room["uuid"], room["write_key"]
    print("room", uuid)
    for agent, text in msgs:
        call("POST", f"/api/rooms/{uuid}/messages",
             json.dumps({"agent_id": agent, "text": text}, ensure_ascii=False).encode(),
             {**auth, "X-Room-Key": room_key})
        time.sleep(7)  # stay under the 10-posts/60 s per-IP limit
    if admin:
        req = urllib.request.Request(f"{BASE}/admin/rooms/{uuid}/ttl", method="POST",
                                     data=b"ttl_hours=never",
                                     headers={"Authorization": f"Bearer {admin}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        print("ttl", urllib.request.urlopen(req, timeout=30).read().decode())
    print(f"{BASE}/{uuid}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--d0")
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args()
    today = dt.datetime.now(CN).date()
    if a.publish:
        d0 = today
    elif a.d0:
        d0 = dt.date.fromisoformat(a.d0)
    else:
        d0 = today
        while render(d0)[0]:
            d0 += dt.timedelta(days=1)
    problems, dates, desc, msgs = render(d0)
    print(f"D0 {d0} ({d0:%a})  deadline {dates['DL']} ({dates['DL']:%a})  "
          f"air {dates['AIR']} ({dates['AIR']:%a})  T200 {dates['T200']} ({dates['T200']:%a})")
    for p in problems:
        print("  ✗", p)
    if a.publish:
        if problems:
            sys.exit("refusing to publish: the story does not hold for today")
        publish(desc, msgs)
    else:
        print(desc)
        for agent, text in msgs:
            print(f"[{agent}] {text}")


if __name__ == "__main__":
    main()
