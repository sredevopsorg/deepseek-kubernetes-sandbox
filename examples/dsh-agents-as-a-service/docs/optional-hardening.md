# Optional hardening: gVisor, nodeSelector, NetworkPolicy, provider keys and failed-Pod recovery

The base example is deliberately portable: it runs on kind and GKE without a
gVisor runtime, a tainted node pool or a NetworkPolicy-capable CNI, and it
boots with only the required `dsh-provider-keys` Secret. This document covers
four **opt-in** controls plus failed-Pod recovery:

| Setting | Where it lives | What it changes |
| --- | --- | --- |
| gVisor | commented `runtimeClassName` in the SandboxTemplates and Deployments | puts DSH and/or the gateway in a userspace kernel |
| nodeSelector | commented `nodeSelector` in the same files | pins pods to a chosen node pool |
| NetworkPolicy | `manifests/optional/network-policy/`, `manifests/exposure/*/` | limits sandbox, gateway and proxy connectivity |
| Provider keys | optional `dsh-provider-keys-extra` Secret | injects extra provider API keys into every sandbox |
| Failed-Pod recovery | commented `failedPodPolicy` in the SandboxTemplates | replaces a Failed sandbox Pod instead of parking the sandbox |

Each setting is independent. Enable only what the cluster supports and test the
walkthrough after each change.

## 1. gVisor

### What it changes

The DSH sandbox and the gateway (and optionally oauth2-proxy) run under the
`gvisor` RuntimeClass. `agent-sandbox` passes `runtimeClassName` straight to the
PodSpec; no CRD change is involved.

### Prerequisites

- A `RuntimeClass` named `gvisor`:

  ```bash
  kubectl get runtimeclass gvisor -o yaml
  ```

- Nodes capable of running it (`runsc` installed and registered with the
  container runtime). How you get there is cluster-specific:
  - GKE Sandbox: create a node pool with `--sandbox=type=gvisor`; GKE labels
    the nodes `sandbox.gke.io/runtime: gvisor`, taints them
    `sandbox.gke.io/runtime=gvisor:NoSchedule`, and its `gvisor` RuntimeClass
    applies the matching affinity and toleration automatically.
  - Self-managed: install gVisor, create the `gvisor` RuntimeClass with
    `handler: runsc`, and label/taint a dedicated node pool yourself.
- A scheduling plan: a gVisor RuntimeClass without capable nodes makes pods
  fail to start. Use the RuntimeClass `scheduling` fields or the optional
  `nodeSelector`/toleration described in section 2.

### Enable it

**Fresh install:** uncomment `runtimeClassName: gvisor` before applying:

- [`manifests/10-sandboxtemplate.yaml`](../manifests/10-sandboxtemplate.yaml)
- [`manifests/50-gateway.yaml`](../manifests/50-gateway.yaml)
- [`manifests/exposure/gateway-api-sso/72-oauth2-proxy.yaml`](../manifests/exposure/gateway-api-sso/72-oauth2-proxy.yaml)
  (only with the SSO variant)
- [minimal template](../../minimal-single-tenant/manifests/10-sandboxtemplate.yaml)

**Existing install:** a template edit does not update existing Sandboxes, and
warm-pool spares use `OnReplenish`, so patch explicitly. Plan a maintenance
window: claimed users are disconnected when their pod is recreated.

