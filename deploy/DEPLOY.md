# Deploying roomcomm

How a change gets from this repository to the running service: one Docker
container behind nginx on a single host. No CI deploys anything; a person (or
an agent the owner asked to deploy in that session — see AGENTS.md) runs the
steps below by hand.

Nothing here is secret, and nothing here is specific to one host: the
host's address, the ssh alias and the paths live in `deploy/host.env`, which
`.gitignore` keeps local (`*.env`). Copy `deploy/host.env.example` and fill it
in. The application's own secrets (`ROOMCOMM_ADMIN_TOKEN`, LLM keys, the
Telegram token and so on) stay in the `.env` next to the build context on the
host and are never copied off it.

```bash
set -a; . deploy/host.env; set +a   # HOST, APP_DIR, NGINX_SITE, NGINX_BACKUPS
T=$(date +%Y%m%d); REV=$(git rev-parse --short HEAD); TAG=<what>-$REV
```

## What runs where

| Piece | Where | Notes |
|---|---|---|
| Build context | `$APP_DIR` on the host: `app/`, `Dockerfile`, `requirements.txt`, `.env` | not a git checkout; files are copied in |
| Image | `commroom:latest`, plus a tag per deploy | old tags are the rollback |
| Container | `commroom`, network `commroom-net`, `127.0.0.1:8000` | started with `--env-file $APP_DIR/.env` |
| Data | Docker volume `commroom_data` → `/app/data` | SQLite `commroom.db`, files, skills, arbiter key; owned by 1000:1000. **Never recreate the volume** |
| Redis | container `commroom-redis` on `commroom-net` | rate-limit and counter state shared by workers |
| nginx | `$NGINX_SITE` (a regular file in `sites-enabled`) | shares the host with other sites |

The build context, `/etc/nginx` and the container need root on the host; the
unprivileged account can run `docker` but cannot write either.

## 1. Before touching the host

1. The gates are clean on the commit you deploy (`pytest -q`, `ruff check .`,
   `mypy app`; AGENTS.md). Deploy a commit, not a working tree.
2. **The host may be newer than git.** Hand-made fixes have landed there
   before. Compare what runs with the commit your change started from, and
   look at every difference before overwriting it:

   ```bash
   git archive <base> app Dockerfile requirements.txt | tar -x -C /tmp/base
   ssh $HOST "cd $APP_DIR && find app Dockerfile requirements.txt -type f ! -path '*__pycache__*' -exec sha256sum {} +"
   ```

   Compare contents with carriage returns stripped (`tr -d '\r'`): files
   copied from a Windows checkout carry CRLF, and a hash difference alone
   means nothing.
3. **nginx: the host's file is the base, not `deploy/nginx-commroom.live.conf`.**
   The live copy in the repo lags whatever was added on the host for other
   sites (12.11.2026: a denylist include, a proxy for another service, a
   webhook route). Fetch `$NGINX_SITE`, apply your change to it, and check
   that `diff` against the fetched file shows only your lines. Then save the
   result as the new `deploy/nginx-commroom.live.conf` and run
   `pytest tests/test_deploy_conf.py`, which also checks it against the
   published config.
4. A renamed `limit_req_zone` must not be referenced anywhere else:
   `grep -rn '<old zone name>' /etc/nginx/`. Zones are renamed rather than
   re-keyed because nginx refuses, on reload, a zone whose key changed.

## 2. Back up

```bash
ssh $HOST "set -e
  cp -a $APP_DIR/app $APP_DIR/app.bak-pre-<what>-$T
  cp -a $APP_DIR/Dockerfile $APP_DIR/Dockerfile.bak-pre-<what>-$T
  cp -a $APP_DIR/requirements.txt $APP_DIR/requirements.txt.bak-pre-<what>-$T
  docker tag commroom:latest commroom:pre-<what>-$T
  mkdir -p $NGINX_BACKUPS && cp -a $NGINX_SITE $NGINX_BACKUPS/commroom.pre-<what>-$T"
```

Keep nginx backups **outside** `sites-enabled`: everything in it is loaded.

Database, through SQLite's backup API (a plain `cp` of a live WAL database
can copy a torn state), then an integrity check of the copy:

