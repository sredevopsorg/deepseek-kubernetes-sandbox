# Examples

Every implementation in this repository is a self-contained example: one
directory, `kubectl` (plain manifests or `kubectl apply -k` of a Kustomize
tree), an end-to-end README, and a cleanup command. Examples consume the
[main DSH Web image](../docs/main-image.md); they are reference material, not
libraries.

| Example | What it demonstrates | Needs |
| --- | --- | --- |
| [`examples/dsh-agents-as-a-service/`](dsh-agents-as-a-service/) | The full multi-tenant platform pattern: one `SandboxTemplate` with per-user PVCs, a warm pool, one ~10-line `SandboxClaim` per user, suspend-when-idle / wake-on-return, and a browser gateway with signup, `HttpOnly` sessions, an HTTP + WebSocket proxy and two exposure variants (Ingress, or Gateway API + GitHub SSO). | agent-sandbox with extensions; kind for the e2e script. |
| [`examples/minimal-single-tenant/`](minimal-single-tenant/) | The smallest useful setup: a single `SandboxTemplate` and one `SandboxClaim`, no gateway — reach DSH Web with a launch-token URL through `kubectl port-forward`. | agent-sandbox with extensions. |

The multi-tenant example also documents opt-in gVisor, `nodeSelector`,
NetworkPolicy, extra provider-key hardening and failed-Pod recovery in
[optional hardening](dsh-agents-as-a-service/docs/optional-hardening.md).

## The example contract (short version)

Full rules: [docs/examples.md](../docs/examples.md). `scripts/check-examples.sh`
enforces the layout, and `make check` runs it.

- `README.md` is the walkthrough; manifests are numbered `NN-*.yaml`, either at
  the top of `manifests/` (plain `kubectl apply -f`) or in `manifests/base/`
  under a `kustomization.yaml` (Kustomize, applied with `kubectl apply -k`).
- Optional `manifests/optional/<feature>/` and `manifests/exposure/<variant>/`
  each ship a README and their own `kustomization.yaml`; exposure variants are
  mutually exclusive overlays, not a stack.
- Cluster-specific values go in the documented `manifests/local/` overlay, not
  in the base; apply it with `kubectl apply -k manifests/local/`.
- Optional `scripts/` must be executable, idempotent and self-cleaning; `docs/`
  holds deeper material.
- Components with a Dockerfile need a README and tests; images are published
  nested below the main image (`<registry>/<owner>/<repo>/<example>/<component>`).
- Secrets are `*.yaml.example` with placeholders (never `secretGenerator`
  inputs); every example is listed above with its literal `examples/<name>/`
  path.

## Running an example

Each example README has the exact prerequisites, commands, optional hardening,
exposure options and teardown. The multi-tenant example also has a full kind
walkthrough that cleans up after itself:

```bash
examples/dsh-agents-as-a-service/scripts/e2e-kind.sh
```

The multi-tenant example is a Kustomize tree. Apply one root, not a stack: the
base (`kubectl apply -k manifests/base/`), or exactly one exposure variant
(`kubectl apply -k manifests/exposure/<variant>/`), which includes the base. The
optional component and the local overlay are applied explicitly with
`kubectl apply -k`; `kubectl apply -f manifests/` is not the interface.
