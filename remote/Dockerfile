# Voxarium Studio Remote — the shared server local Studios synchronise through (dev spec §16).
#
#   docker compose up -d --build
#   docker compose exec remote voxarium-studio-remote user add alice
#
# The image holds this package and nothing of the Studio itself: the two share a wire
# protocol, not code.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VOXARIUM_REMOTE_DATA=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY voxarium_studio_remote ./voxarium_studio_remote
RUN pip install .

# Everything the server keeps is one SQLite file under /data — mount a volume there.
RUN useradd --system --uid 10001 --home-dir /data --no-create-home studio \
    && mkdir /data && chown studio:studio /data
USER studio
VOLUME /data

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"

CMD ["voxarium-studio-remote", "serve", "--host", "0.0.0.0", "--port", "8787"]
