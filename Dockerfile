# syntax=docker/dockerfile:1
# Hermes Artifacts service. Base images pinned by digest; Python deps from uv.lock.
ARG PYTHON_IMAGE=python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/srv/.venv
WORKDIR /srv
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

FROM ${PYTHON_IMAGE}
ARG REVISION=unknown
LABEL org.opencontainers.image.title="hermes-artifacts" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="MIT"
RUN useradd --system --uid 10001 --home-dir /srv --shell /usr/sbin/nologin artifacts \
    && mkdir -p /data && chown artifacts:artifacts /data
WORKDIR /srv
COPY --from=build /srv/.venv /srv/.venv
COPY app ./app
ENV PATH=/srv/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ARTIFACTS_DB=/data/artifacts.db ARTIFACTS_HOST=0.0.0.0 ARTIFACTS_PORT=8080
USER artifacts
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('ARTIFACTS_PORT', '8080'), timeout=3)"]
CMD ["python", "-m", "app"]
