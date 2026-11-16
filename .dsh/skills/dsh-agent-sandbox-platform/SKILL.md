---
name: dsh-agent-sandbox-platform
description: >-
  Operate and extend this repository's DeepSeek Harness-on-agent-sandbox platform:
  warm pools, claims, sandbox suspend/resume, the gateway, and the optional
  agent-sandbox MCP server, with version-aware safety and a read-only preflight.
whenToUse: >-
  Use when working in deepseek-kubernetes-sandbox or with Kubernetes agent-sandbox:
  inspect or provision sandboxes, debug warm pools or claims, use
  mcp__*__create_sandbox/execute_command, connect the agent-sandbox MCP server, or
  add a manifest, gateway route, exposure variant, example, or version bump here.
metadata:
  author: sredevopsorg
  version: "1.1.0"
---

# DSH agent-sandbox platform

This repository runs DeepSeek Harness Web (DSH) as one sandbox per user on
[agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox), with an
optional official MCP server for sandbox execution. For generic cluster work
load `kubernetes-kubectl-operations`; for profile MCP rows load
`cordis-plugin-development`.

## 0. Facts before action

1. Read `AGENTS.md`, `versions.env`, the example README/architecture. The
   control-plane pin is `AGENT_SANDBOX_VERSION`; release identity is
   `DSH_VERSION` + `RELEASE_REVISION`.
2. Establish context, user and namespace; never assume `dsh-demo`.
3. Run `scripts/platform-preflight.sh --namespace <ns>` (read-only).
4. If `mcp__*__create_sandbox`-style tools are absent, use `kubectl`; do not
   install or enable a server on your own.

## 1. Non-negotiables

- **Read-only by default.** Change a cluster only when the user asked for that
  exact change; show the command and rollback first.
- **Scope every command** with `--context` and `-n`. `--all`/`-A` mutations and
  deleting namespaces, PVCs, PVs, CRDs, or cluster-scoped objects need an
  explicit, named confirmation.
- **Never `kubectl delete -k <root>`** (including an exposure variant): it
  deletes everything the kustomization renders, including the namespace.
  Delete the named objects instead.
- **Secrets and logs stay private.** Never print Secret data, kubeconfigs, or
  node logs. Pod logs can contain the DSH launch token.
- **The MCP server has no authentication.** It is a full-access management
  endpoint; never expose it. A DSH profile row affects every session.
- **Clean up what you create.** Delete MCP-created sandboxes in the same
  session; never leave orphaned claims.
- **Bound output.** Use `--tail`, `--since`, selectors, JSONPath and excerpts
  instead of whole manifests or huge logs.

## 2. Choose the path

| Need | Path |
| --- | --- |
| Inspect or change claims, pools, sandboxes, PVCs, gateway | `kubectl` + [platform runbook](references/platform-runbook.md) |
| Run code or move files in a sandbox | agent-sandbox MCP, only for a runtime-capable template ([MCP reference](references/mcp-server.md)) |
| Add or change a repository feature | [repo extension guide](references/extending-the-repo.md) |

The tools are scoped to claims created by the same MCP session; use `kubectl`
for the platform.

## 3. Operate the platform

Resolve user -> claim -> sandbox -> pod:

```bash
SB=$(kubectl -n "$NS" get sandboxclaim "$CLAIM" -o jsonpath='{.status.sandbox.name}')
kubectl -n "$NS" get sandbox "$SB" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{" "}{.status.conditions[?(@.type=="Ready")].reason}{"\n"}{.status.serviceFQDN}{"\n"}'
POD=$(kubectl -n "$NS" get sandbox "$SB" -o jsonpath='{.metadata.annotations.agents\.x-k8s\.io/pod-name}'); POD=${POD:-$SB}
```

- Read runtime state from the Sandbox, not the claim: `spec.operatingMode`,
  `Suspended`, `Ready`. The claim tracks binding, not liveness. Use `$POD` (not
  `$SB`) for `kubectl exec` after a resume.
- Suspend = patch `operatingMode: Suspended` (pod deleted, PVC + Service kept);
  resume = `Running` + wait for `Ready`. The gateway wakes on connect.
