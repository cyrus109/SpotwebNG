# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

# Runtime deps only; the app is pure Python.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SPOTWEBNEXT_DATABASE_URL=sqlite+aiosqlite:////data/spotwebng.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source last: a code change re-uses the cached dependency layer above.
COPY app ./app
COPY scripts ./scripts

# Spot covers, cached NZBs and the SQLite file live outside the image so they
# survive `docker compose pull` — mount a volume at /data.
VOLUME ["/data"]
ENV SPOTWEBNG_DATA=/data

# Drop privileges: nothing here needs root.
RUN useradd --create-home --uid 10001 spotweb \
 && mkdir -p /data && chown -R spotweb:spotweb /app /data
USER spotweb

EXPOSE 7979
# A big database on a slow disk can need minutes at start-up (schema upgrade, recovering a large
# write-ahead log after a hard shutdown), so give it a long grace period before judging health.
HEALTHCHECK --interval=30s --timeout=10s --start-period=600s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7979/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7979"]
