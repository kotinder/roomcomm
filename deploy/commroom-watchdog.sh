#!/bin/bash
# Host-side watchdog for the commroom container (systemd: commroom-watchdog.service).
# `--restart unless-stopped` only covers a crash. On 05.10.2026 the process hung
# at 100% CPU, alive but serving nobody, until a manual restart. This polls
# /healthz (which answers even while the app sheds load) and restarts the
# container after FAILS_TO_RESTART misses in a row, then alerts the owner.
# Leaves a container alone while it is stopped or freshly (re)started, so a
# deploy (stop/rm/run) never races it.
# Overridable from the environment only to test the watchdog on a throwaway
# container; the systemd unit runs it with these defaults.
C=${WD_CONTAINER:-commroom}
URL=${WD_URL:-http://127.0.0.1:8000/healthz}
INTERVAL=${WD_INTERVAL:-10}
FAILS_TO_RESTART=3
GRACE=${WD_GRACE:-90}   # seconds after a container start before it may be judged
LOG=${WD_LOG:-/root/incidents/watchdog.log}
ENV=/home/deploy/commroom/.env
ALERT=${WD_ALERT:-1}

alert() {
  [ "$ALERT" = 1 ] || return 0
  local tok chat
  tok=$(grep -E '^TG_BOT_TOKEN=' "$ENV" | cut -d= -f2- | tr -d '"'"'"'\r ')
  chat=$(grep -E '^TG_CHAT_ID=' "$ENV" | cut -d= -f2- | tr -d '"'"'"'\r ')
  [ -n "$tok" ] && [ -n "$chat" ] && curl -s -m 10 -o /dev/null \
    "https://api.telegram.org/bot$tok/sendMessage" \
    --data-urlencode "chat_id=$chat" --data-urlencode "text=$1"
}

fails=0
while true; do
  sleep "$INTERVAL"
  running=$(docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null)
  [ "$running" = "true" ] || { fails=0; continue; }
  started=$(date -d "$(docker inspect -f '{{.State.StartedAt}}' "$C")" +%s 2>/dev/null || echo 0)
  [ $(( $(date +%s) - started )) -ge "$GRACE" ] || { fails=0; continue; }
  if curl -sf -m 5 -o /dev/null "$URL"; then
    fails=0
    continue
  fi
  fails=$((fails + 1))
  echo "$(date -u +%FT%TZ) healthz miss $fails/$FAILS_TO_RESTART" >> "$LOG"
  if [ "$fails" -ge "$FAILS_TO_RESTART" ]; then
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    docker logs --since 10m "$C" > "/root/incidents/hang-$stamp.log" 2>&1
    docker restart -t 10 "$C" >> "$LOG" 2>&1
    echo "$(date -u +%FT%TZ) RESTARTED $C (logs: hang-$stamp.log)" >> "$LOG"
    alert "roomcomm: $C не отвечал на /healthz $((FAILS_TO_RESTART * INTERVAL)) с — перезапущен сторожем. Логи: /root/incidents/hang-$stamp.log"
    fails=0
  fi
done
