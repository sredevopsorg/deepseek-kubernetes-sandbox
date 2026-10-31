# syntax=docker/dockerfile:1.7
#
# The primary artifact of this repository: DeepSeek Harness Web (dsh web),
# packaged as a slim, non-root, multi-arch runtime image.
#
# Build args (all pinned in versions.env, verified by scripts/check-versions.sh):
#   DSH_REPO              upstream git remote to package
#   DSH_VERSION           upstream git ref to build (tag or branch)
#   DSH_COMMIT            full commit SHA DSH_VERSION must resolve to
#   RELEASE_REVISION      optional repository revision label (r2, r3, ...)
#   PNPM_VERSION          pnpm used to build DSH
#   VCS_REF               this repository's commit, recorded in OCI labels
#   NODE_BUILD_IMAGE      node image used to build DSH (pinned by digest)
#   NODE_RUNTIME_IMAGE    node image used at runtime (pinned by digest)
ARG NODE_BUILD_IMAGE=docker.io/node:24-trixie@sha256:8c761eca2bf7eec965f946cf47b8090143cb41f45f2f8699af0cc0298d2d2d19
ARG NODE_RUNTIME_IMAGE=docker.io/node:24-trixie-slim@sha256:54f84b4cf8432b21aaffd5d3ce8d41e12cea0d10d33c09d2f3c9879d49d6b446

# ---------------------------------------------------------------------------
# Build stage: clone DSH at the pinned ref, build, and package the CLI.
# ---------------------------------------------------------------------------
FROM $NODE_BUILD_IMAGE AS build

ARG DSH_REPO=https://github.com/deepseek-ai/deepseek-harness.git
ARG DSH_VERSION=dsh-v0.2.1-rc.5
ARG DSH_COMMIT=b830ec950faba0a46dda564be3e2d854afed94f1
ARG PNPM_VERSION=11.28.5

ENV CI=true \
    NODE_OPTIONS=--max-old-space-size=4096

RUN corepack enable \
 && corepack prepare "pnpm@$PNPM_VERSION" --activate

WORKDIR /src

# Clone the pinned ref and fail closed unless it resolves to DSH_COMMIT. A moved
# tag would otherwise silently change what the image packages.
RUN git clone --depth 1 --branch "$DSH_VERSION" "$DSH_REPO" . \
 && actual="$(git -C /src rev-parse HEAD)" \
 && if [ "$actual" != "$DSH_COMMIT" ]; then \
      echo "error: $DSH_REPO $DSH_VERSION resolved to $actual, expected DSH_COMMIT $DSH_COMMIT" >&2; \
      echo "error: update DSH_VERSION/DSH_COMMIT in versions.env with scripts/set-version.sh" >&2; \
      exit 1; \
    fi

RUN pnpm install --frozen-lockfile

RUN pnpm run build

# ── Package the CLI with pnpm pack ───────────────────────────────────────
RUN mkdir -p /opt/dsh \
 && pnpm --filter @deepseek-ai/dsh pack \
        --pack-destination /opt \
        --config.ignore-scripts=true \
 && mv /opt/deepseek-ai-dsh-*.tgz /opt/dsh.tgz \
 && tar -xzf /opt/dsh.tgz -C /opt/dsh --strip-components=1 \
 && rm /opt/dsh.tgz

# ── Production install with npm ──────────────────────────────────────────
RUN cd /opt/dsh \
 && npm install --omit=dev --no-audit --no-fund \
 && test -f lib/bin.js \
 && node lib/bin.js web --help >/dev/null

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM $NODE_RUNTIME_IMAGE AS runtime

ARG DSH_VERSION=dsh-v0.2.1-rc.5
ARG RELEASE_REVISION=
ARG PNPM_VERSION=11.28.5
ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="DeepSeek Harness Web packaged by SREDevOps.org" \
      org.opencontainers.image.description="DeepSeek Harness Web (dsh web) as a slim, non-root, Kubernetes-ready image" \
      org.opencontainers.image.source="https://github.com/sredevopsorg/deepseek-kubernetes-sandbox" \
      org.opencontainers.image.version="$DSH_VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.licenses="MIT" \
      io.sredevopsorg.dsh.version="$DSH_VERSION" \
      io.sredevopsorg.release.revision="$RELEASE_REVISION"

ENV NODE_ENV=production \
    HOME=/home/node \
    DSH_HOME=/home/node/.dsh \
    DSH_PORT=3080 \
    PATH="/app/node_modules/.bin:$PATH"

# git/ripgrep/jq/yq/curl/gh are deliberate agent tooling parity: a sandboxed DSH
# instance expects the usual developer CLI. tini reaps signals for a clean stop.
# bubblewrap is DSH's first-choice command-sandbox runner on Linux. It ships
# unsetuid, so it needs unprivileged user namespaces plus the namespace/mount
# syscalls; a hardened pod (all capabilities dropped, RuntimeDefault seccomp)
# usually cannot create one, which is why DSH then falls back to Landlock and,
# when that is unavailable too, to DSH_PERMISSION_MODE=danger-full-access.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      git \
      ripgrep \
      jq \
      yq \
      curl \
      wget \
      tini \
      gh \
      bubblewrap \
      python3 \
      python3-pip \
      python3-venv \ 
 && rm -rf /var/lib/apt/lists/* \
 && apt-get autoclean -y \
     && install -d -o node -g node -m 0755 /workspace "$DSH_HOME" \
     && corepack enable \
     && corepack prepare "pnpm@$PNPM_VERSION" --activate \
     && pnpm --version

COPY --from=build --chown=node:node /opt/dsh /app

COPY <<'EOF' /usr/local/bin/dsh-entrypoint
#!/bin/sh
set -eu

: "${DSH_PORT:=3080}"
: "${DSH_HOST:=}"
: "${DSH_TRUSTED_HOSTS:=}"
DSH_PUBLIC_URL="${DSH_PUBLIC_URL-http://localhost:${DSH_PORT}}"

# DSH rejects wildcard hosts. Pick the container/pod's first non-loopback IPv4.
if [ -z "$DSH_HOST" ]; then
  DSH_HOST="$(node -e 'const os=require("os"); const ifaces=Object.values(os.networkInterfaces()).flat(); const addr=ifaces.find(v=>v&&v.family==="IPv4"&&!v.internal); if(!addr){process.exit(1)} process.stdout.write(addr.address)')"
fi

if [ -n "$DSH_PUBLIC_URL" ]; then
  set -- --public-url "$DSH_PUBLIC_URL" "$@"
fi

# Additional trusted hosts (e.g. the public gateway host behind a proxy).
# DSH Host/Origin-fences /api: without this, requests forwarded with the
# browser's Host header are rejected. Accepts comma- or space-separated names;
# localhost/127.0.0.1 are always trusted below.
old_ifs="$IFS"
IFS=', '
for trusted_host in $DSH_TRUSTED_HOSTS; do
  [ -n "$trusted_host" ] || continue
  set -- "$@" --trusted-host "$trusted_host"
done
IFS="$old_ifs"

exec node /app/lib/bin.js web \
  --no-open \
  --host "$DSH_HOST" \
  --port "$DSH_PORT" \
  --trusted-host localhost \
  --trusted-host 127.0.0.1 \
  "$@"
EOF

RUN chmod 0755 /usr/local/bin/dsh-entrypoint

WORKDIR /workspace
USER node

EXPOSE 3080
VOLUME ["/home/node/.dsh"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/dsh-entrypoint"]
