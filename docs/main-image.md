# The main DSH Web image

This repository's primary artifact is a container image that packages
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) Web
(`dsh web`) for Kubernetes and plain Docker. The [Dockerfile](../Dockerfile) at the
repository root builds it; every version pin it uses lives in
[versions.env](../versions.env).

The image is image-first: it is the product, and the examples under
[`examples/`](../examples/) are self-contained implementations that consume it.

## What the build does

The Dockerfile is a two-stage build:

1. **Build stage** (`node:24-trixie`) clones DSH at the pinned git ref, installs
   dependencies with pnpm, runs the production build, packages the `@deepseek-ai/dsh`
   CLI with `pnpm pack`, and installs its production dependencies with npm.
2. **Runtime stage** (`node:24-trixie-slim`) copies `/opt/dsh` to `/app`, adds the
   CLI tools DSH agents commonly use (`git`, `ripgrep`, `jq`, `yq`, `curl`, `wget`,
   `gh`, `tini`, `uv`), and starts `dsh web` through a small entrypoint.

## Build it

```bash
make image          # canonical: builds the DSH Web image, no push
```

`make image` reads [versions.env](../versions.env) and passes the pins as build args.
To reproduce the equivalent by hand:

```bash
docker build \
  --build-arg DSH_VERSION=dsh-v0.2.1-rc.5 \
  --build-arg RELEASE_REVISION= \
  --build-arg PNPM_VERSION=11.28.5 \
  --build-arg VCS_REF="$(git rev-parse --short HEAD)" \
  -t ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:dsh-v0.2.1-rc.5 .
```

### Build args

| Arg | Default / source | Purpose |
| --- | --- | --- |
| `DSH_REPO` | `https://github.com/deepseek-ai/deepseek-harness.git` | Upstream DSH repository to clone. |
| `DSH_VERSION` | `DSH_VERSION` in [versions.env](../versions.env) | Upstream DSH git tag/ref to package; recorded as `org.opencontainers.image.version`. |
| `RELEASE_REVISION` | `RELEASE_REVISION` in [versions.env](../versions.env) | Repository revision suffix (`rN`); the release identity is `DSH_VERSION-RELEASE_REVISION`, recorded in the `io.sredevopsorg.release.revision` label. See [versioning](versioning.md). |
| `PNPM_VERSION` | `PNPM_VERSION` in [versions.env](../versions.env) | pnpm version activated with corepack in both stages. |
| `VCS_REF` | `git rev-parse --short HEAD` (via `make image`) | Source revision recorded for traceability. |
| `NODE_BUILD_IMAGE` | `docker.io/node:24-trixie` | Build-stage base image. |
| `NODE_RUNTIME_IMAGE` | `docker.io/node:24-trixie-slim` | Runtime-stage base image. |

Never hard-code a version outside [versions.env](../versions.env):
`./scripts/check-versions.sh` (and `make check`) fails when a pin drifts.

## Runtime contract

### Entrypoint

The entrypoint is `tini -- /usr/local/bin/dsh-entrypoint`:

