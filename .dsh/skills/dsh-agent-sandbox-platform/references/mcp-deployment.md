# Deploying the agent-sandbox MCP server

Read [the MCP reference](mcp-server.md) first. This file covers the connection
matrix, a safe deployment shape, and the DSH profile row. The server is
unauthenticated; its service account is the security boundary.

## Connection matrix

| `K8S_SANDBOX_CONNECTION__TYPE` | Required env | Use when |
| --- | --- | --- |
| `gateway` | `GATEWAY_NAME`, `GATEWAY_NAMESPACE=default`, `GATEWAY_READY_TIMEOUT=180`, `SERVER_PORT=8888` | Gateway API + implementation exist |
| `direct` | `API_URL` (router URL), `SERVER_PORT=8888` | sandbox-router is deployed |
| `in-cluster` | `USE_POD_IP=false` (DNS) or `true` (`status.podIPs`), `SERVER_PORT=8888` | Bypass the router from inside the cluster |
| `sandboxd-in-cluster` (v1.0.8+) | `MODE=in-cluster-service` or `in-cluster-pod-ip`, `REST_PORT=8080`, `GRPC_PORT=9090` | Template runs sandboxd |

Also set `K8S_SANDBOX_PROBE_NAMESPACE` to a namespace the server's service
account can list claims in, and optionally
`K8S_SANDBOX_SESSION_ID_LABEL_KEY` (default
`mcp.k8s-agent-sandbox/session-id`).

For `sandboxd-in-cluster` service mode the template needs `spec.service: true`.
The default managed NetworkPolicy admits only sandbox-router, so add an ingress
rule for the MCP server on 8080/9090. Supplying `spec.networkPolicy` replaces the
secure defaults: also add the egress rules the sandbox needs, because an omitted
egress list denies all outbound traffic. RBAC for that mode is
`sandboxclaims` create/get/list/watch/delete and `sandboxes` get/list/watch; no
`pods/portforward`.

## Build and deploy (sketch)

No official image reference is documented in the upstream README; pin what you
build.

```bash
# AGENT_SANDBOX_VERSION comes from versions.env; never hard-code it.
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
git clone --depth 1 --branch "$AGENT_SANDBOX_VERSION" \
  https://github.com/kubernetes-sigs/agent-sandbox.git "$tmp"
docker build -t "$MCP_IMAGE" "$tmp/clients/integrations/mcp-server"
```

Kubernetes shape:

- Dedicated namespace and ServiceAccount; a namespace-scoped Role granting only
  `sandboxclaims` create/get/list/watch/delete and `sandboxes` get/list/watch.
  No Secrets, no `pods/exec`, no cluster-admin.
- Deployment: one replica, non-root, `readOnlyRootFilesystem`, dropped
  capabilities, `K8S_SANDBOX_CONNECTION__TYPE=in-cluster`,
  `K8S_SANDBOX_PROBE_NAMESPACE`, resource limits.
- Probes: `GET /healthz` (liveness; never calls the API) and `GET /readyz`
  (readiness; lists claims; use `periodSeconds: 15`, not aggressive polling).
- ClusterIP Service on `8000`. Add a NetworkPolicy that allows only the intended
  client namespace/pod. Never expose it through Ingress/LoadBalancer without an
  authenticating proxy. The service account is the real boundary.

## Connect it to DSH

Add a profile MCP row (through the Plugins page/`plugin_manager`; see
`cordis-plugin-development`). A row affects every session and needs approval.

```yaml
- insert:
    - id: mcp-agent-sandbox
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: agent-sandbox
        transport: streamable-http
        url: http://agent-sandbox-mcp.example.svc.cluster.local:8000/mcp
        toolCallTimeoutMs: 240000
        failOnStartupError: false
```

- `serverName` must match `[A-Za-z0-9_-]{1,32}`; tools appear as
  `mcp__<serverName>__<tool>`. Check the actual names in the tool list; do not
  assume hyphens/underscores.
- Raise `toolCallTimeoutMs` above `sandbox_ready_timeout` with headroom; the DSH
  default is 60 s and would cancel a 180 s creation.
- If the server is behind an authenticating proxy, pass its token via the row's
  `headers` and an environment expression; never commit it.
- To reach a locally port-forwarded server, use a loopback URL; the MCP session
  still needs streamable HTTP.
- Remove the row when it was enabled only for one task.

## Deployment safety checklist

- [ ] Build tag equals the cluster's `AGENT_SANDBOX_VERSION`.
- [ ] Dedicated namespace/SA; RBAC limited to claims and sandboxes.
- [ ] Service is ClusterIP; no public route without authentication.
- [ ] NetworkPolicy admits only the DSH client and needed sandbox traffic.
- [ ] `/readyz` succeeds with `K8S_SANDBOX_PROBE_NAMESPACE` set to a listable
      namespace.
- [ ] No Secret data in manifests, logs, or the repository.
- [ ] Cleanup path documented: delete session-owned claims or wait for the
      shutdown timeout.