```bash
# 1. The blueprint for future sandboxes.
kubectl -n dsh-demo patch sandboxtemplate dsh-web --type merge \
  -p '{"spec":{"podTemplate":{"spec":{"runtimeClassName":"gvisor"}}}}'

# 2. Warm-pool spares: delete them; the pool replenishes from the template.
kubectl -n dsh-demo delete sandboxes -l agents.x-k8s.io/warm-pool-sandbox

# 3. Claimed sandboxes: snapshot their pod template, then suspend/resume.
for sb in $(kubectl -n dsh-demo get sandboxes \
  -l '!agents.x-k8s.io/warm-pool-sandbox' -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n dsh-demo patch sandbox "$sb" --type merge \
    -p '{"spec":{"podTemplate":{"spec":{"runtimeClassName":"gvisor"}}}}'
  kubectl -n dsh-demo patch sandbox "$sb" --type merge \
    -p '{"spec":{"operatingMode":"Suspended"}}'
  kubectl -n dsh-demo patch sandbox "$sb" --type merge \
    -p '{"spec":{"operatingMode":"Running"}}'
done

# 4. The gateway (and oauth2-proxy when the SSO variant is installed).
kubectl -n dsh-demo patch deploy dsh-gateway --type merge \
  -p '{"spec":{"template":{"spec":{"runtimeClassName":"gvisor"}}}}'
kubectl -n dsh-demo rollout status deploy/dsh-gateway --timeout=180s
# kubectl -n dsh-demo patch deploy oauth2-proxy --type merge \
#   -p '{"spec":{"template":{"spec":{"runtimeClassName":"gvisor"}}}}'
```

If the pool's nodes are tainted and the RuntimeClass does not already add a
toleration, add the matching `tolerations` block (section 2) in the same patch
or in the manifests.

### Verify

```bash
SB=$(kubectl -n dsh-demo get sandboxclaim dsh-alice -o jsonpath='{.status.sandbox.name}')
POD=$(kubectl -n dsh-demo get sandbox "$SB" \
  -o jsonpath='{.metadata.annotations.agents\.x-k8s\.io/pod-name}'); POD=${POD:-$SB}

kubectl -n dsh-demo get pod "$POD" -o jsonpath='{.spec.runtimeClassName}{"\n"}'
# gvisor
kubectl -n dsh-demo exec "$POD" -- uname -r
kubectl -n dsh-demo get node "$(kubectl -n dsh-demo get pod "$POD" -o jsonpath='{.spec.nodeName}')" \
  -o jsonpath='{.status.nodeInfo.kernelVersion}{"\n"}'
```

The pod's `runtimeClassName` is authoritative. The kernel release reported by
`uname -r` inside a gVisor pod is the sandbox kernel, not the host kernel; on
GKE it looks like `4.19.0-gvisor`.

### Caveats

- **`kubectl port-forward` does not work with gVisor.** The multi-tenant
  gateway path (HTTP/WebSocket to the pod) works; the minimal example's
  port-forward quickstart does not. Use the gateway example or a reachable
  Service (NodePort/LoadBalancer/Sandbox Router) instead.
- gVisor has performance overhead and does not implement every syscall. Test
  DSH and the agent tools you rely on (`git`, `gh`, `uv`, language toolchains).
- Node scheduling is the hard part. A `gvisor` RuntimeClass on a cluster with
  no capable nodes leaves pods unschedulable or failing at container start.
- Existing Sandboxes are snapshots. The loop above is required for every
  claimed sandbox; deleting the `SandboxClaim` would also delete its PVC and
  workspace data, so do not use claim re-creation as a shortcut.
- A resumed sandbox starts cold; the startup probe gives it up to 120s.

### Roll back

```bash
kubectl -n dsh-demo patch sandboxtemplate dsh-web --type merge \
  -p '{"spec":{"podTemplate":{"spec":{"runtimeClassName":null}}}}'
for sb in $(kubectl -n dsh-demo get sandboxes -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n dsh-demo patch sandbox "$sb" --type merge \
    -p '{"spec":{"podTemplate":{"spec":{"runtimeClassName":null}}}}'
done
kubectl -n dsh-demo delete sandboxes -l agents.x-k8s.io/warm-pool-sandbox
kubectl -n dsh-demo patch deploy dsh-gateway --type merge \
  -p '{"spec":{"template":{"spec":{"runtimeClassName":null}}}}'
# kubectl -n dsh-demo patch deploy oauth2-proxy --type merge \
#   -p '{"spec":{"template":{"spec":{"runtimeClassName":null}}}}'
```

Recreate the pods (delete spares, suspend/resume claimed sandboxes, let the
Deployment roll out) so the cleared field takes effect.

## 2. nodeSelector

### What it changes

