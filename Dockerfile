FROM python:3.11-slim
WORKDIR /app
# Непривилегированный пользователь для рантайма (см. USER ниже).
# Данные в volume commroom_data (/app/data) на сервере chown 1000:1000 — иначе SQLite/arbiter.key read-only.
RUN useradd -u 1000 -m appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
# Scheduled jobs run inside the container (docker exec … python -m scripts.x).
# The anchor publisher needs the same DB and arbiter key the app uses, so it
# ships with the image rather than living on the host.
COPY scripts ./scripts
RUN mkdir -p /app/data
EXPOSE 8000
VOLUME ["/app/data"]
USER appuser
# Several worker processes (one Python process uses one core), each a uvicorn
# event loop: app/workers.py carries the uvicorn settings, incl. the per-worker
# admission limit (--limit-concurrency; load test 05.10.2026). gunicorn's
# --timeout kills and respawns a worker whose event loop stops answering its
# heartbeat for that long — a hung worker heals in seconds, before the host
# watchdog would restart the whole container. State shared across workers
# (rate limits, locks) lives in Redis: REDIS_URL, see app/shared.py.
# WEB_CONCURRENCY / GUNICORN_BACKLOG / UVICORN_LIMIT_CONCURRENCY via .env.
CMD ["sh", "-c", "exec gunicorn app.main:app --worker-class app.workers.RoomcommWorker --workers ${WEB_CONCURRENCY:-2} --bind 0.0.0.0:8000 --backlog ${GUNICORN_BACKLOG:-256} --timeout 30 --graceful-timeout 20 --keep-alive 5 --access-logfile - --error-logfile -"]
