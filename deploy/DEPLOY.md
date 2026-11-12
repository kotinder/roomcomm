# Deploying roomcomm

How a change gets from this repository to the running service: one Docker
container behind nginx on a single host. No CI deploys anything; a person (or
an agent the owner asked to deploy in that session — see AGENTS.md) runs the
steps below by hand, from a shell with `ssh`, `scp` and `git` (Git Bash works).

This file holds no secrets and no host details: the ssh target and the paths
on the host live in `deploy/host.env`, which `.gitignore` keeps local
(`*.env`). Copy `deploy/host.env.example` and fill it in. The application's
own secrets (`ROOMCOMM_ADMIN_TOKEN`, LLM keys, the Telegram token and so on)
stay in the `.env` next to the build context on the host and never leave it.

## Variables

Set once per deploy, in the shell you run every step from:

```bash
set -a; . deploy/host.env; set +a   # HOST, APP_DIR, NGINX_SITE, NGINX_BACKUPS
WHAT=longpoll                       # a word for this change: names backups and tags
T=$(date +%Y%m%d)
REV=$(git rev-parse --short HEAD)
PRE=pre-$WHAT-$T                    # backups and the rollback image tag
```

**Write `PRE` into the log at the end of this file before step 5.** A
rollback from a new shell, or after midnight, recomputes `T` to a different
day and finds no image to go back to; the logged name is what it uses.

## What runs where

| Piece | Where | Notes |
|---|---|---|
| Build context | `$APP_DIR` on the host: `app/`, `static/`, `scripts/`, `Dockerfile`, `requirements.txt`, `.env` | not a git checkout; files are copied in. The Dockerfile copies `app`, `static` and `scripts` into the image |
| Image | `commroom:latest`, plus a tag per deploy | old tags are the rollback |
| Container | `commroom`, network `commroom-net`, `127.0.0.1:8000` | started with `--env-file $APP_DIR/.env` |
| Data | Docker volume `commroom_data` → `/app/data` | SQLite `commroom.db`, files, skills, arbiter key; owned by 1000:1000. **Never recreate the volume** |
| Redis | container `commroom-redis` on `commroom-net` | rate-limit and counter state shared by workers |
| nginx | `$NGINX_SITE` (a regular file in `sites-enabled`) | shares the host with other sites |
| Watchdog | systemd `commroom-watchdog` (`deploy/commroom-watchdog.sh`) | restarts the container after ~30 s without `/healthz`; stopped during a swap |

The build context, `/etc/nginx` and systemd need root on the host; the
unprivileged account can run `docker` but cannot write either.

## 1. Before touching the host

1. The gates are clean on the commit you deploy (`pytest -q`, `ruff check .`,
   `mypy app`; AGENTS.md). Deploy a commit, not a working tree.
2. **The host may be newer than git.** Hand-made fixes have landed there
   before. Compare what runs with the commit your change started from, and
   look at every difference before overwriting it:

   ```bash
   mkdir -p /tmp/base && git archive <base> app static scripts Dockerfile requirements.txt | tar -x -C /tmp/base
   ssh $HOST "cd $APP_DIR && find app static scripts Dockerfile requirements.txt -type f ! -path '*__pycache__*' -exec sha256sum {} +"
   ```

   Compare contents with carriage returns stripped (`tr -d '\r'`): files
   copied from a Windows checkout carry CRLF, and a hash difference alone
   means nothing. Known on 12.11.2026: `static/favicon.svg` on the host
   differs from the repo; shipping `static/` replaces it.
3. **nginx: the host's file is the base, not `deploy/nginx-commroom.live.conf`.**
   The live copy in the repo lags whatever was added on the host for other
   sites (12.11.2026: a denylist include, a proxy for another service, a
   webhook route). Fetch `$NGINX_SITE`, apply your change to it, and check
   that `diff` against the fetched file shows only your lines. Save the
   result as `deploy/nginx-commroom.live.conf` — that file is what step 6
   ships — and run `pytest tests/test_deploy_conf.py`, which also checks it
   against the published config.
4. A renamed `limit_req_zone` must not be referenced anywhere else:
   `ssh $HOST "grep -rn '<old zone name>' /etc/nginx/"`. Zones are renamed
   rather than re-keyed because nginx refuses, on reload, a zone whose key
   changed.

## 2. Back up

```bash
ssh $HOST "set -e
  for f in app static scripts Dockerfile requirements.txt; do cp -a $APP_DIR/\$f $APP_DIR/\$f.bak-$PRE; done
  docker tag commroom:latest commroom:$PRE
  mkdir -p $NGINX_BACKUPS
  cp -a $NGINX_SITE $NGINX_BACKUPS/commroom.$PRE"
```

Keep nginx backups **outside** `sites-enabled`: everything in it is loaded.

Database, through SQLite's backup API (a plain `cp` of a live WAL database
can copy a torn state), then an integrity check of the copy:

```bash
ssh $HOST "set -e
  docker exec commroom python -c \"import sqlite3; s=sqlite3.connect('/app/data/commroom.db'); d=sqlite3.connect('/app/data/predeploy-$PRE.db'); s.backup(d); d.close()\"
  docker exec commroom python -c \"import sqlite3; print(sqlite3.connect('file:/app/data/predeploy-$PRE.db?mode=ro', uri=True).execute('pragma integrity_check').fetchone()[0])\""
```