Pods get a `nodeSelector` so they land only on nodes carrying the chosen label.
The same field is available in both SandboxTemplates and both Deployments;
the shipped value is **commented out** because active cluster-specific
scheduling does not belong in the repository.

### Choose a label

```bash
kubectl get nodes --show-labels
```

- **GKE Sandbox:** the nodes carry `sandbox.gke.io/runtime: gvisor` and are
  tainted `sandbox.gke.io/runtime=gvisor:NoSchedule`. With
  `runtimeClassName: gvisor`, GKE already schedules the pod onto those nodes;
  add a `nodeSelector` only when you need a specific sandbox pool or
  ComputeClass (`cloud.google.com/compute-class: <name>`).
- **Self-managed:** label the pool you want, for example
  `dsh.example.com/node-pool: gvisor`, and use that label.
- **General pinning:** any stable node-pool label works. Keep the real key and
  value in a local patch; the manifests use `dsh.example.com/node-pool` as an
  obvious placeholder.

### Enable it

Uncomment and replace the placeholder before a fresh apply:

```yaml
      nodeSelector:
        dsh.example.com/node-pool: gvisor
```

For an existing install, patch the template, each claimed Sandbox and the
Deployments, then recreate pods exactly as in section 1:

```bash
kubectl -n dsh-demo patch sandboxtemplate dsh-web --type merge \
  -p '{"spec":{"podTemplate":{"spec":{"nodeSelector":{"dsh.example.com/node-pool":"gvisor"}}}}}'
# ... same warm-pool delete / claimed-sandbox suspend-resume / Deployment
# patch and rollout as in section 1, with nodeSelector instead of
# runtimeClassName.
```

If the selected nodes are tainted, add a matching toleration. This is the
companion block; do **not** uncomment it unless the pool is actually tainted:

```yaml
      tolerations:
        - key: dsh.example.com/node-pool
          operator: Equal
          value: gvisor
          effect: NoSchedule
```

GKE Sandbox's taint is `sandbox.gke.io/runtime=gvisor:NoSchedule`; the GKE
`gvisor` RuntimeClass adds that toleration automatically, so a manual copy is
only needed when you schedule without the RuntimeClass or with a different
pool.

### Verify and troubleshoot

```bash
kubectl -n dsh-demo get pod "$POD" -o wide
kubectl -n dsh-demo get pod "$POD" -o jsonpath='{.spec.nodeSelector}{"\n"}'
kubectl -n dsh-demo describe pod "$POD" | sed -n '/Events:/,$p'
```

- `Pending` with `0/N nodes are available`: the label is wrong, the pool is
  full, or the pod needs a toleration for a taint.
- A pod-level `nodeSelector` is merged with the RuntimeClass
  `scheduling.nodeSelector`. If both are set and conflict, no node matches.
- Labels changed after scheduling do not move running pods. Patch/recreate.
- A `nodeSelector` that excludes every gVisor node is a valid way to keep the
  gateway on a normal pool while the sandboxes run under gVisor.

### Roll back

Remove the field from the manifests, or patch it to `null`, then recreate the
pods:

```bash
kubectl -n dsh-demo patch sandboxtemplate dsh-web --type merge \
  -p '{"spec":{"podTemplate":{"spec":{"nodeSelector":null}}}}'
# ... same per-Sandbox and Deployment loop as section 1.
```

## 3. NetworkPolicy

The base example does **not** manage NetworkPolicy: the SandboxTemplate is
`networkPolicyManagement: Unmanaged`, and the gateway has no policy. That keeps
the walkthrough portable and independent of the sandbox-router; the opt-in
policies below are standard Kubernetes `NetworkPolicy` objects.

### Optional sandbox policy

[`manifests/optional/network-policy/10-sandbox-networkpolicy.yaml`](../manifests/optional/network-policy/10-sandbox-networkpolicy.yaml)
selects every Sandbox pod through the agent-sandbox tracking label
`agents.x-k8s.io/sandbox-name-hash`. It applies to existing warm-pool and
claimed sandboxes without recreating them.

```bash
kubectl apply -f manifests/optional/network-policy/
kubectl delete -f manifests/optional/network-policy/   # rollback
```

