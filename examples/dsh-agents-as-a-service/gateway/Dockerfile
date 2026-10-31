# syntax=docker/dockerfile:1.7
#
# Example gateway image: the tenant-facing proxy in front of DSH sandboxes.
# The base image is digest-pinned in versions.env (GATEWAY_PYTHON_IMAGE) and
# scripts/check-versions.sh verifies the ARG default below.
#
# Two stages on that base:
#   build    installs the fully hash-pinned runtime dependencies into /venv as
#            a dedicated non-root UID, so the runtime stage copies a venv that
#            contains no root-owned files.
#   runtime  copies /venv and the app, runs as UID 10000 with HOME=/tmp and no
#            writable location of its own: usable with readOnlyRootFilesystem.
ARG GATEWAY_PYTHON_IMAGE=docker.io/python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

# ---------------------------------------------------------------------------
# Build stage: hash-pinned virtualenv owned by the runtime identity.
# ---------------------------------------------------------------------------
FROM $GATEWAY_PYTHON_IMAGE AS build

ARG APP_UID=10000
ARG APP_GID=10000

# No .pyc writes and no user cache dirs: the runtime image stays read-only.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

RUN groupadd --gid "$APP_GID" app \
 && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home \
      --home-dir /tmp --shell /usr/sbin/nologin app \
 && python -m venv /venv \
 && chown -R "$APP_UID:$APP_GID" /venv

WORKDIR /build
COPY requirements.txt .

# Install as the unprivileged user so every file in /venv is owned by UID 10000.
# --require-hashes makes pip reject any requirement without a matching hash:
# the dependency set in requirements.txt is fully pinned and verified. Use the
# venv's own pip: the system pip would user-install into $HOME instead.
USER $APP_UID:$APP_GID
RUN /venv/bin/pip install --no-cache-dir --require-hashes -r requirements.txt

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM $GATEWAY_PYTHON_IMAGE AS runtime

ARG APP_UID=10000
ARG APP_GID=10000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PATH="/venv/bin:$PATH"

RUN groupadd --gid "$APP_GID" app \
 && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home \
      --home-dir /tmp --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies come from the build stage: same interpreter, no build tools left
# in the runtime image and nothing owned by root inside /venv.
COPY --from=build --chown=$APP_UID:$APP_GID /venv /venv
COPY --chown=$APP_UID:$APP_GID gateway.py .

EXPOSE 8080

# Run as a fixed non-root UID; HOME=/tmp keeps caches out of the image.
USER 10000

CMD ["uvicorn", "gateway:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
