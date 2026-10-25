# deepseek-kubernetes-sandbox

**DeepSeek Harness Web, packaged as a container.** This repository's primary
artifact is a slim, non-root, multi-arch image of
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) Web
(`dsh web`), ready for Docker and Kubernetes. Everything else exists to support
that image or to demonstrate an implementation on top of it.

The flagship implementation is
[`examples/dsh-agents-as-a-service/`](examples/dsh-agents-as-a-service/): the
"agents-as-a-service" platform pattern with one DSH Web instance per user,
provisioned from a warm pool on
[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox),
suspended when idle, and fronted by a small browser-usable gateway.

## Quick start

```bash
docker run -d --name dsh-web \
  -p 3080:3080 \
  -v dsh-home:/home/node/.dsh \
  -e DEEPSEEK_API_KEY \
  ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:dsh-v0.2.1-rc.3-r3
```

DSH Web is **not usable at a bare `/`**. Without its launch-token cookie it
answers `401`, and the token is printed only in the startup logs:

```bash
docker logs dsh-web 2>&1 | grep -m1 '?token='
# dsh web: http://localhost:3080/?token=...
```

Open the printed `?token=...` URL once: DSH sets its auth cookie and the UI loads
normally afterwards. Hitting `/` directly (or after the cookie expires) returns
`401` by design.

### Host port and public URL

If `3080` is taken locally, map another port and set the public URL so the quoted
token URL matches:

```bash
docker run -d --name dsh-web -p 18081:3080 \
  -e DSH_PUBLIC_URL=http://localhost:18081 \
  -v dsh-home:/home/node/.dsh \
  -e DEEPSEEK_API_KEY \
  ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:dsh-v0.2.1-rc.3-r3
# then open http://localhost:18081/?token=...
```

### Hosts, probes and the 401

- **Probes must not hit `/`.** It returns `401` without the launch token, so a
  readiness/liveness probe on `/` crash-loops the pod. Probe `/favicon.svg`
  instead: it is unauthenticated. The shipped manifests use it.
- **Host allow-listing.** DSH rejects unknown `Host` headers. `localhost` and
  `127.0.0.1` are always trusted; add any other public hostname to
  `DSH_TRUSTED_HOSTS` (comma or space separated). A gateway or Ingress that
  forwards the browser's `Host` needs its hostname listed there.
- **State lives in `/home/node/.dsh`.** Mount a volume (Docker) or PVC
  (Kubernetes) to keep conversations, sessions and config across restarts.

### Runtime environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | empty | Provider credential consumed by DSH. Inject it; never bake it into the image or commit it. |
| `DSH_PORT` | `3080` | Port DSH Web listens on inside the container. |
| `DSH_HOST` | auto-detected pod/container IP | Bind address. Leave empty in Kubernetes. |
| `DSH_PUBLIC_URL` | `http://localhost:${DSH_PORT}` | Public base URL used in generated links and the launch-token URL in the logs. |
| `DSH_TRUSTED_HOSTS` | empty | Extra trusted `Host`/`Origin` entries, comma/space separated. |
| `DSH_HOME` | `/home/node/.dsh` | DSH state directory (mount a volume here). |

Full contract, build args, probes, volumes and signals:
[docs/main-image.md](docs/main-image.md).

## Published images

Published to GitHub Container Registry. The main image is the repository image;
example components are nested packages below it:

```
ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox                              # DSH Web (main image)
ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox/dsh-agents-as-a-service/gateway   # example gateway
```

| Tag | Contents | Meaning |
| --- | --- | --- |
| `dsh-v0.2.1-rc.3-r3` | DSH Web, port `3080` | **Current release identity.** Immutable: once published it never moves. |
| `latest` | DSH Web | Most recent release; same digest as the current identity. |
| `main` | DSH Web | Development channel rebuilt from every push to `main`. Not a release; not for production. |
| `dsh-v0.2.1-rc.3` | DSH Web | **Legacy tag** published before the release-identity scheme. It is not updated. New deployments use `dsh-v0.2.1-rc.3-r3`. |

The example gateway is published under its nested path with the same release
identity, e.g.
`ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox/dsh-agents-as-a-service/gateway:dsh-v0.2.1-rc.3-r3`.

Release identities, channels and rollback rules are explained in
[docs/versioning.md](docs/versioning.md). Production deployments should pin a
digest:

```bash
docker buildx imagetools inspect ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:dsh-v0.2.1-rc.3-r3
# image: ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox@sha256:<digest>
```

## Build from source

```bash
make image    # build the main DSH Web image (clones and builds upstream DSH)
make run      # build it, then run it interactively (needs DEEPSEEK_API_KEY)
```

The pins (`DSH_VERSION`, `RELEASE_REVISION`, `PNPM_VERSION`) come from
[versions.env](versions.env) — never hard-code them
(`make check` verifies). [`build.sh`](build.sh) is a deprecated shim that
forwards to `make`; use the Makefile targets.

## Examples

Self-contained implementations that consume the image. Index and contract:
[`examples/README.md`](examples/README.md) and
[docs/examples.md](docs/examples.md).

| Example | What it demonstrates |
| --- | --- |
| [`examples/dsh-agents-as-a-service/`](examples/dsh-agents-as-a-service/) | Full multi-tenant platform: SandboxTemplate with per-user PVCs, warm pool, one claim per user, suspend-when-idle, wake-on-return, browser gateway with signup + `HttpOnly` sessions + HTTP/WebSocket proxy, Ingress or Gateway API + GitHub SSO exposure. Start with the [walkthrough](examples/dsh-agents-as-a-service/README.md) and the [architecture](examples/dsh-agents-as-a-service/docs/architecture.md). |
| [`examples/minimal-single-tenant/`](examples/minimal-single-tenant/) | The smallest useful setup: one `SandboxTemplate` and one `SandboxClaim`, reached through `kubectl port-forward` + the launch-token URL. Manifest: [`manifests/sandbox-template.yaml`](examples/minimal-single-tenant/manifests/sandbox-template.yaml). |

