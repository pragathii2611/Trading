FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata \
    DB_PATH=/data/trading.db \
    PORT=8000

WORKDIR /srv
COPY requirements.txt .
RUN grep -vE '^(pytest|httpx)' requirements.txt > req.txt && pip install --no-cache-dir -r req.txt && rm req.txt
COPY app ./app
RUN mkdir -p /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health')"
# One worker only: the trading engine runs inside the web process.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