What it allows:

| Direction | Allowed | Blocked |
| --- | --- | --- |
| Ingress | gateway pods (`app: dsh-gateway`) on TCP `3080` | everything else, including other sandboxes and cluster Services |
| Egress | UDP/TCP `53` to kube-dns and NodeLocal DNSCache; TCP `443` to public IPs | RFC1918, CGNAT, link-local/metadata, cluster Services/API, plain HTTP, other ports |

Verify from inside the pods (NetworkPolicy does not affect `kubectl exec`
itself, only the traffic the commands generate):

```bash
SB=$(kubectl -n dsh-demo get sandboxclaim dsh-alice -o jsonpath='{.status.sandbox.name}')
POD=$(kubectl -n dsh-demo get sandbox "$SB" \
  -o jsonpath='{.metadata.annotations.agents\.x-k8s\.io/pod-name}'); POD=${POD:-$SB}

# Allowed: gateway -> sandbox.
kubectl -n dsh-demo exec deploy/dsh-gateway -- python -c \
  "import urllib.request; print(urllib.request.urlopen('http://$POD:3080/favicon.svg', timeout=5).status)"

# Allowed: DNS and public HTTPS from the sandbox.
kubectl -n dsh-demo exec "$POD" -- getent hosts api.deepseek.com
kubectl -n dsh-demo exec "$POD" -- curl -fsS -o /dev/null -w '%{http_code}\n' https://example.com

# Denied: another sandbox cannot reach this one on 3080 (expect a timeout).
OTHER_SB=$(kubectl -n dsh-demo get sandboxclaim dsh-bob -o jsonpath='{.status.sandbox.name}')
OTHER_POD=$(kubectl -n dsh-demo get sandbox "$OTHER_SB" \
  -o jsonpath='{.metadata.annotations.agents\.x-k8s\.io/pod-name}'); OTHER_POD=${OTHER_POD:-$OTHER_SB}
kubectl -n dsh-demo exec "$OTHER_POD" -- curl --max-time 3 -sS "http://$POD:3080/favicon.svg"
```

Caveats:

- **The CNI must enforce NetworkPolicy.** If it does not, the file applies but
  has no effect.
- DNS: the default assumes CoreDNS/kube-dns pods labelled `k8s-app: kube-dns`
  and covers NodeLocal DNSCache at `169.254.20.10`. A different DNS layout
  needs a selector/`ipBlock` edit.
- **Kubelet probes** from the node are CNI-dependent. If a pod starts failing
  its probes, allow the node CIDR on the probe port or remove the ingress
  policy.
- The policy blocks cluster Services and the Kubernetes API from the sandbox.
  DSH does not need them, but an agent tool that does needs an explicit rule.
- It allows any public host on TCP `443`, not an FQDN allowlist. It is network
  isolation, not data-loss prevention.
- Custom provider base URLs, an HTTP proxy, port `80`, SSH `git` (port `22`) or
  a non-443 provider endpoint need explicit egress additions.
- Do not combine this standalone policy with `networkPolicyManagement: Managed`
  unless you migrate the rules into `spec.networkPolicy`. NetworkPolicies are
  additive; the controller's Managed secure default would also be present.
- `kubectl port-forward` still works for debugging (the connection is local to
  the pod), except under gVisor.

### Exposure-variant policies

Each exposure variant ships its own policy. They are part of the variant, not a
separate opt-in step:

- [`manifests/exposure/ingress/61-networkpolicy.yaml`](../manifests/exposure/ingress/61-networkpolicy.yaml)
  allows only Traefik pods (`app.kubernetes.io/name: traefik`) to reach the
  gateway on `8080`.
- [`manifests/exposure/gateway-api-sso/75-networkpolicies.yaml`](../manifests/exposure/gateway-api-sso/75-networkpolicies.yaml)
  allows only oauth2-proxy to reach the gateway, only the Gateway
  implementation (Traefik) to reach oauth2-proxy, and limits oauth2-proxy
  egress to DNS, public HTTPS (GitHub) and the gateway.

