# Examples

Every implementation in this repository is a self-contained example: one
directory, plain `kubectl`, an end-to-end README, and a cleanup command.
Examples consume the [main DSH Web image](../docs/main-image.md); they are
reference material, not libraries.

| Example | What it demonstrates | Needs |
| --- | --- | --- |
| [`examples/dsh-agents-as-a-service/`](dsh-agents-as-a-service/) | The full multi-tenant platform pattern: one `SandboxTemplate` with per-user PVCs, a warm pool, one ~10-line `SandboxClaim` per user, suspend-when-idle / wake-on-return, and a browser gateway with signup, `HttpOnly` sessions, an HTTP + WebSocket proxy and two exposure variants (Ingress, or Gateway API + GitHub SSO). | agent-sandbox with extensions; kind for the e2e script. |
| [`examples/minimal-single-tenant/`](minimal-single-tenant/) | The smallest useful setup: a single `SandboxTemplate` and one `SandboxClaim`, no gateway — reach DSH Web with a launch-token URL through `kubectl port-forward`. | agent-sandbox with extensions. |

## The example contract (short version)

Full rules: [docs/examples.md](../docs/examples.md). `scripts/check-examples.sh`
enforces the layout, and `make check` runs it.

```
examples/<name>/
+-- README.md                  # walkthrough: prerequisites, steps, troubleshooting, cleanup
+-- manifests/                 # NN-*.yaml, applied with plain kubectl
|   +-- exposure/<variant>/    # optional per-variant manifests...
|   +-- exposure/README.md     # ...required whenever exposure/ exists
+-- scripts/                   # optional executable, idempotent, self-cleaning *.sh
+-- docs/                      # optional architecture/ADR material
+-- <component>/               # optional component with Dockerfile + README + tests
```

- Secrets never ship populated: use `*.yaml.example` files with placeholders.
- Manifests are numbered (`00-`, `10-`, ...) and read as an ordered walkthrough.
- Component images are published nested below the main image:
  `<registry>/<owner>/<repo>/<example>/<component>`.
- Every example must be listed above with its literal `examples/<name>/` path.
- Scripts under `scripts/` must be executable, idempotent and clean up after
  themselves.

## Running an example

```bash
# e.g. the multi-tenant platform example
kubectl apply -f examples/dsh-agents-as-a-service/manifests/

# or run the whole walkthrough (kind + agent-sandbox required) and let it clean up
examples/dsh-agents-as-a-service/scripts/e2e-kind.sh
```

Each example README has the exact prerequisites, exposure options and teardown.
