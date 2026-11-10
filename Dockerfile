# syntax=docker/dockerfile:1.7
#
# Personal Finance Analyzer as a container image for a NAS or any Docker host (linux/amd64 and
# linux/arm64). See docs/DOCKER.md for how to run it; the short version is docker-compose.yml.
#
#   docker build -t personal-finance-analyzer .
#   docker buildx build --platform linux/amd64,linux/arm64 -t personal-finance-analyzer .
#
# Three stages, so the final image holds no Node, no uv, no compilers and no package manager cache:
#   web      builds the React frontend (once, on the build machine's own architecture: the output
#            is plain JS/CSS and does not depend on the target CPU)
#   builder  installs the app and its locked, non-dev dependencies into a virtualenv with uv, with
#            the built frontend bundled into the package
#   final    python:slim plus that virtualenv, an entrypoint and a healthcheck
#
# Base images are pinned by digest (tag@sha256) for reproducible builds; Dependabot
# (.github/dependabot.yml) proposes updates.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.19@sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6


# --- web: the frontend bundle -----------------------------------------------------------------
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS web
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/ ./
# vite.config.ts writes to ../src/personal_finance_analyzer/web, i.e. /build/src/... here. The same
# `vite build` the wheel's hatch_build.py hook runs; type checking is CI's job, not the image's.
RUN npx vite build \
 && test -f /build/src/personal_finance_analyzer/web/index.html


# --- builder: the app and its locked runtime dependencies --------------------------------------
FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/pfa \
    PFA_SKIP_FRONTEND_BUILD=1
WORKDIR /src
# Dependencies first, from the lockfile alone, so a source-only change reuses this layer.
COPY pyproject.toml uv.lock README.md hatch_build.py ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
# Then the project itself, as a regular (non-editable) wheel install with the frontend inside it
# (PFA_SKIP_FRONTEND_BUILD makes hatch_build.py use the bundle copied in below instead of
# looking for npm; `artifacts` in pyproject.toml force-includes it into the wheel).
COPY src ./src
COPY --from=web /build/src/personal_finance_analyzer/web ./src/personal_finance_analyzer/web
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
 && test -f /opt/pfa/lib/python3.12/site-packages/personal_finance_analyzer/web/index.html \
 && /opt/pfa/bin/pfa --help >/dev/null


# --- final -------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS final

ARG SOURCE_URL=https://github.com/haoxinm/personal-finance-analyzer
LABEL org.opencontainers.image.title="Personal Finance Analyzer" \
      org.opencontainers.image.description="Local-first spending, income and tax, and net-worth analysis from your own statements and tax forms. Self-hosted; no telemetry." \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.url="${SOURCE_URL}" \
      org.opencontainers.image.documentation="${SOURCE_URL}/blob/main/docs/DOCKER.md" \
      org.opencontainers.image.licenses="MIT"

COPY --from=builder /opt/pfa /opt/pfa
COPY docker/entrypoint.sh /usr/local/bin/pfa-entrypoint
COPY docker/pfa-wrapper.sh /usr/local/bin/pfa
COPY docker/healthcheck.py /usr/local/bin/pfa-healthcheck
RUN chmod 0755 /usr/local/bin/pfa-entrypoint /usr/local/bin/pfa /usr/local/bin/pfa-healthcheck \
 && mkdir -p /pfa \
 && chmod 0755 /pfa

# PFA_HOME is the data directory: finance.db, config.toml, logs, statement PDFs. It sits one level
# below the /pfa mount on purpose: restoring a backup stages the new data next to the old and swaps
# the two directories, which needs a writable parent on the same filesystem and cannot be done to
# a mount point itself. Mount one host directory at /pfa (see docker-compose.yml).
#
# PFA_HOST/PFA_SERVER__PORT: listen on every interface of the container (docker publishes the
# port); because that is beyond loopback, the app refuses to start without PFA_PASSWORD or
# PFA_PASSWORD_FILE. PFA_CONTAINER makes Settings and `pfa` say "use a Docker secret" instead of
# talking about an OS keychain. The `fail` keyring backend stops the keyring library from probing
# for a D-Bus Secret Service that a container does not have.
ENV PATH="/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/pfa/bin" \
    PFA_HOME=/pfa/data \
    PFA_HOST=0.0.0.0 \
    PFA_SERVER__PORT=8756 \
    PFA_CONTAINER=1 \
    PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring \
    HOME=/tmp \
    PUID=1000 \
    PGID=1000 \
    UMASK=077 \
    TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# No VOLUME instruction on purpose: it would make Docker create an anonymous volume when none is
# mounted, which `docker volume prune` can delete. Bind-mount a host directory instead; the
# entrypoint warns loudly when nothing is mounted at /pfa.
EXPOSE 8756

HEALTHCHECK --interval=30s --timeout=6s --start-period=60s --retries=3 \
    CMD ["pfa-healthcheck"]

# Starts as root only to match PUID/PGID/UMASK to the bind mount and then drops to that user
# (setpriv); `docker run --user UID:GID` skips that step and runs as that user throughout.
ENTRYPOINT ["pfa-entrypoint"]
CMD ["serve"]
