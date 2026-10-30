# Troubleshooting

Start with evidence, not fixes: resolve context/namespace, read conditions and
events, then compare desired vs observed state. Keep output bounded. Never
"fix" an access error by broadening credentials.

## Platform

| Symptom | First evidence | Likely cause / fix |
| --- | --- | --- |
| `Forbidden` on every call | `kubectl auth whoami`, `kubectl auth can-i --list -n <ns>` | Wrong context or RBAC. Do not retry with broader credentials; ask for the right kubeconfig. |
| CRDs not found | `kubectl get crd | grep agents.x-k8s.io` | agent-sandbox extensions not installed. Install the pinned release YAML; that is a prerequisite, not a code change. |
| Claim created, no sandbox | `claim.status.conditions[?(@.type=="Ready")]`, events | Empty pool, image pull, quota, unschedulable; or policy rejection. |
| Claim rejected | Ready reason `EnvVarsInjectionRejected` | Claim carries `env`/`volumeClaimTemplates` while the template disallows injection. Remove them; do not bypass the pool. |
| User is suspended but the claim exists | `Sandbox.spec.operatingMode`, `Suspended` condition | Resume with `operatingMode: Running`; a gateway request would wake it. |
| Warm pool exhausted | `kubectl get sandboxes -l agents.x-k8s.io/warm-pool-sandbox` | Scale `spec.replicas` (with confirmation) or accept slow cold provisioning. |
| Pod `Pending` | `kubectl describe pod` | Requests/limits, taints, affinity, unbound PVC, storage class. |
| `ImagePullBackOff` | describe events | Wrong image/tag, private GHCR package, missing pull secret; on kind, load the image under the exact manifest ref. |
| `CrashLoopBackOff`, probe 401 | logs, `describe` | Probe path is `/`; use `/favicon.svg`. |
| Restart with mode-660 error | logs mention `.credentials.yaml` | kubelet fsGroup widened the file; suspend, `chmod 600` from a throwaway pod, patch `fsGroupChangePolicy: OnRootMismatch`, resume. |
| Every bash command fails with `SANDBOX_UNAVAILABLE` | DSH tool result names the requested mode and backends | DSH's own command sandbox has no usable runner: `bubblewrap` ships in the image but is unsetuid and needs an unprivileged user namespace this hardened pod does not grant, and no Landlock runner is available. The example template sets `DSH_PERMISSION_MODE=danger-full-access`; do not grant capabilities to restore it unless the operator explicitly accepts that downgrade. |
| Folder picker opens on `/home/node`, not the workspace | `kubectl exec <pod> -- printenv HOME` | The sandbox predates the template's `HOME=/workspace` and pod env is stamped at creation. Recreate the sandbox (PVC survives) or patch its `podTemplate` with `HOME`, then resume. Workspace dotfiles (`.cache`, `.config`, `.npm`, `.agents`) are the expected trade-off of the override, not a fault. |
| PVC `Pending` | `kubectl describe pvc`, StorageClass | No default provisioner, access mode, zone binding. |
| Launch token not found | `kubectl logs <pod> --tail=2000 | grep 'dsh web:'` | Pod not ready yet, or logs rotated. Do not guess tokens; wait for readiness. |
| Sandbox reports `Finished=True` | pod phase, `kubectl get events` | The backing Pod reached `Failed` (eviction, spot reclaim) and `failedPodPolicy` defaults to `Retain`. Delete the Pod to force a replacement, or set `Recreate` (v1.0.8+) on the Sandbox/template; the claim and PVCs survive either way. |
| Hand-applied sandbox disappeared | claim annotations, gateway logs | Idle sweeper only manages claims with `aaas.example.com/managed-by=dsh-gateway`; if it was managed, recreate/resume. |
| Gateway 401 | gateway logs, cookie flags | Missing/expired cookie, DSH handshake failed, or `COOKIE_SECURE=true` over HTTP. |
| Gateway 502/503 | sandbox Ready, gateway logs | Wake timeout, stale DNS, token not yet in logs; check `WAKE_TIMEOUT` and RBAC `pods/log`. |
| WebSocket closes 1011 | gateway logs, `Host`/Origin | DSH rejected the upstream authority; check `DSH_TRUSTED_HOSTS` and the gateway's pod-IP dial path. |
| WebSocket closes 4401 | cookie, Origin | Missing gateway auth or cross-origin upgrade; send a matching `Origin`. |
| SSO header ignored | gateway env | `AUTH_PROXY_HEADER` unset; setting it requires the proxy to be the only path to the ClusterIP Service. |
| Someone is treated as another user | service paths | Trust-boundary violation: the gateway is directly reachable. Remove the extra path and disable SSO until fixed. |

