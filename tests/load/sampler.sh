#!/bin/bash
# Server-side 1 Hz sampler for the load test. Runs ON the roomcomm host:
#   nohup bash sampler.sh <seconds> <room_uuid> > /root/loadtest/sampler.csv 2>&1 &
# Columns: unix time, commroom/igra CPU % of one core, commroom RSS MiB, load1,
# MemAvailable MiB, WAL bytes, local latency ms of roomcomm / (through nginx),
# of a room's messages API, and of igra-station (node, no nginx), plus HTTP codes.
DUR=${1:-4000}; ROOM=$2
CG=/sys/fs/cgroup/cpuacct/docker
RC=$(docker inspect -f '{{.Id}}' commroom); IG=$(docker inspect -f '{{.Id}}' igra-station)
WAL=/var/lib/docker/volumes/commroom_data/_data/commroom.db-wal
probe() { curl -s -o /dev/null -m 10 -w '%{http_code} %{time_total}' "$@" || echo "000 10"; }
ms() { awk -v t="$1" 'BEGIN{printf "%d", t*1000}'; }
echo "t,rc_cpu,ig_cpu,rc_rss_mb,load1,memavail_mb,wal_b,rc_root_ms,rc_root_code,rc_api_ms,rc_api_code,ig_ms,ig_code"
prc=$(cat $CG/$RC/cpuacct.usage); pig=$(cat $CG/$IG/cpuacct.usage); pt=$(date +%s.%N)
end=$(( $(date +%s) + DUR ))
while [ "$(date +%s)" -lt "$end" ]; do
  sleep 1
  crc=$(cat $CG/$RC/cpuacct.usage); cig=$(cat $CG/$IG/cpuacct.usage); ct=$(date +%s.%N)
  read rcc igc < <(awk -v a=$prc -v b=$crc -v c=$pig -v d=$cig -v t0=$pt -v t1=$ct \
     'BEGIN{dt=(t1-t0)*1e9; printf "%.0f %.0f", (b-a)/dt*100, (d-c)/dt*100}')
  prc=$crc; pig=$cig; pt=$ct
  rss=$(( $(cat /sys/fs/cgroup/memory/docker/$RC/memory.usage_in_bytes) / 1048576 ))
  l1=$(cut -d' ' -f1 /proc/loadavg)
  ma=$(( $(awk '/MemAvailable/{print $2}' /proc/meminfo) / 1024 ))
  wal=$(stat -c %s $WAL 2>/dev/null || echo 0)
  read c1 t1 < <(probe --resolve roomcomm.xyz:443:127.0.0.1 -H 'Accept: text/html' https://roomcomm.xyz/)
  read c2 t2 < <(probe --resolve roomcomm.xyz:443:127.0.0.1 "https://roomcomm.xyz/api/rooms/$ROOM/messages?since=290")
  read c3 t3 < <(probe http://127.0.0.1:3010/arena)
  echo "$(date +%s),$rcc,$igc,$rss,$l1,$ma,$wal,$(ms $t1),$c1,$(ms $t2),$c2,$(ms $t3),$c3"
done
