# agent-sandbox MCP server reference

Upstream: [clients/integrations/mcp-server](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/clients/integrations/mcp-server).
It is a Python/FastAPI + FastMCP service (streamable HTTP on `:8000`, MCP
endpoint `/mcp`) that provisions sandboxes from a warm pool and runs commands or
file operations through a sandbox runtime. It ships with **no authentication**.

Build the server from the same tag as the cluster: read `AGENT_SANDBOX_VERSION`
in `versions.env`, and check the installed controller image. Do not build from
`main` against an older cluster; tool schemas and connection types differ.

## Tool surface

| Tool | Arguments (defaults) | Returns / notes |
| --- | --- | --- |
| `create_sandbox` | `warmpool`, `namespace`, `sandbox_ready_timeout=180`, `labels`, `shutdown_after_seconds=300` (`null` disables), `pod_labels`, `pod_annotations` | `sandbox_claim_name`; adds the MCP session label |
| `delete_sandbox` | `sandbox_claim_name`, `namespace` | Deletes the session-owned claim; PVC cascades |
| `execute_command` | `sandbox_claim_name`, `namespace`, `command`, `timeout=60` (max 600), `max_output_chars` only on v1.0.8+ (128-300,000, default 30,000) | `exit_code`, `stdout`, `stderr`; v1.0.8 adds `*_total_chars` and `*_truncated` |
| `upload_file` | `sandbox_claim_name`, `namespace`, `path`, `content`, `binary=false`, `timeout=60` | Bytes written; base64 when `binary=true` |
| `download_file` | `sandbox_claim_name`, `namespace`, `path`, `binary=false`, `timeout=60` | Content + bytes read |
| `list_files` | `sandbox_claim_name`, `namespace`, `path`, `timeout=60`, `max_entries=1000` (max 10,000) | Entries (`name`, `size`, `type`, `mod_time`), `total_entries`, `truncated` |
| `file_exists` | `sandbox_claim_name`, `namespace`, `path`, `timeout=60` | `exists`; a missing path is not an error |
| `get_sandbox_status` | `sandbox_claim_name`, `namespace` | `SandboxReady` / `SandboxNotReady` / `SandboxNotFound`, `ready`, `message` |

Resource: `sandboxes://{namespace}` lists sandboxes for the current MCP session.

## Session scoping is the most important behavior

Every tool, including `get_sandbox_status`, calls `get_sandbox()` ->
`ensure_session_owns()`, which lists sandboxes with the label selector
`<K8S_SANDBOX_SESSION_ID_LABEL_KEY>=<MCP session id>`. The resource uses the same
selector. Consequences:

- The server can only see/exec/delete claims **it created in the same MCP
  session**. It cannot inspect or manage DSH platform claims created by the
  gateway or `kubectl`, or claims from another session.
- On a DSH MCP reconnect, the MCP session changes; earlier claims become
  invisible to the tools. They keep running until `shutdown_after_seconds`
  expires, or until you remove them by label with a confirmed `kubectl delete`.
- A "Sandbox claim '<name>' is not found in namespace '<ns>'" error usually
  means wrong namespace **or** the claim belongs to a different session.
- Use `kubectl` for global inventory; use the MCP only for sandboxes it created
  in this task.

Session identity requires a session-providing transport. With stdio,
`ctx.session_id` is `None` and creation fails. Connect DSH over
`streamable-http`.

## Runtime prerequisite

The server talks to a sandbox runtime, not to arbitrary pods:

- `gateway`, `direct`, `in-cluster`: runtime HTTP on `SERVER_PORT=8888`
  (sandbox-router API or direct pod runtime).
- `sandboxd-in-cluster` (v1.0.8+): sandboxd REST `8080`, gRPC `9090`.

The shipped DSH template runs only DSH Web on `3080`, so no connection type can
exec into it. A sandboxd **sidecar** executes commands in the sidecar's own
container, not in the DSH container; shared volumes share files, not binaries or
processes. To exec inside DSH, build a combined image (sandboxd injected into the
DSH image) or use `kubectl exec` with explicit RBAC and confirmation. For
untrusted code, deploy a separate runtime warm pool rather than modifying the
DSH template.

## Version differences

| Area | Before v1.0.8 | v1.0.8+ |
| --- | --- | --- |
| Connection types | `gateway`, `direct`, `in-cluster` | Adds `sandboxd-in-cluster` |
| `execute_command` output | Unbounded | Bounded by `max_output_chars`; four metadata fields |
| Failed pod recovery | `Retain` only | `spec.failedPodPolicy: Recreate` |
| Router install | `kubectl apply -f sandbox-router/deploy/` | `kubectl apply -k sandbox-router/deploy/`; projected token |

Never pass `max_output_chars` to a pre-v1.0.8 server. On older servers, redirect
verbose output to a file and `download_file` it, or pipe through `tail`/`head`.

## Safety and cleanup

- No auth: anyone reaching `:8000` gets every tool and the service account's
  access. Keep it ClusterIP, authenticated, and namespace-scoped.
- Treat commands as untrusted code: they can read files and environment
  variables inside the sandbox, including provider keys in a combined image.
- `delete_sandbox` cascades the claim, sandbox, and PVC: confirm the target and
  namespace. Never delete a namespace or PVC as cleanup.
- Clean up in the same session that created the sandbox. If the session is gone,
  list claims by the session label and delete them explicitly (requires write
  RBAC); otherwise rely on the shutdown timeout.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Tools missing | Row not enabled, wrong URL, connection failed; check logs and `/readyz` |
| 404 on connect | URL is missing `/mcp` or points at the wrong port |
| `ctx.session_id is None` | Connected over stdio; use streamable HTTP |
| `Sandbox claim ... is not found` | Wrong namespace, or claim created by another MCP session |
| Creation times out at ~60 s | MCP session alive but DSH cancelled the call; raise `toolCallTimeoutMs` |
| `execute_command` connection error | No runtime on 8888/8080/9090, or wrong connection type; the DSH template is unsupported |
| Output flood | Pre-v1.0.8 server; bound the command output yourself |
| Empty sandbox list | The resource is session-scoped; use `kubectl` for global inventory |

Deployment, connection matrix, RBAC, and the DSH row:
[MCP deployment](mcp-deployment.md).
