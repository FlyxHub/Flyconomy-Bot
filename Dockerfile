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

# No HEALTHCHECK: the bot exposes no endpoint to probe, and a check that only
# proves the container can start a second Python process would report healthy
# while the gateway connection was dead. The client exits when the connection is
# lost for good, so Compose's restart policy is the honest signal.

ENTRYPOINT ["python", "-m", "flyconomy"]