The SSO policy is what turns the trust boundary in the
[exposure README](../manifests/exposure/README.md) into network enforcement.
The `dsh-gateway` Service must still stay ClusterIP; do not apply both exposure
variants.

Verify the negative case from a sandbox pod (it is not the proxy):

```bash
kubectl -n dsh-demo exec "$POD" -- curl --max-time 3 -sS http://dsh-gateway:8080/healthz
# expected: timeout/connection failure
```

Then verify the real browser path through the Ingress or Gateway, and confirm
the gateway/proxy pods are `Ready`. If Traefik uses `hostNetwork` or a
different pod label, replace the `podSelector` with the correct selector or an
`ipBlock`. As with the sandbox policy, kubelet probes can be affected on some
CNIs.

### Gateway egress (documented, not shipped)

The gateway needs DNS, the Kubernetes API, and TCP `3080` to sandbox pods. The
API server endpoint is cluster-specific (Service CIDR or node IP/port), so an
egress policy is **not** shipped. If your cluster has a stable API-server CIDR
and port, add an egress policy like this (replace the placeholders; verify the
port, commonly `443` or `6443`):

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: dsh-gateway-egress
  namespace: dsh-demo
spec:
  podSelector:
    matchLabels:
      app: dsh-gateway
  policyTypes:
    - Egress
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    - to:
        - ipBlock:
            cidr: 10.96.0.1/32   # placeholder: API-server Service/cluster endpoint
      ports:
        - protocol: TCP
          port: 443
    - to:
        - podSelector:
            matchExpressions:
              - key: agents.x-k8s.io/sandbox-name-hash
                operator: Exists
      ports:
        - protocol: TCP
          port: 3080
```

Cilium users can express the API server as `toEntities: kube-apiserver` (or
`toServices`) instead of a CIDR. Do not apply the sample blindly: a wrong CIDR
or port silently breaks signup/proxy.

## 4. Optional provider keys

### How injection works

The SandboxTemplate injects two Secrets via `envFrom`:

- `dsh-provider-keys` — required by the walkthrough; holds
  `DEEPSEEK_API_KEY`.
- `dsh-provider-keys-extra` — optional; referenced with `optional: true`, so a
  missing Secret does not block sandbox startup. Every key in it becomes an
  environment variable in every sandbox.

Create the extra Secret before the warm pool (pod env is stamped at creation):

```bash
kubectl -n dsh-demo create secret generic dsh-provider-keys-extra \
  --from-literal=OPENROUTER_API_KEY="sk-or-..." \
  --from-literal=GEMINI_API_KEY="..." \
  --from-literal=NVIDIA_API_KEY="nvapi-..." \
  --dry-run=client -o yaml | kubectl apply -f -
```

Use the exact env-var names (uppercase, underscores) — invalid Secret keys are
skipped by the kubelet with an event. Do not duplicate a key across the two
Secrets; when the same key exists twice, the later `envFrom` entry wins.

### Supported names (curated)

These are the names the pinned DSH release resolves through its packaged provider
catalog; the catalog has more entries, these are the common ones:

| Provider | Environment variable |
| --- | --- |
| DeepSeek | `DEEPSEEK_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| Google Gemini | `GEMINI_API_KEY` |
| NVIDIA | `NVIDIA_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| Groq | `GROQ_API_KEY` |
| xAI | `XAI_API_KEY` |
| Mistral | `MISTRAL_API_KEY` |
| Together | `TOGETHER_API_KEY` |
| Cerebras | `CEREBRAS_API_KEY` |
| Vercel AI Gateway | `AI_GATEWAY_API_KEY` |
| Hugging Face | `HF_TOKEN` |

Aliases to avoid: `GOOGLE_API_KEY`, `HUGGINGFACE_API_KEY` and
`VERCEL_AI_GATEWAY_API_KEY`. The packaged catalog looks for `GEMINI_API_KEY`,
`HF_TOKEN` and `AI_GATEWAY_API_KEY` instead.

### Picking up a key change

A Secret update does not change running pods. The base key and extra keys are
both snapshotted at container start:

```bash
# Warm-pool spares: delete them so the pool recreates them from the template.
kubectl -n dsh-demo delete sandboxes -l agents.x-k8s.io/warm-pool-sandbox

