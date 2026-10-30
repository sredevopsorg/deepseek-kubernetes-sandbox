# Platform runbook

Scope: the resource layer and gateway in
`examples/dsh-agents-as-a-service/`, one namespace, one DSH Web sandbox per
user. Read the example README and `AGENTS.md` first. All commands are read-only
unless a section is explicitly a change; confirm before changing anything.

## 1. Context, version, CRDs

```bash
kubectl config current-context
kubectl auth whoami
kubectl config view --minify -o jsonpath='{..namespace}{"\n"}'
kubectl get crd | grep -E 'agents\.x-k8s\.io'
kubectl -n agent-sandbox-system get deploy agent-sandbox-controller \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

Compare the controller image tag with `AGENT_SANDBOX_VERSION` in
`versions.env`. The example needs the extension CRDs plus core `sandboxes`;
a missing CRD is a prerequisite failure, not an application bug.

## 2. Resource model

| Object | Role | Key fields |
| --- | --- | --- |
| `SandboxTemplate` | Blueprint; pinned image, probes, security context, PVC template | `envVarsInjectionPolicy`, `volumeClaimTemplatesPolicy`, `service`, `podTemplate` |
| `SandboxWarmPool` | Pre-warmed spares from one template | `spec.replicas`, `sandboxTemplateRef` |
| `SandboxClaim` | One tenant/user request; adopts a spare | `spec.warmPoolRef`, `additionalPodMetadata`, `status.sandbox.name` |
| `Sandbox` | Adopted or directly created pod + Service + PVC | `spec.operatingMode`, `status.conditions`, `status.serviceFQDN`, `status.podIPs` |
| `Pod` | The DSH Web process | `:3080`, PVC at `/home/node/.dsh` and `/workspace` (`subPath: workspace`) |
| `PersistentVolumeClaim` | Per-sandbox DSH state + agent workspace | `dsh-home-<sandbox>` |
| `Service` | Stable per-sandbox DNS | `<sandbox>`, `status.serviceFQDN` |

Delete a claim to cascade-delete its sandbox and PVC (account deletion). Never
delete a PVC or namespace without an explicit, named confirmation.

## 3. Read-only inspection

```bash
kubectl -n "$NS" get sandboxtemplates,sandboxwarmpools,sandboxclaims,sandboxes -o wide
kubectl -n "$NS" get pods,pvc,svc -o wide
kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -30
kubectl -n "$NS" get sandboxclaim "$CLAIM" -o yaml
kubectl -n "$NS" get sandbox "$SB" -o yaml
```

Bounded logs: `kubectl -n "$NS" logs "$POD" --tail=200` (add `--previous`
only after a restart).

## 4. Claim -> sandbox -> pod

```bash
SB=$(kubectl -n "$NS" get sandboxclaim "$CLAIM" -o jsonpath='{.status.sandbox.name}')
POD=$(kubectl -n "$NS" get sandbox "$SB" -o jsonpath='{.metadata.annotations.agents\.x-k8s\.io/pod-name}')
POD=${POD:-$SB}
kubectl -n "$NS" get sandbox "$SB" -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'
kubectl -n "$NS" get sandbox "$SB" -o jsonpath='{.spec.operatingMode}{" "}{.status.serviceFQDN}{" "}{.status.podIPs}{"\n"}'
```

Rules:

- Resolve user -> sandbox only through `claim.status.sandbox.name`; the sandbox
  keeps its pool-generated name, not the claim name.
- A claim tracks binding; for runtime state read `Sandbox.spec.operatingMode`
  and the `Suspended`/`Ready` conditions. A suspended sandbox is not serving
  even when the claim still exists.
- A missing `claim.status.sandbox.name` means the claim did not bind: inspect
  the Ready condition `reason` and events. The pod is normally named after the
  sandbox; use the annotation when present.

## 5. Suspend, resume, wake

Suspend keeps the PVC and Service, deletes the pod:

```bash
kubectl -n "$NS" patch sandbox "$SB" --type merge \
  -p '{"spec":{"operatingMode":"Suspended"}}'
kubectl -n "$NS" wait --for=delete "pod/$POD" --timeout=120s
kubectl -n "$NS" get pvc "dsh-home-$SB"
kubectl -n "$NS" get svc "$SB"
```

Resume and verify state:

```bash
kubectl -n "$NS" patch sandbox "$SB" --type merge \
  -p '{"spec":{"operatingMode":"Running"}}'