- The idle sweeper only suspends claims the gateway created, and only after a
  DSH activity probe: a running agent keeps the sandbox; a probe error or
  unknown answer defers the suspend; idle sessions plus `IDLE_TIMEOUT` suspend.
- Warm-pool spares carry `agents.x-k8s.io/warm-pool-sandbox`; adopted sandboxes
  lose it. No spares makes signup cold and slow.
- The DSH launch token appears once in pod logs; hand the user the `?token=` URL,
  never the token in reports. Probe `/favicon.svg`.
- Behind TLS, forward `X-Forwarded-Proto: https`; otherwise DSH sees a plain
  scheme and every `/api` call is `403`.
- SSO (`AUTH_PROXY_HEADER`) is honored only for a claim the gateway minted
  itself (the `aaas.example.com/token-sha256` marker); never rotate the marker.
  `scripts/preflight-sso.sh` verifies SSO and `scripts/cluster-drift.sh` reports
  drift (both read-only).

Full commands, security boundaries and failure matrix:
[platform runbook](references/platform-runbook.md).

## 4. Use the agent-sandbox MCP server within its limits

Verified from the upstream server ([reference](references/mcp-server.md)):

- **Session-scoped:** every tool and the resource filter claims by
  `K8S_SANDBOX_SESSION_ID_LABEL_KEY=<MCP session id>`, so it cannot read, exec
  in, or delete DSH claims or claims from a prior session.
- **Streamable HTTP only:** stdio has no MCP session id; a DSH row needs
  `transport: streamable-http` (`/mcp`; health `/healthz`, `/readyz`).
- **Runtime required:** connection types target a sandbox runtime on 8888, or
  sandboxd on 8080/9090; the DSH template listens only on 3080, so
  `execute_command` cannot work against it.
- **Timeouts and output:** creation waits up to 180 s while the DSH default call
  timeout is 60 s (raise `toolCallTimeoutMs`), and pre-v1.0.8 output is
  unbounded. The documented deployment is the `AGENT_SANDBOX_VERSION` pin in
  `versions.env`.

Deployment, connection matrix and DSH row:
[MCP deployment](references/mcp-deployment.md).

## 5. Extend the project

Read `docs/examples.md` and the testing table in `AGENTS.md` before editing.

- **Manifest step:** numbered `manifests/base/NN-*.yaml`, add the file to
  `manifests/base/kustomization.yaml`, update the README step, run `make check`
  and render every touched root with `kubectl kustomize` (CI does not render).
- **Gateway:** edit `gateway/gateway.py` and `gateway/test_gateway.py`, update
  the gateway README API table and architecture doc, run `make test` and
  `make lint`.
- **Exposure variant:** one variant under `manifests/exposure/<variant>/`; the
  overlay already sets the gateway env and `DSH_TRUSTED_HOSTS` — custom values
  go to `manifests/local/patches/`, never a second place. Variants are
  alternatives, never a stack.
- **New example:** follow `docs/examples.md`, add the index entry, keep scripts
  executable/idempotent/self-cleaning, run `make check`.
- **Version bump:** use `make set-version`; for agent-sandbox update
  `AGENT_SANDBOX_MANIFEST_SHA256` together with `AGENT_SANDBOX_VERSION`. Never
  edit pins by hand; run `make check` and the e2e on a cluster. Never mix a
  bump with an unrelated feature.
- **MCP integration:** build at the cluster tag, least-privilege SA, ClusterIP +
  auth proxy, raised DSH timeout, no repo secrets.
- **Optional hardening:** gVisor, `nodeSelector`, NetworkPolicy, gateway egress,
  extra provider keys and failed-Pod recovery stay opt-in; all are documented in
  `examples/dsh-agents-as-a-service/docs/optional-hardening.md`.
- **Docs:** update the affected doc in the same change.

Details and review checklist:
[repo extension guide](references/extending-the-repo.md).

## 6. Report and clean up

State context/namespace, bounded evidence, conclusion, change, rollback and
resources deleted. Disable any MCP row enabled only for the task; leave no
sandboxes, port-forwards or temporary files behind.

## References

- [Platform runbook](references/platform-runbook.md)
- [MCP deployment](references/mcp-deployment.md)
- [MCP server reference](references/mcp-server.md)
- [Repo extension guide](references/extending-the-repo.md)
- [Troubleshooting](references/troubleshooting.md)
