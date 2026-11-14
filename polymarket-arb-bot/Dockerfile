FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MALLOC_ARENA_MAX=2 \
    POLYARB_IN_CONTAINER=1 \
    POLYARB_DATA_DIR=/data \
    POLYARB_MODE=paper \
    PORT=8080

RUN groupadd --gid 10001 polyarb \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin polyarb

WORKDIR /app

COPY requirements.lock ./
RUN pip install --only-binary=:all: -r requirements.lock

COPY pyproject.toml README.md ./
COPY polyarb ./polyarb
COPY config ./config
COPY entrypoint.sh ./
RUN pip install --no-deps . \
    && pip check \
    && rm -rf build ./*.egg-info \
    && chmod 755 entrypoint.sh \
    && cd / && polyarb --help > /dev/null \
    && python -c "import polymarket, polyarb.app, polyarb.execution.live; from polyarb.web.server import STATIC_DIR; assert (STATIC_DIR / 'app.js').is_file()"

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=4)"]

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["polyarb", "serve"]
