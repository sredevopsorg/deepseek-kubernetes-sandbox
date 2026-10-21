# deepseek-kubernetes-sandbox

Packages [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) Web (`dsh web`) into
a multi-arch, Kubernetes-ready container image, and ships the
[agents-as-a-service](examples/dsh-agents-as-a-service/) platform pattern — running a personal
DSH Web instance per user on [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
behind a small browser-usable gateway.

## What's in this repo

| Path | Purpose |
| --- | --- |
| [`Dockerfile`](Dockerfile) | Multi-stage build that clones DSH, `pnpm pack`s the CLI, and produces a slim runtime image (`:3080`) |
| [`build.sh`](build.sh), [`Makefile`](Makefile) | Local image builds: DSH Web and gateway, plus test/lint targets |
| [`examples/dsh-agents-as-a-service/`](examples/dsh-agents-as-a-service/) | The full multi-tenant platform pattern (warm pool, per-user claims, gateway, smoke test) |
| [`examples/dsh-agents-as-a-service/gateway/`](examples/dsh-agents-as-a-service/gateway/) | Starlette ASGI gateway: signup, cookie sessions, `/u/{user}/**` HTTP + WebSocket proxy, idle sweeper |
| [`deploy/k8s/sandbox-template.yaml`](deploy/k8s/sandbox-template.yaml) | Minimal single-tenant `SandboxTemplate` |
| [`docs/architecture.md`](docs/architecture.md) | Container and sequence diagrams, design decisions, security notes |
| [`.github/workflows/`](.github/workflows/) | CI: manifests dry-run, gateway tests/lint, multi-arch image builds |

## Published images

Everything is published to GitHub Container Registry as
`ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox`:

| Tag | Contents | Port |
| --- | --- | --- |
| `dsh-v0.2.1-rc.2` (also `main`, `latest`) | DSH Web image | `3080` |
| `gateway` | Starlette gateway image | `8080` |

The DSH Web tag matches the upstream DSH git tag it was built from.

## Quick start (DSH Web image)

```bash
docker run -d --name dsh-web \
  -p 3080:3080 \
  -v dsh-home:/home/node/.dsh \
  -e DEEPSEEK_API_KEY \
  ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:dsh-v0.2.1-rc.2
```

**DSH Web is not usable at a bare `/`.** Without its launch-token cookie it answers `401`, and
the token is only printed in the startup logs:

```bash
docker logs dsh-web 2>&1 | grep -m1 '?token='
# dsh web: http://localhost:3080/?token=...
```

Open the printed `?token=...` URL once — DSH sets its auth cookie and the UI loads normally
afterwards. Hitting `/` directly (or after the cookie expires) returns `401` by design; that is
why Kubernetes probes use `/favicon.svg` (see below) and why the gateway performs this
handshake for the browser.

> **Different host port?** If `3080` is already taken on your workstation, map another port and
> set the public URL so the printed token URL matches: `-p 18081:3080 -e
> DSH_PUBLIC_URL=http://localhost:18081`, then open `http://localhost:18081/?token=...`.

## Quick start (Kubernetes)

```bash
kubectl create namespace dsh-demo --dry-run=client -o yaml | kubectl apply -f -
kubectl -n dsh-demo create secret generic dsh-provider-keys \
  --from-literal=DEEPSEEK_API_KEY="sk-..."
kubectl apply -f deploy/k8s/sandbox-template.yaml

# A template needs a warm pool and a claim to become a running Sandbox.
kubectl apply -f - <<'EOF'
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxWarmPool
metadata:
  name: dsh-pool
  namespace: dsh-demo
spec:
  replicas: 1
  sandboxTemplateRef:
    name: dsh-web-template
---
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxClaim
metadata:
  name: dsh
  namespace: dsh-demo
spec:
  warmPoolRef:
    name: dsh-pool
EOF
kubectl -n dsh-demo wait sandboxclaim/dsh --for=condition=Ready --timeout=180s
```

Then reach the instance through its launch-token URL:

```bash
SB=$(kubectl -n dsh-demo get sandboxclaim dsh -o jsonpath='{.status.sandbox.name}')
kubectl -n dsh-demo logs "$SB" | grep -m1 '?token='
kubectl -n dsh-demo port-forward "pod/$SB" 18081:3080
# open http://localhost:18081/?token=... from the log line
```

Two things bite first-time setups:

- **Probes must not hit `/`.** DSH returns `401` there without the launch token, so a
  readiness/liveness probe on `/` crash-loops the pod. The shipped templates use
  `/favicon.svg`, which is unauthenticated; keep that path in custom templates.
- **Host allow-listing.** DSH rejects unknown `Host` headers. `localhost` and `127.0.0.1` are
  always trusted; for any other hostname add it to the template's `DSH_TRUSTED_HOSTS` (comma or
  space separated). The gateway forwards the browser's `Host`, so the public gateway hostname
  must be listed there.

