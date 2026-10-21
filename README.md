Here is the complete updated Dockerfile incorporating the hoisted deploy fix and the dependency patch. The key changes versus the last version are in the deploy block: `--legacy` is dropped, `node-linker=hoisted` and `inject-workspace-packages` are enabled, and the missing workspace peer is added via the `workspace:*` protocol.

```dockerfile
# syntax=docker/dockerfile:1.7

ARG NODE_BUILD_IMAGE=docker.io/node:24-trixie
ARG NODE_RUNTIME_IMAGE=docker.io/node:trixie-slim

# ---------------------------------------------------------------------------
# Build stage
# ---------------------------------------------------------------------------
FROM ${NODE_BUILD_IMAGE} AS build

ARG DSH_REPO=https://github.com/deepseek-ai/deepseek-harness.git
ARG DSH_VERSION=dsh-v0.2.1-rc.2
ARG PNPM_VERSION=11.28.5

# CI=true skips the repository's lefthook postinstall in the container.
ENV CI=true \
    NODE_OPTIONS=--max-old-space-size=4096

RUN corepack enable \
 && corepack prepare "pnpm@${PNPM_VERSION}" --activate

WORKDIR /src

# Exact upstream tag, e.g. dsh-v0.2.1-rc.2.
RUN git clone --depth 1 --branch "${DSH_VERSION}" "${DSH_REPO}" .

RUN pnpm install --frozen-lockfile

# ── Workaround for upstream packaging defect ──────────────────────────────
# dsh-app-boot statically imports @deepseek-ai/cordis-plugin-group, which is
# only a peer/workspace dependency and is omitted by `pnpm deploy --prod`.
# Promote it to a real dependency of @deepseek-ai/dsh so it is always part of
# the deploy closure.
RUN pnpm --filter @deepseek-ai/dsh add @deepseek-ai/cordis-plugin-group@workspace:*
# ──────────────────────────────────────────────────────────────────────────

# Builds native addon, Host/Client bundles, and the Web frontend.
RUN pnpm run build

# ── Production deploy closure ────────────────────────────────────────────
# A hoisted (npm-style) node_modules is required: the default isolated pnpm
# layout drops peer dependencies such as @deepseek-ai/cosmokit from the
# deployed tree, causing ERR_MODULE_NOT_FOUND at runtime. `--legacy` is
# intentionally NOT used, so pnpm's modern deploy binds peers correctly.
RUN pnpm --config.node-linker=hoisted \
         --config.inject-workspace-packages=true \
         deploy --prod /opt/dsh \
 && test -f /opt/dsh/lib/bin.js \
 && test -d /opt/dsh/node_modules/@deepseek-ai/cosmokit \
 && test -d /opt/dsh/node_modules/@deepseek-ai/cordis-plugin-group \
 && node /opt/dsh/lib/bin.js web --help >/dev/null
# ──────────────────────────────────────────────────────────────────────────

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM ${NODE_RUNTIME_IMAGE} AS runtime

ARG DSH_VERSION

LABEL org.opencontainers.image.title="DeepSeek Harness Web" \
      org.opencontainers.image.version="${DSH_VERSION}" \
      org.opencontainers.image.source="https://github.com/deepseek-ai/deepseek-harness"

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
      tini \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -o node -g node -m 0755 /workspace "${DSH_HOME}"

COPY --from=build --chown=node:node /opt/dsh /app

COPY <<'EOF' /usr/local/bin/dsh-entrypoint
#!/bin/sh
set -eu

: "${DSH_PORT:=3080}"
: "${DSH_HOST:=}"
DSH_PUBLIC_URL="${DSH_PUBLIC_URL-http://localhost:${DSH_PORT}}"

# DSH rejects wildcard hosts. Pick the container/pod's first non-loopback IPv4.
if [ -z "$DSH_HOST" ]; then
  DSH_HOST="$(node -e 'const os=require("os"); const ifaces=Object.values(os.networkInterfaces()).flat(); const addr=ifaces.find(v=>v&&v.family==="IPv4"&&!v.internal); if(!addr){process.exit(1)} process.stdout.write(addr.address)')"
fi

if [ -n "$DSH_PUBLIC_URL" ]; then
  set -- --public-url "$DSH_PUBLIC_URL" "$@"
fi

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
```

### What changed and why

| Area | Before | Now | Reason |
| ------ | -------- | ----- | -------- |
| Deploy linker | default isolated `.pnpm` store | `--config.node-linker=hoisted` | Produces a flat npm-style tree so peer deps like `@deepseek-ai/cosmokit` are physically present. |
| Deploy flag | `--legacy` | removed | Legacy deployer does not reliably bind peer dependencies; the modern deployer does. |
| Workspace binding | implicit | `--config.inject-workspace-packages=true` | Guarantees workspace packages and their peers are bound into the closure deterministically. |
| Missing peer patch | `@deepseek-ai/cordis-plugin-group@^1.0.1` | `@deepseek-ai/cordis-plugin-group@workspace:*` | Correct protocol for a monorepo-internal package; no dependency on a published npm version. |
| Verification | only `bin.js` exists + `web --help` | also asserts `cosmokit` and `cordis-plugin-group` directories exist | Fails the build early if the closure is still incomplete, instead of failing at container start. |

### Build and run

```bash
docker build \
  --build-arg DSH_VERSION=dsh-v0.2.1-rc.2 \
  -t dsh-web:0.2.1-rc.2 .

docker run --rm -it \
  -p 3080:3080 \
  -v dsh-home:/home/node/.dsh \
  -e DEEPSEEK_API_KEY \
  dsh-web:0.2.1-rc.2
```

Then open the `dsh web:` URL printed in the logs (it carries a per-process access token).

### If the `pnpm add …@workspace:*` step ever fails

That would mean `@deepseek-ai/cordis-plugin-group` is not a workspace package in the chosen tag. In that case remove the two `pnpm --filter … add` lines; the hoisted linker alone is usually sufficient to include peer dependencies. You can confirm by checking that the `test -d /opt/dsh/node_modules/@deepseek-ai/cordis-plugin-group` assertion still passes.
