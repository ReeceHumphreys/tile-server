# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS tools
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates libexpat1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY pipeline ./pipeline
COPY datasets ./datasets
RUN uv sync --locked --no-dev --no-editable
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/app/.venv/bin/tile-service"]
CMD ["--help"]

FROM caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f AS runtime
# Port 8080 needs no file capability; removing it permits cap_drop: ALL.
RUN setcap -r /usr/bin/caddy \
    && mkdir -p /srv && chown 10001:10001 /srv
COPY Caddyfile /etc/caddy/Caddyfile
ENV XDG_CONFIG_HOME=/tmp/config XDG_DATA_HOME=/tmp/data
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/release.json || exit 1
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