kubectl -n "$NS" wait sandbox "$SB" --for=condition=Ready --timeout=180s
kubectl -n "$NS" exec "$SB" -- cat /home/node/.dsh/marker.txt
```

The gateway wakes a suspended sandbox on the first request. The idle sweeper
only suspends claims it created (`aaas.example.com/managed-by=dsh-gateway`);
hand-applied claims are never swept.

## 6. Warm pool

```bash
kubectl -n "$NS" get sandboxwarmpool "$POOL" -o yaml
kubectl -n "$NS" get sandboxes -l agents.x-k8s.io/warm-pool-sandbox -o wide
kubectl -n "$NS" get sandboxwarmpool "$POOL" -o jsonpath='{.spec.replicas}{"\n"}'
```

Adopted sandboxes lose the warm-pool label; count current spares against
`spec.replicas`. Zero spares or `spec.replicas: 0` means cold, slow signup.
Scale only when asked:

```bash
kubectl -n "$NS" patch sandboxwarmpool "$POOL" --type merge -p '{"spec":{"replicas":2}}'
```

## 7. DSH runtime specifics

- DSH Web listens on `3080` in-cluster. Port-forward to a free local port such
  as `18081`, never `3080` if a local DSH already uses it.
- The launch token is printed once as `dsh web: http://<ip>:3080/?token=...`.
  Treat it as a credential: do not paste it into reports; hand the user the URL.
- `/` returns `401` without the token. Probes must target `/favicon.svg`.
- `DSH_TRUSTED_HOSTS` must include any public host that a proxy forwards; DSH
  fences on `Host`/`Origin`.
- State lives at `/home/node/.dsh`. DSH requires `.credentials.yaml` to be
  `0600`; the template uses `fsGroupChangePolicy: OnRootMismatch`. If a resumed
  sandbox crash-loops with a mode-660 error, suspend it, fix the file with a
  throwaway pod, patch the policy, then resume.
- Agent files live at `/workspace`, the same PVC mounted with
  `subPath: workspace`; it survives suspend/resume and dies with the PVC.
- DSH confines bash with its own file sandbox (`bubblewrap`, else Landlock) and
  fails closed with `SANDBOX_UNAVAILABLE` when neither works. That is the norm in
  this hardened pod (all capabilities dropped), which is why the template sets
  `DSH_PERMISSION_MODE=danger-full-access` and treats the pod as the boundary.
  Never "fix" it by granting `CAP_SYS_ADMIN` or `seccompProfile: Unconfined`
  without an explicit decision.

## 8. Gateway layer

```bash
kubectl -n "$NS" get deploy,svc,pods -l app=dsh-gateway -o wide
kubectl -n "$NS" get endpoints dsh-gateway
kubectl -n "$NS" logs deploy/dsh-gateway --tail=100
```

The gateway is a ClusterIP Service on `8080`; it proxies under `/u/{user}/` and
derives state (`Provisioning`, `Waking`, `Ready`, `Suspending`, `Suspended`).
It performs the DSH launch-token handshake server-side. With SSO enabled
(`AUTH_PROXY_HEADER`), the Service must be reachable only through the
authenticating proxy; never add a second path or port-forward it on a shared
host. A resource-only install has no gateway: that is a warning, not a failure.

## 9. Security boundaries

- Provider keys (`dsh-provider-keys` plus the optional
  `dsh-provider-keys-extra`) are shared by all sandboxes; the example is not
  tenant-isolated.
- gVisor, `nodeSelector`, the sandbox/exposure NetworkPolicies and extra
  provider keys are opt-in, not active by default. A NetworkPolicy only helps
  with a CNI that enforces it; see
  [optional hardening](../../../../examples/dsh-agents-as-a-service/docs/optional-hardening.md).
- Secret data, kubeconfigs, and pod logs are sensitive; RBAC may allow Secret
  access, policy does not. Keep gateway RBAC namespace-scoped; it needs pod logs
  to find the launch token.
- TLS terminates outside the gateway; set `COOKIE_SECURE=true` for HTTPS.

## 10. Failure matrix

| Symptom | First evidence | Likely cause / fix |
| --- | --- | --- |
| Claim not Ready, no sandbox | Ready condition reason, events | Empty pool, image pull, quota, unschedulable |
| `EnvVarsInjectionRejected` | claim Ready reason | Claim carries env/volume templates; template policy rejects it |
| Pod Pending | `kubectl describe pod` | Requests/taints/affinity, unbound PVC |
| CrashLoopBackOff | logs, `--previous` | Startup failure, bad config, probe 401, fsGroup mode 660 |
| ImagePullBackOff | describe events | Wrong tag, private registry, missing pull secret |
| DSH 401 in browser | gateway logs | Launch-token handshake failed; check gateway RBAC `pods/log` |
| 502/503 through gateway | sandbox Ready, gateway logs | Wake timeout, DNS stale, token not in logs yet |
| Warm pool empty | sandboxes label count | Scale the pool or wait for replenishment |
| Suspended but expected running | `operatingMode` | Resume it; the gateway only wakes on a request |
| WS closes `1011` | gateway logs | DSH rejected the upstream Host/authority; check trusted hosts |

## 11. Cleanup and reporting

Delete only claims/sandboxes you created, by name:

```bash
kubectl -n "$NS" delete sandboxclaim "$CLAIM" --dry-run=server
kubectl -n "$NS" delete sandboxclaim "$CLAIM"
```

Report context/namespace, evidence, conclusion, change, rollback, and what was
deleted. Leave the namespace, pools, templates, and unrelated claims alone.
