#!/usr/bin/env bash
# Potluck health watchdog. Runs from root cron every 5 minutes.
#
# systemd already has Restart=always, which covers a crash and a reboot. What it
# cannot see is a process that is alive but no longer serving — an event loop
# wedged behind a stuck SSE stream looks perfectly healthy to systemd. This probes
# the health endpoint and restarts only after the probe fails three times in a row,
# so a single slow response does not bounce a live pool.
#
# This file lives in the REPOSITORY on purpose. It used to exist only on the server,
# and a deploy with `rsync --delete` removed it — cron then called a missing file
# every five minutes for a week, silently.
set -u
URL="http://127.0.0.1:8001/api/healthz"
LOG="/root/potluck/watchdog.log"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

probe() { curl -s -o /dev/null -m 5 -w '%{http_code}' "$URL" 2>/dev/null; }

code=""
ok=0
for _ in 1 2 3; do
  code="$(probe)"
  [ "$code" = "200" ] && { ok=1; break; }
  sleep 3
done

if [ "$ok" = "1" ]; then
  # Healthy: log once an hour, on the run just after the hour, to keep the file small.
  min=$((10#$(date '+%M')))
  [ "$min" -lt 5 ] && echo "$STAMP OK ($code)" >> "$LOG"
  exit 0
fi

echo "$STAMP UNHEALTHY (last code=$code) — restarting potluck" >> "$LOG"
systemctl restart potluck
sleep 8
echo "$STAMP post-restart health: $(probe)" >> "$LOG"