## The multi-user platform pattern

`examples/dsh-agents-as-a-service/` is a port of the upstream
[hermes-agents-as-a-service](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/examples/hermes-agents-as-a-service)
example to DSH Web. It demonstrates the platform shape — one `SandboxTemplate`, one warm pool,
one tiny `SandboxClaim` per user, and `operatingMode` as the cost dial — plus a gateway that
turns it into a usable service.

**Browser flow:** open the gateway landing page, sign up, and get an `HttpOnly` session cookie.
The browser then browses `http://<gateway>/u/<user>/`, which proxies HTTP **and WebSocket** to
that user's DSH Web. The gateway reads the DSH launch token from the pod logs, performs the
token handshake server-side, and injects the resulting DSH cookie on every proxied request — the
browser never sees the token. Suspended instances are woken transparently on the next request;
idle ones are suspended again after `IDLE_TIMEOUT`.

See the [example README](examples/dsh-agents-as-a-service/README.md) for the full walkthrough
and [docs/architecture.md](docs/architecture.md) for the diagrams and trade-offs.

### Mechanism mapping: Hermes → DSH

| Hermes example mechanism | Role | Adaptation for DSH |
| :--- | :--- | :--- |
| `SandboxTemplate` | Reusable Pod blueprint | Image `dsh-web`, port `3080`, probes on `/favicon.svg` |
| `volumeClaimTemplates` | Auto-create a PVC per user | Mount `/home/node/.dsh` (DSH state) |
| `SandboxWarmPool` | Pre-warmed instances | Keep as-is; size for expected concurrency |
| `SandboxClaim` + `warmPoolRef` | "Registration" adopts a pooled instance | Keep exactly as-is; one claim per user |
| `service: true` | Stable DNS per Sandbox | Enabled, so the gateway routes to each user's DSH Web |
| `envVarsInjectionPolicy: Disallowed` | Reject claims that would cold-start | `DEEPSEEK_API_KEY` injected via `envFrom` on the template (shared platform key) |
| `operatingMode` | Cost switch: Running / Suspended | Reused directly — suspend idle instances to save cost |

### Key differences from Hermes

- **Multi-arch** — the DSH image is built for amd64 and arm64 (no amd64 nodeSelector).
- **Single port** — DSH Web serves everything on `3080`; no API/dashboard port split.
- **One secret** — only `DEEPSEEK_API_KEY`; no dashboard/session/api-server credentials.
- **Host forwarding** — the gateway forwards `Host` + `X-Forwarded-*` (and keeps
  `DSH_PUBLIC_URL` empty) so DSH generates reachable links; add the public hostname to
  `DSH_TRUSTED_HOSTS`.
- **Browser sessions + WebSocket** — the gateway is Starlette ASGI, so it proxies WebSocket
  upgrades as well as HTTP/SSE, and authenticates browsers with an `HttpOnly` cookie instead of
  exposing per-user bearer tokens in URLs.

## Build it yourself

```bash
./build.sh              # build the DSH Web image, then run it interactively
./build.sh web          # build the DSH Web image only
./build.sh gateway      # build the gateway image

make image              # same as ./build.sh web
make gateway            # gateway image
make test               # gateway unit tests (pytest)
make lint               # gateway lint (ruff)
```

Or directly:

```bash
docker build \
  --build-arg DSH_VERSION=dsh-v0.2.1-rc.2 \
  --build-arg PNPM_VERSION=11.28.5 \
  -t dsh-web:0.2.1-rc.2 .

docker build -t ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:gateway \
  examples/dsh-agents-as-a-service/gateway/
```

See [`Dockerfile`](Dockerfile) for build args (`DSH_REPO`, `DSH_VERSION`, `PNPM_VERSION`) and
the runtime image contract (non-root `node` user, `DSH_HOME=/home/node/.dsh`, `EXPOSE 3080`).
`.env.sample` documents the runtime variables for both images.
