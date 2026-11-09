# Example contract

Every implementation in this repository is a **self-contained example** under
[`examples/<name>/`](../examples/). Examples demonstrate how to run the main
DSH Web image (and, when needed, auxiliary components such as the
agents-as-a-service gateway) on Kubernetes. They are reference material, not
libraries: each one is applied with `kubectl` — plain manifests with
`kubectl apply -f`, or a Kustomize tree with `kubectl apply -k` — documented end
to end, and deletable.

[`examples/README.md`](../examples/README.md) is the index. If an example is not
listed there, it does not exist as far as `make check` is concerned.

## Required layout

Two manifest layouts are supported. A small example uses numbered top-level
manifests; a larger one uses a Kustomize base plus sibling overlays.

```
examples/<name>/
+-- README.md                     # required: what it demonstrates, prerequisites,
|                                 #   ordered steps, troubleshooting, cleanup
+-- manifests/                    # required
|   +-- NN-*.yaml                 #   plain layout: numbered manifests, apply -f
|   +-- base/                     #   OR Kustomize layout: portable base
|   |   +-- kustomization.yaml
|   |   +-- NN-*.yaml             #     numbered, apply -k manifests/base
|   +-- optional/                 #   optional opt-in manifests/components
|   |   +-- README.md             #     required when optional/ exists
|   |   +-- <feature>/            #     e.g. network-policy/
|   |       +-- kustomization.yaml
|   |       +-- *.yaml, patches/
|   +-- exposure/                 #   optional exposure variants
|   |   +-- README.md             #     required when exposure/ exists
|   |   +-- <variant>/            #     e.g. ingress/, gateway-api-sso/
|   |       +-- kustomization.yaml
|   |       +-- *.yaml, patches/
|   +-- local/                    #   optional cluster-specific overlay
|       +-- kustomization.yaml
|       +-- patches/
|       +-- README.md             #     documents how to compose it
+-- scripts/                      # optional
|   +-- *.sh                      #   executable, idempotent, self-cleaning
+-- docs/                         # optional deeper material (architecture, ADRs)
+-- <component>/                  # optional published component
    +-- Dockerfile
    +-- README.md                 #   required whenever a Dockerfile exists
    +-- tests...
```

Every directory applied with `kubectl apply -k` is a kustomization root and
contains exactly one `kustomization.yaml`; overlay roots are siblings of the
base they reference, never nested inside it.

### Rules the CI enforces

[scripts/check-examples.sh](../scripts/check-examples.sh) fails the build when:

- `examples/README.md` is missing or does not mention `examples/<name>/` for every
  example directory;
- an example has no `README.md` or no `manifests/` directory;
- the plain layout has no top-level `NN-*.yaml`, or the Kustomize layout has no
  top-level `manifests/base/NN-*.yaml`;
- a top-level manifest under `manifests/` or `manifests/base/` is not named
  `NN-*.yaml` — kustomization files (`kustomization.yaml`, `kustomization.yml`,
  `Kustomization`) are never treated as manifests;
- a `*.yaml` file sits at the example root instead of under `manifests/`;
- `manifests/exposure/` exists without a `manifests/exposure/README.md` describing
  the variants;
- `manifests/optional/` exists without a `manifests/optional/README.md`
  describing the opt-in manifests;
- a subdirectory with a `Dockerfile` has no `README.md`;
- a file in `scripts/` is not executable.

[scripts/check-manifests.py](../scripts/check-manifests.py) parses every
`manifests/**/*.yaml` **except** kustomization files and files under a `patches/`
directory, and requires `apiVersion`, `kind` and `metadata.name`. It also
enforces `spec.podTemplate.spec.automountServiceAccountToken: false` on every
`SandboxTemplate`.

CI then runs a server-side dry-run against the real agent-sandbox CRDs on a
throwaway kind cluster, one full manifest object at a time. Kustomization files
and `patches/` fragments are skipped there too: a kustomization describes a build
and a patch file is a partial document, so neither is an object the API server
can accept. **CI does not render the Kustomize tree**; render it locally with
`kubectl kustomize <root>` before applying.

Run the checks locally with:

```bash
make check
```

## Manifests

- **Numbered and ordered.** `NN-*.yaml` files are applied in filename order and
  read as a walkthrough: `00-prereqs.yaml`, `10-sandboxtemplate.yaml`,
  `20-sandboxwarmpool.yaml`, ... In a plain layout they sit at the top of
  `manifests/`; in a Kustomize layout they are the portable base in
  `manifests/base/`. Keep the numbering stable; it is referenced from the
  example README.
