# syntax=docker/dockerfile:1.7

ARG NODE_BUILD_IMAGE=docker.io/node:24-trixie
ARG NODE_RUNTIME_IMAGE=docker.io/node:24-trixie-slim

# ---------------------------------------------------------------------------
# Build stage
# ---------------------------------------------------------------------------
FROM ${NODE_BUILD_IMAGE} AS build

ARG DSH_REPO=https://github.com/deepseek-ai/deepseek-harness.git
ARG DSH_VERSION=dsh-v0.2.1-rc.2
ARG PNPM_VERSION=11.28.5

ENV CI=true \
    NODE_OPTIONS=--max-old-space-size=4096

RUN corepack enable \
 && corepack prepare "pnpm@${PNPM_VERSION}" --activate

WORKDIR /src

RUN git clone --depth 1 --branch "${DSH_VERSION}" "${DSH_REPO}" .

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
FROM ${NODE_RUNTIME_IMAGE} AS runtime

ARG DSH_VERSION
ARG PNPM_VERSION=11.28.5

LABEL org.opencontainers.image.title="DeepSeek Harness Web packaged by SREDevOps.org" \
      org.opencontainers.image.version="${DSH_VERSION}" \
      org.opencontainers.image.source="https://github.com/sredevopsorg/deepseek-kubernetes-sandbox"

ENV NODE_ENV=production \
    HOME=/home/node \
    DSH_HOME=/home/node/.dsh \
    DSH_PORT=3080 \
    PATH="/app/node_modules/.bin:${PATH}"

# git/ripgrep are useful for DSH agent tools; tini handles signals.
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
 && rm -rf /var/lib/apt/lists/* \
     && install -d -o node -g node -m 0755 /workspace "${DSH_HOME}" \
     && corepack enable \
     && corepack prepare "pnpm@${PNPM_VERSION}" --activate \
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

# Additional trusted hosts (e.g. the public gateway host behind an Ingress).
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