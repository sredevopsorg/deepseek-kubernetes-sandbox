# Examples

Every implementation in this repository is a self-contained example: one
directory, plain `kubectl`, an end-to-end README, and a cleanup command.
Examples consume the [main DSH Web image](../docs/main-image.md); they are
reference material, not libraries.

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

- `README.md` is the walkthrough; `manifests/NN-*.yaml` is applied with plain
  kubectl and read in order.
- Optional `manifests/optional/<feature>/` and `manifests/exposure/<variant>/`
  each ship a README; exposure variants are mutually exclusive.
- Optional `scripts/` must be executable, idempotent and self-cleaning; `docs/`
  holds deeper material.
- Components with a Dockerfile need a README and tests; images are published
  nested below the main image (`<registry>/<owner>/<repo>/<example>/<component>`).
- Secrets are `*.yaml.example` with placeholders; every example is listed above
  with its literal `examples/<name>/` path.

## Running an example

Each example README has the exact prerequisites, commands, optional hardening,
exposure options and teardown. The multi-tenant example also has a full kind
walkthrough that cleans up after itself:

```bash
examples/dsh-agents-as-a-service/scripts/e2e-kind.sh
```

`kubectl apply -f manifests/` is non-recursive: `optional/` and `exposure/`
subdirectories are applied explicitly.
