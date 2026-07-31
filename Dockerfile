FROM python:3.11-slim
WORKDIR /app
# Непривилегированный пользователь для рантайма (см. USER ниже).
# Данные в volume commroom_data (/app/data) на сервере chown 1000:1000 — иначе SQLite/arbiter.key read-only.
RUN useradd -u 1000 -m appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
RUN mkdir -p /app/data
EXPOSE 8000
VOLUME ["/app/data"]
USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