1. `tini` runs as PID 1 and reaps children/forwards signals.
2. The shell entrypoint applies defaults, derives a bind address when `DSH_HOST` is
   empty (the pod/container's first non-loopback IPv4), builds the
   `dsh web` command line, and `exec`s it so signals reach the Node process.
3. `localhost` and `127.0.0.1` are always added to DSH's trusted-host list; each
   `DSH_TRUSTED_HOSTS` entry is added as an extra `--trusted-host`.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DSH_PORT` | `3080` | Port DSH Web listens on inside the container. |
| `DSH_HOST` | auto-detected non-loopback IPv4 | Bind address. Leave empty in Kubernetes so the pod IP is used automatically. |
| `DSH_PUBLIC_URL` | `http://localhost:${DSH_PORT}` | Public base URL used to build links and the launch-token URL printed in the logs. Set it when the container is published on a different host port or behind a proxy. |
| `DSH_TRUSTED_HOSTS` | empty | Extra `Host`/`Origin` allow-list entries, comma- or space-separated. `localhost`/`127.0.0.1` are always trusted. Required when a proxy forwards the browser's `Host`. |
| `DEEPSEEK_API_KEY` | empty | Provider credential read by DSH. Inject it with `-e`/`envFrom`; never bake it into the image. Only model calls fail when it is missing. |
| `DSH_HOME` | `/home/node/.dsh` | DSH state directory (conversations, sessions, config, credentials). Mount a volume here to persist it. |

The image also sets `NODE_ENV=production`, `HOME=/home/node`, and a `PATH` that
includes `/app/node_modules/.bin`.

### Provider credentials

DSH resolves provider API keys from the environment when no stored credential
exists. The packaged provider catalog maps each provider to its standard
environment name. Commonly used names:

`DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `NVIDIA_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `XAI_API_KEY`,
`MISTRAL_API_KEY`, `TOGETHER_API_KEY`, `CEREBRAS_API_KEY`,
`AI_GATEWAY_API_KEY`, `HF_TOKEN`.

Use `GEMINI_API_KEY` for Google Gemini (not `GOOGLE_API_KEY`), `HF_TOKEN` for
Hugging Face, and `AI_GATEWAY_API_KEY` for the Vercel AI Gateway. Inject keys
with `-e`/`--env-file` in Docker or a Secret + `envFrom` in Kubernetes; never
bake them into the image.

A key saved through DSH's credential store (`$DSH_HOME/.credentials.yaml`) wins
over the environment. The
[agents-as-a-service example](../examples/dsh-agents-as-a-service/docs/optional-hardening.md)
documents the optional `dsh-provider-keys-extra` Secret and the pod-refresh
procedure.

### Ports, volumes and signals

| Item | Value | Notes |
| --- | --- | --- |
| Exposed port | `3080/tcp` | `EXPOSE 3080`; one port serves the UI, assets, API, SSE and WebSocket traffic. |
| State volume | `/home/node/.dsh` | Declared with `VOLUME`; use a named volume (Docker) or PVC (Kubernetes). |
| Working directory | `/workspace` | The agent workspace; mount a volume here if agents should keep files. |
| Stop signal | `SIGTERM` | `tini` forwards it to `dsh web`; allow a graceful shutdown period. |
| User | `node`, UID/GID `1000` | The process is non-root. A PVC mounted at `DSH_HOME` must be writable by UID 1000. |

### Health probes

`/` is **not** a health endpoint: DSH returns `401` until a browser completes the
launch-token handshake. Probe `/favicon.svg` instead — it is unauthenticated and
served by the web layer, so it proves the HTTP server is up without triggering auth.

```yaml
startupProbe:
  httpGet: {path: /favicon.svg, port: 3080}
  periodSeconds: 5
  failureThreshold: 24
readinessProbe:
  httpGet: {path: /favicon.svg, port: 3080}
  periodSeconds: 5
livenessProbe:
  httpGet: {path: /favicon.svg, port: 3080}
  periodSeconds: 15
```

The shipped templates use exactly this shape; see
[`examples/minimal-single-tenant/manifests/sandbox-template.yaml`](../examples/minimal-single-tenant/manifests/sandbox-template.yaml).
A `startupProbe` is important because plugin activation (or the first start of a
large sandbox) can exceed a default liveness budget.

### Launch token

DSH prints its one-time launch URL to the logs:

```
dsh web: http://<host>:${DSH_PORT}/?token=...
```

Open that URL once to obtain DSH's auth cookie. After that the UI works at the bare
URL; without the cookie `/` answers `401` by design. The
[agents-as-a-service gateway](../examples/dsh-agents-as-a-service/gateway/) performs
this handshake server-side so browsers never see the token.

## Published image names and tags

Registry layout: the main image is the repository itself, and every example
component is a nested package below it:

```
ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox                        # main DSH Web image
ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox/dsh-agents-as-a-service/gateway   # example component
```

The main image is published multi-arch (`linux/amd64`, `linux/arm64`) to
GitHub Container Registry:

| Tag | Meaning |
| --- | --- |
| `dsh-v0.2.1-rc.5` | Current release identity. **Immutable**: once published it never moves. |
| `latest` | Most recent release; points at the same digest as the current identity. |
| `main` | Development channel rebuilt from every push to `main`; not a release, not for production. |

Tags published before the release-identity scheme remain published but are not
updated; new deployments use the current identity.

Release identities, channels and rollback rules are defined in
[versioning](versioning.md). Tags are immutable, so production deployments should
pin a digest:

```bash
docker buildx imagetools inspect ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:dsh-v0.2.1-rc.5
# ... pin the digest in your deployment manifests:
#   image: ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox@sha256:<digest>
```

## Verifying an image

```bash
docker inspect ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:dsh-v0.2.1-rc.5 \
  --format '{{json .Config.Labels}}'
# org.opencontainers.image.title / .version / .source
```

Release builds also publish SBOM and provenance attestations from the release
workflow (see [SECURITY.md](../SECURITY.md) and [gitflow](gitflow.md)).