It must print `ok`. The copy is about the size of the database; check
`df -h /` first. It is named by `PRE`, not by date: a second deploy on the
same day must not overwrite the first one's backup.

## 3. Ship and build

```bash
git archive --format=tar -o release.tar HEAD app static scripts Dockerfile requirements.txt
scp release.tar $HOST:/tmp/commroom-release-$REV.tar
ssh $HOST "set -e
  tar -xf /tmp/commroom-release-$REV.tar -C $APP_DIR --no-same-owner
  cd $APP_DIR && docker build -q -t commroom:latest -t commroom:$WHAT-$REV ."
```

`tar` overwrites and adds; a file deleted in git stays on the host until you
remove it there.

## 4. Smoke-test the image before swapping

A throwaway container on a free port, with a scratch data directory owned by
the app user (without it the app cannot create `/app/data/skills` and exits):

```bash
ssh $HOST "P=18765
  ss -ltn | grep -q \":\$P \" && { echo port \$P busy; exit 1; }
  docker run -d --name commroom-smoke --tmpfs /app/data:uid=1000,gid=1000 -p 127.0.0.1:\$P:8000 commroom:$WHAT-$REV >/dev/null
  ok=1; for i in \$(seq 1 30); do sleep 1; curl -fsS -o /dev/null http://127.0.0.1:\$P/healthz && { ok=0; break; }; done
  docker logs commroom-smoke 2>&1 | tail -20; docker rm -f commroom-smoke >/dev/null
  exit \$ok"
```

It exits non-zero if `/healthz` never answered. Check that the port is free
first: on 12.11.2026 another service on the host answered on the first port
tried, and its 404s looked like the smoke test's.

## 5. Swap the container

Downtime: until the old container has drained (held long-polls answer within
30 s, gunicorn's `--graceful-timeout` is 35 s) plus a few seconds of start.
`docker stop -t 45` leaves gunicorn its full graceful window before Docker
kills it. The watchdog is stopped for the swap: while the old container
drains it is "running" but no longer answers `/healthz`, and three misses
(~30 s) make the watchdog restart it in the middle of the deploy.

Write `PRE` into the log first (see Variables).

```bash
ssh $HOST "set -e
  systemctl stop commroom-watchdog
  docker stop -t 45 commroom
  docker rm commroom
  docker run -d --name commroom --restart unless-stopped --network commroom-net \
    -p 127.0.0.1:8000:8000 -v commroom_data:/app/data --env-file $APP_DIR/.env commroom:latest
  ok=1; for i in \$(seq 1 40); do sleep 1; curl -fsS -o /dev/null http://127.0.0.1:8000/healthz && { ok=0; break; }; done
  systemctl start commroom-watchdog
  docker logs commroom 2>&1 | grep -iE 'traceback|exception' | head
  exit \$ok"
```

A non-zero exit means `/healthz` never answered: roll back. The run flags are
the container's whole configuration; if you change them, change them here
and in Rollback. To see what is running now: `docker inspect commroom`
(`.HostConfig`, and `.Config.Env` names only).

## 6. nginx

Ships `deploy/nginx-commroom.live.conf`, prepared in step 1.3.

```bash
scp deploy/nginx-commroom.live.conf $HOST:/tmp/commroom.nginx.new
ssh $HOST "cmp -s $NGINX_SITE $NGINX_BACKUPS/commroom.$PRE || { echo changed since backup; exit 1; }
  cp /tmp/commroom.nginx.new $NGINX_SITE
  if nginx -t; then nginx -s reload; else cp $NGINX_BACKUPS/commroom.$PRE $NGINX_SITE; nginx -t; echo 'nginx -t failed, restored'; exit 1; fi"
```

Reload, never restart: the other sites on the host keep serving.

## 7. Check from outside

As an ordinary client, over HTTPS, without writing anything: `/healthz`; the
behaviour you changed (for limits: a burst of reads, a burst of POSTs to a
room that does not exist — nginx counts them, the app answers 404 and
stores nothing); the app and nginx error logs since the deploy.

## Rollback

`PRE` is the name logged below for the deploy you are undoing — set it by
hand, do not recompute it.

```bash
PRE=<from the log>
ssh $HOST "set -e
  systemctl stop commroom-watchdog
  docker stop -t 45 commroom
  docker rm commroom
  docker run -d --name commroom --restart unless-stopped --network commroom-net \
    -p 127.0.0.1:8000:8000 -v commroom_data:/app/data --env-file $APP_DIR/.env commroom:$PRE
  systemctl start commroom-watchdog
  cp $NGINX_BACKUPS/commroom.$PRE $NGINX_SITE && nginx -t && nginx -s reload"
```

Restore `*.bak-$PRE` into `$APP_DIR` as well, so the next build does not
bring the change back. The database is restored from `predeploy-$PRE.db`
only if a migration damaged it: anything written since the backup is lost.

## Log

| Date | Commit | What | `PRE` | DB backup |
|---|---|---|---|---|
| 12.11.2026 | 0fc8d6c | write limits count POSTs, Retry-After per path, inbox without expired rooms, long-poll, vc_ verify codes | `pre-longpoll-20261112` | `predeploy_20261112.db` |
| 12.11.2026 | c42fbda | inbox `cursor`/`since`, early-leaver metering (app/ only, nginx untouched) | `pre-cursor-20261112` | `predeploy-pre-cursor-20261112.db` |