# Running claimed sandboxes: delete the pod (the Sandbox controller recreates
# it) or suspend/resume the sandbox.
POD=...   # resolve it as in section 3
kubectl -n dsh-demo delete pod "$POD"
```

New claims get the new values automatically. Confirm names without printing
secret values:

```bash
kubectl -n dsh-demo exec "$POD" -- sh -c '
  for k in DEEPSEEK_API_KEY OPENROUTER_API_KEY GEMINI_API_KEY NVIDIA_API_KEY; do
    if [ -n "$(printenv "$k" || true)" ]; then echo "$k=set"; else echo "$k=unset"; fi
  done'
```

### Precedence and shared-key limits

- DSH's stored credential file (`$DSH_HOME/.credentials.yaml`, mode `0600`)
  **wins over the environment**. If a tenant saved a key through the DSH UI,
  changing the Secret will not override it.
- Both Secrets are shared by **every** tenant, and any agent can read its own
  environment. This is a warm-pool constraint, not tenant isolation. A tenant
  can override its own key through the DSH credential store on its PVC, but the
  platform does not manage that; real multi-tenancy needs per-tenant
  credentials or an egress proxy.
- The optional Secret is created imperatively; the repository never contains a
  real key. Keep it out of Git and out of shell history where possible.

## 5. Failed-Pod recovery

### What it changes

When a sandbox Pod reaches phase `Failed`, the controller's default policy,
`Retain`, leaves the Pod in place and surfaces `Finished=True` on the Sandbox.
The Sandbox, its claim and its PVC survive, but nothing runs: the gateway keeps
proxying to a dead Pod and every request fails until someone deletes the Pod or
the claim.

agent-sandbox v1.0.8 (the pin in [versions.env](../../../versions.env)) adds
`spec.failedPodPolicy` to `Sandbox` and to `SandboxTemplate`:

| Value | Behavior |
| --- | --- |
| `Retain` (default) | The Failed Pod stays and the Sandbox reports `Finished=True`; the evidence stays available for debugging. |
| `Recreate` | The controller deletes the Failed Pod and starts a replacement; the Sandbox identity and its Sandbox-owned PVCs are retained. |

Repeated failures back off like a Deployment - 5 s, doubling to 5 min between
creations - so a crash loop cannot hammer the API server. The backoff resets
once the replacement Pod has been continuously `Running` for 10 minutes, and it
lives in memory only, so a controller restart clears it.

The base templates ship it commented out, for two reasons:

- A deterministic failure (bad image, bad Secret, a wrong probe) would recreate
  forever while hiding the cause.
- The templates use `restartPolicy: OnFailure`, so the kubelet restarts a
  crashing container in place and the Pod does not reach phase `Failed` anyway.
  This policy matters for terminal failures - most commonly node eviction
  (`Evicted`), when a spot/preemptible node is reclaimed or the node hits memory
  pressure.

No gateway change is needed: the handshake cache is keyed by Pod UID, so a
replacement Pod re-handshakes on the next request, and the existing `401` retry
covers containers restarted in place.

### Enable it

```bash
# 1. The blueprint for future sandboxes.
kubectl -n dsh-demo patch sandboxtemplate dsh-web --type merge \
  -p '{"spec":{"failedPodPolicy":"Recreate"}}'

# 2. Warm-pool spares were stamped at creation: delete them and let the pool
#    replenish from the updated template.
kubectl -n dsh-demo delete sandboxes -l agents.x-k8s.io/warm-pool-sandbox