The gateway is a component of the multi-tenant example and has its own
[README](examples/dsh-agents-as-a-service/gateway/README.md) (API, configuration,
auth/SSO, limitations).

## Repository layout

| Path | Purpose |
| --- | --- |
| [`Dockerfile`](Dockerfile) | Primary artifact: the multi-stage DSH Web image (port `3080`). |
| [`versions.env`](versions.env) | Single source of truth for the DSH ref, repository revision, pnpm/control-plane versions and image names. |
| [`Makefile`](Makefile), [`build.sh`](build.sh) | Canonical local entrypoint; `build.sh` is a deprecated shim. |
| [`docs/`](docs/README.md) | Repository docs: image contract, example contract, gitflow, versioning. |
| [`examples/`](examples/README.md) | One self-contained implementation per directory. |
| [`examples/dsh-agents-as-a-service/`](examples/dsh-agents-as-a-service/) | Multi-tenant platform example: `manifests/`, `gateway/`, `docs/`, `scripts/`. |
| [`examples/minimal-single-tenant/`](examples/minimal-single-tenant/) | Single-sandbox quickstart. |
| [`scripts/`](scripts/) | Repository checks: versions, example layout, manifest YAML, version bump. |
| [`.github/`](.github/) | CI, release automation, dependency updates, PR/issue templates. |
| [`.env.sample`](.env.sample) | Documented local configuration (copy to `.env`; never commit it). |

## Development

The Makefile is the canonical entrypoint. No cluster is needed for `check`,
`test`, `lint`, `gateway` or `image`:

| Target | What it does |
| --- | --- |
| `make check` | Version pins, example layout and manifest YAML checks. Run before every commit. |
| `make test` | Gateway unit tests (`pytest`). |
| `make lint` | `ruff` + `shellcheck` (+ `yamllint`/`actionlint` when installed). |
| `make image` | Build the main DSH Web image. |
| `make gateway` | Build the example gateway image locally (`dsh-aaas-gateway:dev`). |
| `make e2e` | Full kind walkthrough of the multi-tenant example (needs a cluster with agent-sandbox). |
| `make changelog` | Regenerate `CHANGELOG.md` with git-cliff (release only). |
| `make set-version` | Bump pins in `versions.env`; e.g. `make set-version DSH_VERSION=<ref> RELEASE_REVISION=rN`. |

Local setup and the PR workflow: [CONTRIBUTING.md](CONTRIBUTING.md); branches,
required checks and releases: [docs/gitflow.md](docs/gitflow.md).

## Versioning and releases

The release identity is the upstream DSH ref plus an optional repository
revision: `DSH_VERSION` and `RELEASE_REVISION` in [versions.env](versions.env).
The current identity is **`dsh-v0.2.1-rc.3-r3`** — the first release under the
mirrored-DSH scheme at the same upstream DSH ref as the legacy
`dsh-v0.2.1-rc.3` tag. Tags are immutable; a broken release is fixed by
publishing `-r(N+1)`, never by re-tagging.

`CHANGELOG.md` is generated by [git-cliff](https://git-cliff.org) from
Conventional Commits and is never edited by hand. See
[`CHANGELOG.md`](CHANGELOG.md), [docs/versioning.md](docs/versioning.md) and
[cliff.toml](cliff.toml).

## Migrating from the previous layout

If you are coming from an older checkout, these paths moved. Nothing else changed
semantically; the content was split into the example it belongs to.

| Old path | New path |
| --- | --- |
| `gateway-api/dsh-demo/` (Gateway API + oauth2-proxy / GitHub SSO) | `examples/dsh-agents-as-a-service/manifests/exposure/gateway-api-sso/` (now `70-gateway.yaml` ... `74-tls-secret.yaml.example`) |
| `examples/dsh-agents-as-a-service/60-ingress.yaml` | `examples/dsh-agents-as-a-service/manifests/exposure/ingress/60-ingress.yaml` |
| `examples/dsh-agents-as-a-service/NN-*.yaml` (`00-` ... `50-`) | `examples/dsh-agents-as-a-service/manifests/NN-*.yaml` |
| `examples/dsh-agents-as-a-service/run-test-kind.sh` | `examples/dsh-agents-as-a-service/scripts/e2e-kind.sh` |
| `deploy/k8s/sandbox-template.yaml` | `examples/minimal-single-tenant/manifests/sandbox-template.yaml` |
| `docs/architecture.md` | `examples/dsh-agents-as-a-service/docs/architecture.md` |

The SSO walkthrough that lived in `gateway-api/dsh-demo/README.md` is merged
into the [multi-tenant example README](examples/dsh-agents-as-a-service/README.md)
(step 9) and the [exposure README](examples/dsh-agents-as-a-service/manifests/exposure/README.md).

## Contributing

- [CONTRIBUTING.md](CONTRIBUTING.md) — local setup, PR workflow, what a change needs.
- [AGENTS.md](AGENTS.md) — repository conventions for AI coding agents (and humans) and the CI commands.
- [SECURITY.md](SECURITY.md) — private vulnerability reporting and project threat notes.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — community standards.
- [docs/gitflow.md](docs/gitflow.md) — branches, protected `main`, required checks, release process.

Do not commit secrets; secret-shaped YAML lives in `*.yaml.example` files, and
version pins change only through `versions.env` / `make set-version`.

## License

[MIT](LICENSE) © Nicolás Georger.
