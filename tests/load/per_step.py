"""Server-side view of each load step: join sampler.csv with step end times.
    python3 per_step.py sampler.csv steps.txt     (steps.txt: "phase rate t_end")
"""
import csv
import statistics as st
import sys

rows = list(csv.DictReader(open(sys.argv[1])))
for line in open(sys.argv[2]):
    ph, rate, t = line.split()
    t = int(t)
    w = [r for r in rows if t - 55 <= int(r["t"]) <= t - 3]
    if not w:
        continue

    def col(k):
        return [float(r[k]) for r in w]

    codes = sorted({r["rc_root_code"] for r in w} | {r["ig_code"] for r in w})
    print(f"{ph:5s} {rate:>3s}  commroom CPU {st.mean(col('rc_cpu')):4.0f}% "
          f"(max {max(col('rc_cpu')):3.0f})  igra CPU {st.mean(col('ig_cpu')):3.0f}%  "
          f"load {max(col('load1')):4.1f}  local / p50 {st.median(col('rc_root_ms')):5.0f} ms  "
          f"api p50 {st.median(col('rc_api_ms')):5.0f} ms  "
          f"igra p50 {st.median(col('ig_ms')):4.0f} max {max(col('ig_ms')):5.0f} ms  codes {codes}")