# 3. Claimed sandboxes keep their own spec: patch them in place.
for sb in $(kubectl -n dsh-demo get sandboxes \
  -l '!agents.x-k8s.io/warm-pool-sandbox' -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n dsh-demo patch sandbox "$sb" --type merge \
    -p '{"spec":{"failedPodPolicy":"Recreate"}}'
done
```

For a fresh install, uncomment `failedPodPolicy: Recreate` in the template
instead.

### Verify

```bash
SB=$(kubectl -n dsh-demo get sandboxclaim dsh-alice -o jsonpath='{.status.sandbox.name}')
kubectl -n dsh-demo get sandbox "$SB" -o jsonpath='{.spec.failedPodPolicy}{"\n"}'
# Recreate
```

A failure drill that never touches a user sandbox - a `Sandbox` with
`restartPolicy: Never` and a container that exits non-zero (swap the image if
Docker Hub is unreachable):

```bash
kubectl -n dsh-demo apply -f - <<'EOF'
apiVersion: agents.x-k8s.io/v1beta1
kind: Sandbox
metadata:
  name: failed-pod-drill
spec:
  failedPodPolicy: Recreate
  podTemplate:
    spec:
      restartPolicy: Never
      containers:
        - name: drill
          image: busybox:1.36
          command: ["sh", "-c", "exit 1"]
EOF

kubectl -n dsh-demo get pods -w
# The drill Pod reaches phase Failed; a replacement appears within ~5 s, then
# 10 s, 20 s, ... as the container keeps exiting non-zero.
kubectl -n dsh-demo delete sandbox failed-pod-drill
```

Repeat the drill with `failedPodPolicy: Retain` (or omit the field) to see the
default: the Pod stays `Failed` and the Sandbox condition `Finished` becomes
`True`.

### Caveats

- `Recreate` deletes the Failed Pod, so its logs go with it. Capture
  `kubectl get events` or switch back to `Retain` when debugging.
- The replacement is a cold start: DSH boots again and issues a new launch
  token. The workspace comes back from the PVC; in-flight sessions do not.
- The field needs agent-sandbox v1.0.8+. A v1.0.7 cluster rejects it as an
  unknown field.
- It applies to sandbox Pods only. The gateway and oauth2-proxy are plain
  Deployments, and their ReplicaSets already replace Failed Pods.

### Roll back

```bash
kubectl -n dsh-demo patch sandboxtemplate dsh-web --type merge \
  -p '{"spec":{"failedPodPolicy":"Retain"}}'
for sb in $(kubectl -n dsh-demo get sandboxes -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n dsh-demo patch sandbox "$sb" --type merge \
    -p '{"spec":{"failedPodPolicy":"Retain"}}'
done
```

Warm-pool spares pick the default up as they are claimed and replenished, or
delete them as in step 2 above.

## 6. Verification summary

Required locally for any change to this example:

```bash
make check          # versions, example layout, manifest YAML shape
yamllint .
make test           # only when the gateway code changed
```

CI additionally parses every manifest and runs `kubectl apply
--dry-run=server` against the real agent-sandbox CRDs. The weekly kind e2e
runs the base walkthrough; it also proves the optional extra Secret may be
absent.

Manual checks, on a cluster with the relevant CNI/runtime:

- gVisor: pod `runtimeClassName`, `uname -r` differs from the node kernel,
  gateway proxy works, port-forward limitation acknowledged.
- nodeSelector: pods land on the intended nodes; no `Pending` events.
- Sandbox policy: the allow/deny matrix above.
- Exposure policies: browser path works; a non-proxy pod cannot reach the
  gateway; probes stay healthy.
- Provider keys: set/unset output (never the values) and the refresh procedure.
- Failed-Pod recovery: `spec.failedPodPolicy` on the Sandbox, and the drill
  above replaces a `Failed` Pod while keeping the claim and PVC.

## 7. Rollback summary

| Layer | Rollback |
| --- | --- |
| Sandbox policy | `kubectl delete -f manifests/optional/network-policy/` |
| Exposure policy | delete the exposure variant directory |
| gVisor | patch `runtimeClassName: null`, recreate pods |
| nodeSelector | patch `nodeSelector: null`, recreate pods |
| Provider keys | delete the extra Secret or a key, recreate pods |
| Failed-Pod recovery | patch `failedPodPolicy: Retain`, delete warm-pool spares |

No PVC or workspace data is touched by any rollback. Deleting a `SandboxClaim`
is different: it cascades to the Sandbox **and** its PVC, so do not use claim
deletion to apply a pod-spec change.
