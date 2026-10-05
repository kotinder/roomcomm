#!/bin/bash
# Start one load generator in the background on this machine (run from its
# loadtest dir). Several machines share the nominal rate via RATE_MULT.
#   bash start_gen.sh <results.jsonl> <log> <rate_mult> <phase> [phase ...]
cd "$(dirname "$0")" || exit 1
if pgrep -f '^python3 loadgen' >/dev/null; then echo "ALREADY RUNNING"; exit 1; fi
out=$1; logf=$2; mult=$3; shift 3
rm -f ABORT
RATES_READ=${RATES_READ:-} RATES_WRITE=${RATES_WRITE:-} RATE_MULT=$mult STEP_S=${STEP_S:-60} COOLDOWN_S=${COOLDOWN_S:-45} BASELINE_S=${BASELINE_S:-30} \
  setsid nohup nice -n 5 python3 loadgen.py setup.json "$out" "$@" > "$logf" 2>&1 < /dev/null &
sleep 2
echo "$(hostname) $(date -u +%T): $(pgrep -af '^python3 loadgen')"
