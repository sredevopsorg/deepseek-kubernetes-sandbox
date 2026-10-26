# Minimal single-tenant DSH Web

The smallest Kubernetes deployment of DeepSeek Harness (DSH) Web: one
`SandboxTemplate`, a one-replica warm pool and one claim. It is the "just one
DSH Web instance" quickstart.

For the full platform — warm pool sizing, per-user claims, a browser-facing
gateway with cookie sessions, idle-suspend and wake-on-connect — see
[`examples/dsh-agents-as-a-service/`](../dsh-agents-as-a-service/README.md).
This example deliberately has no gateway: you reach DSH through a
`kubectl port-forward` and its launch-token URL.

## Prerequisites

- A Kubernetes cluster with a default StorageClass (kind and GKE both work).
- The agent-sandbox controller with extensions (API `v1beta1`) installed.
  The version is pinned as `AGENT_SANDBOX_VERSION` in
  [`versions.env`](../../versions.env):

  ```bash
  # from the repository root:
  . versions.env
  kubectl apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/sandbox-with-extensions.yaml"
  kubectl -n agent-sandbox-system rollout status deployment --timeout=180s
  ```

- A DeepSeek API key for real model calls. The pod starts without one; only
  model requests fail. Optionally create `dsh-provider-keys-extra` for
  OpenRouter, Gemini, NVIDIA or other provider keys (the template references it
  with `optional: true`).

## Quickstart

Run these from this example's directory (`examples/minimal-single-tenant/`).

```bash
# 1. Namespace + shared provider keys. They are injected into the sandbox via
#    envFrom and are never committed.
kubectl create namespace dsh-demo
kubectl -n dsh-demo create secret generic dsh-provider-keys \
  --from-literal=DEEPSEEK_API_KEY="sk-..."

# Optional: extra providers, injected through a second Secret. Create it before
# the warm pool so the pods are stamped with the keys.
kubectl -n dsh-demo create secret generic dsh-provider-keys-extra \
  --from-literal=OPENROUTER_API_KEY="sk-or-..." \
  --from-literal=GEMINI_API_KEY="..." \
  --from-literal=NVIDIA_API_KEY="nvapi-..."

# 2. The template itself (image pinned in the manifest).
kubectl apply -f manifests/sandbox-template.yaml

# 3. A template only describes pods; a warm pool turns it into running
#    Sandboxes and a claim adopts one. One replica is enough here.
kubectl apply -f - <<'EOF'
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxWarmPool
metadata:
  name: dsh-pool
  namespace: dsh-demo
spec:
  replicas: 1
  sandboxTemplateRef:
    name: dsh-web-template
---
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxClaim
metadata:
  name: dsh
  namespace: dsh-demo
spec:
  warmPoolRef:
    name: dsh-pool
EOF

kubectl -n dsh-demo wait sandboxclaim/dsh --for=condition=Ready --timeout=180s
```

## Talk to the instance

DSH gates `/` behind a launch token it prints only in its startup logs. Open
the printed URL once; DSH then sets its own auth cookie and the UI loads
normally.

```bash
SB=$(kubectl -n dsh-demo get sandboxclaim dsh -o jsonpath='{.status.sandbox.name}')
kubectl -n dsh-demo logs "$SB" | grep -m1 '?token='
# dsh web: http://<pod-ip>:3080/?token=...

kubectl -n dsh-demo port-forward "pod/$SB" 18081:3080
# open http://localhost:18081/?token=... from the log line
```

> **Port note:** DSH Web listens on `3080` in-cluster. If `3080` is already in
> use on your workstation, forward to another local port (here `18081`), never
> `3080`. `localhost` is always in DSH's trusted-host list, so the forwarded
> request is accepted.
>
> **gVisor caveat:** if you uncomment `runtimeClassName: gvisor` in the
> template, `kubectl port-forward` no longer works. Use the multi-tenant gateway
> example or a reachable Service instead; see
> [optional hardening](../dsh-agents-as-a-service/docs/optional-hardening.md).

## Network policy and optional hardening

The template omits `networkPolicyManagement`, so the agent-sandbox controller
uses its Managed secure default: ingress only from the sandbox-router, public
egress, and no internal cluster DNS. `kubectl port-forward` is local to the pod
and still works (except under gVisor).

gVisor, an optional `nodeSelector`, extra provider keys and a stricter sandbox
NetworkPolicy are documented in
[optional hardening](../dsh-agents-as-a-service/docs/optional-hardening.md).

## Probes and trusted hosts

Two settings in `manifests/sandbox-template.yaml` are easy to get wrong:

- **Probe path.** `/` returns `401` without the launch-token cookie, so a probe
  there crash-loops the pod. The template probes `/favicon.svg` (served
  unauthenticated) and gates liveness/readiness behind a `startupProbe`, which
  gives a cold start (plugin activation, or the first resume) up to 120s before
  a restart.
- **Host allow-list.** DSH rejects unknown `Host` headers. `localhost` and
  `127.0.0.1` are always trusted; if you later front the sandbox with an
  Ingress or proxy, set `DSH_TRUSTED_HOSTS` to that public host (comma or space
  separated). The full example explains both exposure variants in
  [`manifests/exposure/README.md`](../dsh-agents-as-a-service/manifests/exposure/README.md).

The template also sets `fsGroupChangePolicy: OnRootMismatch`. DSH's
credentials plugin requires `.credentials.yaml` to be `0600`, while the
kubelet's default fsGroup policy rewrites it to `0660` on every pod start —
without this policy a recreated or resumed sandbox crash-loops. The rationale
is spelled out in
[`10-sandboxtemplate.yaml`](../dsh-agents-as-a-service/manifests/10-sandboxtemplate.yaml).

## Suspend, resume and clean up

```bash
# Suspend: the pod is deleted, the PVC (state) and Service are kept.
kubectl -n dsh-demo patch sandbox "$SB" --type merge -p '{"spec":{"operatingMode":"Suspended"}}'

# Resume: a new pod reattaches the same PVC; conversations survive.
kubectl -n dsh-demo patch sandbox "$SB" --type merge -p '{"spec":{"operatingMode":"Running"}}'
kubectl -n dsh-demo wait sandbox "$SB" --for=condition=Ready --timeout=180s

# Delete everything this quickstart created.
kubectl -n dsh-demo delete sandboxclaim dsh
kubectl delete namespace dsh-demo
```

Deleting the claim cascades to the sandbox and its PVC.
