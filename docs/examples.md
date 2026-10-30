# Example contract

Every implementation in this repository is a **self-contained example** under
[`examples/<name>/`](../examples/). Examples demonstrate how to run the main
DSH Web image (and, when needed, auxiliary components such as the
agents-as-a-service gateway) on Kubernetes. They are reference material, not
libraries: each one is applied with plain `kubectl`, documented end to end, and
deletable in one command.

[`examples/README.md`](../examples/README.md) is the index. If an example is not
listed there, it does not exist as far as `make check` is concerned.

## Required layout

```
examples/<name>/
+-- README.md                     # required: what it demonstrates, prerequisites,
|                                 #   ordered steps, troubleshooting, cleanup
+-- manifests/                    # required
|   +-- NN-*.yaml                 #   numbered top-level manifests, plain kubectl
|   +-- optional/                 #   optional opt-in manifests, applied by path
|       +-- README.md             #     required when optional/ exists
|       +-- <feature>/NN-*.yaml   #     e.g. network-policy/
|   +-- exposure/                 #   optional exposure variants
|       +-- README.md             #     required when exposure/ exists
|       +-- <variant>/NN-*.yaml   #     e.g. ingress/, gateway-api-sso/
+-- scripts/                      # optional
|   +-- *.sh                      #   executable, idempotent, self-cleaning
+-- docs/                         # optional deeper material (architecture, ADRs)
+-- <component>/                  # optional published component
    +-- Dockerfile
    +-- README.md                 #   required whenever a Dockerfile exists
    +-- tests...
```

### Rules the CI enforces

[scripts/check-examples.sh](../scripts/check-examples.sh) fails the build when:

- `examples/README.md` is missing or does not mention `examples/<name>/` for every
  example directory;
- an example has no `README.md` or no `manifests/` directory;
- `manifests/` contains no top-level `*.yaml` files;
- a top-level `manifests/*.yaml` is not named `NN-*.yaml`;
- a `*.yaml` file sits at the example root instead of under `manifests/`;
- `manifests/exposure/` exists without a `manifests/exposure/README.md` describing
  the variants;
- `manifests/optional/` exists without a `manifests/optional/README.md`
  describing the opt-in manifests;
- a subdirectory with a `Dockerfile` has no `README.md`;
- a file in `scripts/` is not executable.

[scripts/check-manifests.py](../scripts/check-manifests.py) additionally parses
every `manifests/**/*.yaml` and requires `apiVersion`, `kind` and
`metadata.name`. CI also runs a server-side dry-run against the real
agent-sandbox CRDs on a throwaway kind cluster.

Run both locally with:

```bash
make check
```

## Manifests

- **Numbered and ordered.** `NN-*.yaml` files are applied in filename order and
  read as a walkthrough: `00-prereqs.yaml`, `10-sandboxtemplate.yaml`,
  `20-sandboxwarmpool.yaml`, ... Keep the numbering stable; it is referenced from
  the example README.
- **Plain kubectl.** No Helm, Kustomize, or custom installer. `kubectl apply -f`
  against a namespace the example creates is the whole interface.
- **Safe by default.** Never ship a usable Secret. Secret-shaped manifests are
  `*.yaml.example` files with obvious placeholders and an inline creation
  command; real values are generated per install.
- **Cluster-specific state stays out.** Node affinity, replica counts tuned for a
  cluster, and similar fields belong in a local patch, not in the shipped
  manifest. Optional scheduler/runtime hints may ship **commented out** with
  placeholder labels; they must not be active.
- **Optional manifests are opt-in.** Put them under
  `manifests/optional/<feature>/`, document them in
  `manifests/optional/README.md`, and apply them by path. The top-level
  `kubectl apply -f manifests/` is non-recursive and does not apply them.
- **Placeholders are explicit.** Hostnames, GitHub orgs and TLS material use
  values like `dsh.example.com` / `your-github-org` with a TODO comment.

## Exposure variants

An example that can be reached from outside the cluster puts each option under
`manifests/exposure/<variant>/` and describes the trade-offs in
`manifests/exposure/README.md`. Variants are alternatives, not a stack: apply one.
`dsh-agents-as-a-service` currently ships:

| Variant | Path | What it gives you |
| --- | --- | --- |
| Ingress | `manifests/exposure/ingress/` | Traefik-style Ingress + TLS; the gateway does its own session auth. |
| Gateway API + SSO | `manifests/exposure/gateway-api-sso/` | Gateway API `Gateway`/`HTTPRoute` in front of oauth2-proxy (GitHub OAuth); the gateway inherits `X-Forwarded-User` via `AUTH_PROXY_HEADER`. |

## Components

A component is a program the example needs beyond the main image: today only the
agents-as-a-service gateway. A component lives in its own subdirectory with:

- `Dockerfile` — its image build;
- `README.md` — purpose, API/config reference, local development, limitations;
- tests — unit tests that run without a cluster, plus whatever else applies.

Component images are published as **nested packages below the repository image**:

```
<registry>/<owner>/<repo>/<example>/<component>
```

so the gateway is
`ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox/dsh-agents-as-a-service/gateway`.
Never create an unrelated top-level package; `check-versions.sh` verifies the
nesting.

## Scripts

`scripts/*.sh` must be:

- **executable** (the CI checks the mode bit);
- **idempotent** — safe to re-run, cleaning up before creating;
- **self-cleaning** — a trap removes the namespace, port-forwards and temp files,
  even on failure;
- **non-interactive** — usable in CI, with environment-variable knobs instead of
  prompts.

`examples/dsh-agents-as-a-service/scripts/e2e-kind.sh` is the reference
implementation: it runs the README walkthrough and deletes everything it created.

## Documentation

- The example `README.md` is the walkthrough. Keep it runnable: every command
  block should work when pasted in order.
- Deeper material (architecture, sequence diagrams, ADRs) belongs in the
  example's `docs/`, not in the repository-level [docs/](../docs/).
- Repository-wide docs describe the container and the repo rules; example docs
  describe one implementation.

## Adding a new example

1. Create `examples/<name>/` with the layout above.
2. Write `README.md` first: what it demonstrates, prerequisites, numbered steps,
   troubleshooting, limitations, cleanup.
3. Add `manifests/NN-*.yaml`; keep secrets as `*.yaml.example`.
4. If a component needs an image, publish it under
   `<registry>/<owner>/<repo>/<example>/<component>`.
5. Add an entry to [`examples/README.md`](../examples/README.md) using the literal
   `examples/<name>/` path.
6. Run `make check` and, if you added a component, `make test` and `make lint`.
7. If the example is expensive to run (kind, images, cluster CRDs), ship a
   `scripts/e2e-kind.sh` and wire it into CI.

## Current examples

| Example | Demonstrates |
| --- | --- |
| [`examples/dsh-agents-as-a-service/`](../examples/dsh-agents-as-a-service/) | The full multi-tenant platform pattern: SandboxTemplate, warm pool, per-user claims, suspend/resume, and the browser gateway. |
| [`examples/minimal-single-tenant/`](../examples/minimal-single-tenant/) | A single DSH Web sandbox: one SandboxTemplate and a claim. |
