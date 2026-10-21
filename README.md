# deepseek-kubernetes-sandbox

Packages [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) Web (`dsh web`) into
a multi-arch, Kubernetes-ready container image, and ships the
[agents-as-a-service](examples/dsh-agents-as-a-service/) platform pattern — running a personal
DSH Web instance per user on [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox).

## What's in this repo

| Path | Purpose |
| --- | --- |
| [`Dockerfile`](Dockerfile) | Multi-stage build that clones DSH, `pnpm pack`s the CLI, and produces a slim runtime image |
| [`build.sh`](build.sh) | Local build + run (builds `dsh-web:<version>` and runs it) |
| [`.github/workflows/multi-build.yaml`](.github/workflows/multi-build.yaml) | CI build (amd64 + arm64) and push to GHCR |
| [`deploy/k8s/sandbox-template.yaml`](deploy/k8s/sandbox-template.yaml) | Minimal single-tenant `SandboxTemplate` |
| [`examples/dsh-agents-as-a-service/`](examples/dsh-agents-as-a-service/) | The full multi-tenant platform pattern (warm pool, per-user claims, gateway, smoke test) |

## Quick start (image)

The image is published to GitHub Container Registry as
`ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:<tag>`, where `<tag>` is `main`,
`latest`, or a DSH version tag such as `dsh-v0.2.1-rc.2`.

```bash
docker run --rm -it \
  -p 3080:3080 \
  -v dsh-home:/home/node/.dsh \
  -e DEEPSEEK_API_KEY \
  ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:dsh-v0.2.1-rc.2
```

> **Local port note:** `dsh web` listens on `3080` inside the container. If `3080` is
> already in use on your workstation, map a different host port (e.g. `-p 18081:3080`).

## The multi-user platform pattern

`examples/dsh-agents-as-a-service/` is a port of the upstream
[hermes-agents-as-a-service](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/examples/hermes-agents-as-a-service)
example to DSH Web. It demonstrates the platform shape — one `SandboxTemplate`, one warm pool,
one tiny `SandboxClaim` per user, and `operatingMode` as the cost dial — plus a gateway that
turns it into a usable service (signup, per-user tokens, `/u/{user}/**` proxy with
wake-on-connect, idle suspend). See its [README](examples/dsh-agents-as-a-service/README.md) for
the full walkthrough.

### Mechanism mapping: Hermes → DSH

| Hermes example mechanism | Role | Adaptation for DSH |
| :--- | :--- | :--- |
| `SandboxTemplate` | Reusable Pod blueprint | Image `dsh-web`, port `3080` |
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
  `DSH_PUBLIC_URL` empty) so DSH generates reachable links.
- **WebSocket caveat** — the requests-based proxy handles SSE but not WebSocket upgrades; use a
  path-based Ingress per user for full web-GUI interactivity.

## Build it yourself

```bash
./build.sh                              # docker build + run (dsh-web:0.2.1-rc.2)
# or
docker build \
  --build-arg DSH_VERSION=dsh-v0.2.1-rc.2 \
  -t dsh-web:0.2.1-rc.2 .
```

See [`Dockerfile`](Dockerfile) for build args (`DSH_REPO`, `DSH_VERSION`, `PNPM_VERSION`) and
the runtime image contract (non-root `node` user, `DSH_HOME=/home/node/.dsh`, `EXPOSE 3080`).