## Agent-sandbox MCP

| Symptom | First evidence | Likely cause / fix |
| --- | --- | --- |
| No `mcp__*__create_sandbox` tools | tool list, DSH logs | Server not deployed or row not enabled. Use `kubectl`; connect only with approval. |
| 404 on connect | curl `/mcp`, `/healthz` | Wrong path/port; MCP endpoint is `/mcp`, health is at the root. |
| `/readyz` 503 | response body, server logs | API unreachable or namespace `K8S_SANDBOX_PROBE_NAMESPACE` not listable; fix RBAC/config. |
| `ctx.session_id is None` | server error | Connected over stdio; use streamable HTTP. |
| `Sandbox claim ... is not found` | server error, `kubectl get claim` | Wrong namespace, or the claim belongs to another MCP session. List with `kubectl`; use the MCP only for its own claims. |
| Creation cancelled at ~60 s | MCP call log | DSH `toolCallTimeoutMs` is 60 s while creation waits up to 180 s; raise the timeout. |
| `execute_command` connection error | server logs, template ports | No runtime on 8888/8080/9090. The DSH template only listens on 3080; use a runtime template or a combined image. |
| Output truncated / flooded | tool result | Pre-v1.0.8 is unbounded; v1.0.8 bounds output. Redirect verbose output to a file and read/download it; never pass `max_output_chars` to an old server. |
| Orphaned claims after reconnect | `kubectl get claims -l mcp.k8s-agent-sandbox/session-id` | MCP sessions are transient; wait for `shutdown_after_seconds` or delete by label with explicit confirmation. |
| `Forbidden` from the MCP server | server logs | Its ServiceAccount lacks claim/sandbox verbs, or RBAC is in another namespace; fix the Role, not the kubeconfig. |
| Sandbox expired | `get_sandbox_status` -> `SandboxNotFound` | `shutdown_after_seconds` elapsed; create a new sandbox and keep the task inside the budget. |

## Repository checks and CI

| Symptom | Cause / fix |
| --- | --- |
| `check-versions: stale version reference in docs: dsh-v...` | A copied release tag in a Markdown file. Refer to the identity in `versions.env`; run `make set-version` only for a real bump. |
| `check-versions: hard-coded version outside versions.env` | A tag in `.github/workflows` or the Makefile; move it to `versions.env`. |
| `check-examples` layout error | Missing `README.md`/`manifests/`, stray YAML at the example root, missing exposure README, non-executable script, or missing `examples/README.md` entry. |
| Manifest dry-run fails in CI | Invalid YAML or CRD schema; parse locally and run `kubectl apply --dry-run=server` against the pinned CRDs. |
| `shellcheck` failure | Quote expansions, use arrays for kubectl args, avoid unused variables; run shellcheck locally. |
| Lychee link failure | A relative/absolute link does not resolve; the link checker runs offline on the listed docs. |
| PR-title check failure | Use Conventional Commits: `feat(scope): ...`, `fix(scope): ...`, `docs(scope): ...`. |
| Changelog conflict | Never edit `CHANGELOG.md`; it is generated from commits. |

## Do not

- Do not use `--force --grace-period=0` to clear a stuck resource before
  understanding why it is stuck.
- Do not delete namespaces, PVCs, PVs, CRDs, or cluster-scoped objects as a
  cleanup shortcut.
- Do not read Secret data, kubeconfigs, or node logs to "debug" a permission
  problem, and do not paste pod logs containing the DSH launch token into a
  report.
- Do not expose the unauthenticated MCP server or enable `AUTH_PROXY_HEADER`
  while another path to the gateway exists.
- Do not retry a `Forbidden` with a stronger kubeconfig; fix the RBAC scope.