```bash
ssh $HOST "docker exec commroom python -c \"import sqlite3; s=sqlite3.connect('/app/data/commroom.db'); d=sqlite3.connect('/app/data/predeploy_$T.db'); s.backup(d); d.close()\"
           docker exec commroom python -c \"import sqlite3; print(sqlite3.connect('file:/app/data/predeploy_$T.db?mode=ro', uri=True).execute('pragma integrity_check').fetchone()[0])\""
```

It must print `ok`. The copy is about the size of the database; check
`df -h /` first.

## 3. Ship and build

```bash
git archive --format=tar -o release.tar HEAD app Dockerfile requirements.txt
scp release.tar $HOST:/tmp/commroom-release-$REV.tar
ssh $HOST "tar -xf /tmp/commroom-release-$REV.tar -C $APP_DIR --no-same-owner &&
           cd $APP_DIR && docker build -q -t commroom:latest -t commroom:$TAG ."
```

`tar` overwrites and adds; a file deleted in git stays on the host until you
remove it there.

## 4. Smoke-test the image before swapping

A throwaway container on a free port, with a scratch data directory owned by
the app user (without it the app cannot create `/app/data/skills` and exits):

```bash
ssh $HOST "P=18765; ss -ltn | grep -q :\$P && echo busy && exit 1
  docker run -d --name commroom-smoke --tmpfs /app/data:uid=1000,gid=1000 -p 127.0.0.1:\$P:8000 commroom:$TAG
  sleep 5; curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:\$P/healthz
  docker logs commroom-smoke 2>&1 | tail -20; docker rm -f commroom-smoke"
```

Check that the port is free first: on 12.11.2026 another service on the host
answered on the first port tried, and its 404s looked like the smoke test's.

## 5. Swap the container

A few seconds of downtime. `-t` gives held long-polls (up to 30 s) time to
answer; gunicorn's own `--graceful-timeout` in the Dockerfile matches.

```bash
ssh $HOST "set -e
  docker stop -t 35 commroom && docker rm commroom
  docker run -d --name commroom --restart unless-stopped --network commroom-net \
    -p 127.0.0.1:8000:8000 -v commroom_data:/app/data --env-file $APP_DIR/.env commroom:latest
  for i in \$(seq 1 40); do sleep 1; curl -fsS http://127.0.0.1:8000/healthz && break; done
  docker logs commroom 2>&1 | grep -iE 'traceback|exception' | head"
```

The run flags are the container's whole configuration; if you change them,
change them here too. To see what is running now:
`docker inspect commroom` (`.HostConfig`, `.Config.Env` names only).

## 6. nginx

```bash
scp nginx-commroom.new $HOST:/tmp/commroom.nginx.new
ssh $HOST "cmp -s $NGINX_SITE $NGINX_BACKUPS/commroom.pre-<what>-$T || { echo changed since backup; exit 1; }
  cp /tmp/commroom.nginx.new $NGINX_SITE
  if nginx -t; then nginx -s reload; else cp $NGINX_BACKUPS/commroom.pre-<what>-$T $NGINX_SITE; nginx -t; fi"
```

Reload, never restart: the other sites on the host keep serving.

## 7. Check from outside

As an ordinary client, over HTTPS, without writing anything: `/healthz`; the
behaviour you changed (for limits: a burst of reads, a burst of POSTs to a
room that does not exist — nginx counts them, the app answers 404 and
stores nothing); the app and nginx error logs since the deploy.

## Rollback

```bash
ssh $HOST "set -e
  docker stop -t 35 commroom && docker rm commroom
  docker run -d --name commroom --restart unless-stopped --network commroom-net \
    -p 127.0.0.1:8000:8000 -v commroom_data:/app/data --env-file $APP_DIR/.env commroom:pre-<what>-$T
  cp $NGINX_BACKUPS/commroom.pre-<what>-$T $NGINX_SITE && nginx -t && nginx -s reload"
```

Restore `app.bak-pre-…` into `$APP_DIR` as well, so the next build does not
bring the change back. The database is restored from `predeploy_$T.db` only
if a migration damaged it: anything written since the backup is lost.

## Log

| Date | Commit | What | Backups |
|---|---|---|---|
| 12.11.2026 | 0fc8d6c | write limits count POSTs, Retry-After per path, inbox without expired rooms, long-poll, vc_ verify codes | tag `pre-longpoll-20261112`, `predeploy_20261112.db` |
