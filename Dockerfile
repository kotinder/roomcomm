FROM python:3.11-slim
WORKDIR /app
# Run as an unprivileged user (see USER below).
# The data volume mounted at /app/data must be owned by 1000:1000, or SQLite and arbiter.key open read-only.
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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