- **kubectl or Kustomize.** `kubectl apply -f` of the numbered manifests is the
  whole interface for a plain example. A larger example may ship a Kustomize
  tree instead: `manifests/base/` is the portable base and overlays live in
  sibling directories (`manifests/exposure/<variant>/`,
  `manifests/optional/<feature>/`, `manifests/local/`), applied with
  `kubectl apply -k <root>` and rendered with `kubectl kustomize <root>`.
  Kustomize is built into `kubectl`, so no extra tool is needed. No Helm and no
  custom installer.
- **Bases are portable.** No active cluster-specific values, namespaces or
  environment literals in `base/`; overlays add them. An overlay references its
  base with a relative `resources:` entry and the dependency arrow points one
  way: overlay → base.
- **Overlays are siblings, not children.** A base must never reference an
  overlay, and an overlay must not be nested inside the base directory it
  references. Kustomize detects that as a cycle (`candidate root ... contains
  visited root ...`), so keep the tree flat: `manifests/base/` +
  `manifests/exposure/ingress/`, never `manifests/base/exposure/ingress/`.
- **Patches are build inputs.** Patch files live in a `patches/` directory next
  to the kustomization that references them. They are partial documents
  (strategic-merge or JSON-6902 lists), applied only through that
  kustomization — never `kubectl apply -f` directly.
- **Compose before you apply.** If two overlays are always applied together they
  are one overlay. An example that needs per-cluster values ships
  `manifests/local/`, a static overlay that composes the base (or exactly one
  exposure variant) plus optional components and the enabled patches; apply it
  with `kubectl apply -k manifests/local/` instead of chaining separate `-k`
  invocations.
- **Safe by default.** Never ship a usable Secret. Secret-shaped manifests are
  `*.yaml.example` files with obvious placeholders and an inline creation
  command; real values are generated per install. Never commit `secretGenerator`
  input files or literals: generated Secret data is base64, not encryption.
- **Cluster-specific state stays out.** Node affinity, replica counts tuned for a
  cluster, and similar fields belong in a local overlay, not in the shipped base.
  Optional scheduler/runtime hints may ship **commented out** with placeholder
  labels; they must not be active. When an example needs per-cluster values,
  ship a documented static Kustomize overlay — for example
  [`dsh-agents-as-a-service/manifests/local/`](../examples/dsh-agents-as-a-service/manifests/local/README.md):
  edit the `patches/` files, uncomment the entries you need in its
  `kustomization.yaml`, then `kubectl apply -k manifests/local/`. A read-only
  [`scripts/cluster-drift.sh`](../examples/dsh-agents-as-a-service/scripts/cluster-drift.sh)
  is the companion check.
- **Optional manifests are opt-in.** Put them under
  `manifests/optional/<feature>/`, document them in
  `manifests/optional/README.md`, and apply them explicitly: an additive
  Kustomize `Component` with its own `resources:` can be applied with
  `kubectl apply -k manifests/optional/<feature>/`, or imported into a composed
  overlay via `components:`. They are never part of the base apply.
- **Placeholders are explicit.** Hostnames, GitHub orgs and TLS material use
  values like `dsh.example.com` / `your-github-org` with a TODO comment.

## Exposure variants

An example that can be reached from outside the cluster puts each option under
`manifests/exposure/<variant>/` and describes the trade-offs in
`manifests/exposure/README.md`. Each variant is a Kustomize overlay with its own
`kustomization.yaml` that includes the base and patches the gateway environment
(and the trusted host); apply exactly one with
`kubectl apply -k manifests/exposure/<variant>/`. Variants are alternatives, not
a stack: never compose two; applying the base separately first is allowed but
unnecessary because the overlay already includes it. `dsh-agents-as-a-service`
currently ships:

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

Scripts in a Kustomize example use the documented `kubectl apply -k` roots; a
step-by-step walkthrough may replay the numbered base files in order. Scripts
must not hand-patch values that a kustomization already owns. Read-only
verification scripts (for example `preflight-sso.sh` and `cluster-drift.sh`) are
allowed alongside mutating ones; they must state that they change nothing and
must exit non-zero on the condition they check.

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
3. Add `manifests/NN-*.yaml` (plain layout) or `manifests/base/NN-*.yaml` plus
   `kustomization.yaml` (Kustomize layout); keep secrets as `*.yaml.example`.
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
