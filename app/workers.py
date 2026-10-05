"""gunicorn worker class for the container (Dockerfile CMD).

uvicorn-worker reads uvicorn settings from CONFIG_KWARGS. Here they carry what
the plain `uvicorn` command line used to: proxy headers from nginx and the
per-process admission limit (--limit-concurrency; load test 05.10.2026). Note
uvicorn counts open connections, so a burst past the limit gets 503 at once.
The total across workers must stay under nginx's limit_conn commroom_conn (128).
"""
import os

from uvicorn_worker import UvicornWorker


class RoomcommWorker(UvicornWorker):
    CONFIG_KWARGS = {
        **UvicornWorker.CONFIG_KWARGS,  # loop/http "auto" (uvloop, httptools)
        "proxy_headers": True,
        "forwarded_allow_ips": "*",
        "limit_concurrency": int(os.environ.get("UVICORN_LIMIT_CONCURRENCY", "48")),
    }
