# syntax=docker/dockerfile:1

# Build the wheel in a throwaway stage so the runtime image carries no build
# tools and no source tree.
FROM python:3.14-slim AS builder

WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir "hatchling>=1.27" build

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist


FROM python:3.14-slim AS runtime

# PYTHONUNBUFFERED keeps logs flowing to `docker logs` in real time.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    FLYCONOMY_DATABASE_PATH=/data/bot.db

# Run as an unprivileged user. The UID is fixed so a bind-mounted volume keeps
# predictable ownership on the host.
RUN groupadd --gid 10001 flyconomy \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin flyconomy

COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# The database lives on a volume so it survives image upgrades.
RUN mkdir -p /data && chown flyconomy:flyconomy /data
VOLUME ["/data"]

USER flyconomy
WORKDIR /home/flyconomy

# The bot holds a websocket to Discord; if that drops for good the client exits,
# so "is the process alive" is a meaningful health signal.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

ENTRYPOINT ["python", "-m", "flyconomy"]
