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
# Admission control at the door (load test 05.10.2026): past --limit-concurrency
# open connections/tasks uvicorn answers 503 itself, before the request reaches
# the event loop's backlog of work — the app-level shedder (main.py,
# ROOMCOMM_MAX_INFLIGHT) only sees requests that already got that far, and at
# 80 req/s the queue built up in front of it. A short --backlog keeps the kernel
# from parking thousands of connections either. Both overridable from .env.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*' --limit-concurrency ${UVICORN_LIMIT_CONCURRENCY:-64} --backlog ${UVICORN_BACKLOG:-256}"]
