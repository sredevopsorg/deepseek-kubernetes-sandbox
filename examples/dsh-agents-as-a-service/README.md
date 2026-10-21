# DeepSeek Harness Web as a Service: the multi-user platform pattern

This example distills the upstream
[hermes-agents-as-a-service](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/examples/hermes-agents-as-a-service)
platform pattern into pure agent-sandbox resources + kubectl, running **DeepSeek Harness Web
(DSH)** instead of Hermes. Every user of such a platform gets a personal, long-lived DSH Web
instance that is **provisioned in ~2 seconds from a warm pool, suspended when idle to save
cost, and woken transparently when the user returns** — with all state (conversations,
sessions, config) surviving on a per-user PVC at `/home/node/.dsh`.

It ships in two layers:

1. **The resource layer** (steps 1–7): pure manifests + kubectl, so every mechanism is visible.
2. **The service layer** (step 8): a small included **gateway** (`gateway/`) that makes it
   directly usable — signup API, per-user bearer tokens, a `/u/{user}/**` proxy that
   transparently **wakes suspended instances on connect**, and an idle sweeper that suspends
   them again.

What it demonstrates, feature by feature:

| agent-sandbox feature | Role in the platform |
| --- | --- |
| `SandboxTemplate` with `volumeClaimTemplates` | Identical DSH pods; per-user state on a PVC at `/home/node/.dsh` |
| `envVarsInjectionPolicy` / `volumeClaimTemplatesPolicy: Disallowed` | Claims that would bypass the warm pool are **rejected loudly**, not cold-started silently |
| `service: true` | Stable per-sandbox DNS (`status.serviceFQDN`) for the gateway to route on |
| `SandboxWarmPool` | Signup latency is pre-paid: image pull, PVC provision, DSH boot all happen before any user exists |
| `SandboxClaim` + `warmPoolRef` | "Signup" = create a ~10-line claim; adoption is a label flip, Ready in ~2s |
| `additionalPodMetadata` | The one warm-compatible per-claim customization (user label on the pod) |
| `Sandbox.spec.operatingMode` | Suspend = delete only the pod (PVC + Service survive); resume = recreate pod, reattach the same PVC |
| Cascade delete | Deleting the claim garbage-collects sandbox **and** PVC — account deletion in one call |

## Differences from the Hermes example

- **Multi-arch**: the DSH image is built for amd64 and arm64, so there is no
  `kubernetes.io/arch: amd64` nodeSelector.
- **Single port**: DSH Web serves everything on `3080`; the gateway proxies to one upstream
  instead of splitting `/v1/*` (API) from the dashboard.
- **One shared secret**: only `DEEPSEEK_API_KEY` is injected (via `envFrom`); there is no
  dashboard basic-auth, session-cookie secret, or API-server key.
- **Host forwarding**: the gateway forwards the original `Host` + `X-Forwarded-*` (and keeps
  `DSH_PUBLIC_URL` empty) so DSH generates reachable links from the request.
- **WebSocket caveat**: the requests-based streaming proxy handles SSE but not WebSocket
  upgrades. For full DSH web-GUI interactivity, front the gateway with a path-based Ingress
  per user (or a WS-capable proxy).

## Prerequisites

- A Kubernetes cluster with a default StorageClass (kind and GKE both work as-is).
- agent-sandbox with extensions installed:

  ```bash
  VERSION=v1.0.7
  kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox-with-extensions.yaml
  kubectl -n agent-sandbox-system rollout status deployment --timeout=180s
  ```

Or run everything below in one shot with `./run-test-kind.sh` (expects a kind cluster + the
manifest above already applied).

## 1. Install the platform resources

First the namespace, then the shared provider key. The key reaches **every** tenant sandbox
(a warm-pool constraint: pod env is stamped before any user exists), so it is **never
committed** — generate a fresh value per install:

```bash
kubectl apply -f 00-prereqs.yaml
kubectl -n dsh-demo create secret generic dsh-provider-keys \
  --from-literal=DEEPSEEK_API_KEY="sk-..."
kubectl apply -f 10-sandboxtemplate.yaml -f 20-sandboxwarmpool.yaml
```

Watch the two warm spares boot (first time pulls the image — subsequent operations never pay
that again; that is the point of the pool):

```bash
kubectl -n dsh-demo get sandboxes -l agents.x-k8s.io/warm-pool-sandbox -w
```

Wait until both show `READY: True`. Each spare already has its PVC bound and DSH Web serving
on port `3080`.

## 2. "Signup": claim an instance (~2s)

A user signing up is one small claim — `warmPoolRef` plus a pod label:

```bash
kubectl apply -f 30-claim-alice.yaml
kubectl -n dsh-demo wait sandboxclaim dsh-alice --for=condition=Ready --timeout=60s
```

Inspect what happened:

```bash
SB=$(kubectl -n dsh-demo get sandboxclaim dsh-alice -o jsonpath='{.status.sandbox.name}')
echo "alice's sandbox: $SB"
kubectl -n dsh-demo get sandboxes --show-labels
```

Three things worth noticing:

- **The sandbox keeps its pool-generated name** (`dsh-pool-…`), not the claim's name —
  adoption re-labels an existing warm sandbox rather than creating one. A platform must always
  resolve user → sandbox through `claim.status.sandbox.name`.
- The `agents.x-k8s.io/warm-pool-sandbox` label is **gone** from the adopted sandbox (it left
  the pool's inventory), and the pool has already started replenishing back to 2 spares.
- The pod carries the claim's `additionalPodMetadata` label
  (`sandbox.users.io/dsh-user=alice`), and
  `kubectl -n dsh-demo get sandbox $SB -o jsonpath='{.status.serviceFQDN}'` shows the stable
  DNS name the gateway proxies to.

## 3. Talk to the instance

```bash
kubectl -n dsh-demo port-forward "$SB" 18081:3080
# open http://localhost:18081 — DSH Web for alice
```

> **Local port note:** DSH Web listens on `3080` **in-cluster**. If `3080` is already in use
> on your workstation (e.g. by a local DSH), forward to a different local port (here `18081`),
> never `3080`.

## 4. Write state on the PVC

```bash
kubectl -n dsh-demo exec "$SB" -- sh -c 'echo "remember me" > /home/node/.dsh/marker.txt'
```

## 5. Suspend: delete the pod, keep the PVC + Service

```bash
kubectl -n dsh-demo patch sandbox "$SB" --type merge -p '{"spec":{"operatingMode":"Suspended"}}'
kubectl -n dsh-demo wait --for=delete "pod/$SB" --timeout=120s
kubectl -n dsh-demo get pvc "dsh-home-$SB"   # still here
kubectl -n dsh-demo get svc "$SB"            # still here
```

## 6. Resume and verify state survived

```bash
kubectl -n dsh-demo patch sandbox "$SB" --type merge -p '{"spec":{"operatingMode":"Running"}}'
kubectl -n dsh-demo wait sandbox "$SB" --for=condition=Ready --timeout=180s
kubectl -n dsh-demo exec "$SB" -- cat /home/node/.dsh/marker.txt   # "remember me"
```

## 7. Injection policy rejects env-carrying claims

```bash
kubectl apply -f 40-claim-rejected.yaml
kubectl -n dsh-demo get sandboxclaim dsh-mallory \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].reason}'
# EnvVarsInjectionRejected — the claim never bound a sandbox
kubectl delete -f 40-claim-rejected.yaml
```

## 8. The gateway: signup, proxy, idle-suspend, wake-on-connect

Build the gateway image and deploy it:

```bash
docker build -t dsh-aaas-gateway:demo gateway/
kind load docker-image dsh-aaas-gateway:demo   # (kind clusters)
kubectl apply -f 50-gateway.yaml
kubectl -n dsh-demo rollout status deploy/dsh-gateway --timeout=120s
kubectl -n dsh-demo port-forward svc/dsh-gateway 18080:8080
```

Then:

```bash
# signup → claim over the warm pool + one-time bearer token
SIGNUP=$(curl -s -X POST http://localhost:18080/users -H 'Content-Type: application/json' -d '{"user":"bob"}')
TOKEN=$(echo "$SIGNUP" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')

# proxy to bob's DSH Web (the gateway wakes it if suspended)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:18080/u/bob/

# tokenless is rejected
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:18080/u/bob/   # 401

# after IDLE_TIMEOUT (60s) the sweeper suspends bob
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:18080/users/bob   # state: Suspended

# delete bob (cascades sandbox + PVC)
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:18080/users/bob
```

## Cost model

The same economics as the upstream pattern: suspend-when-idle plus warm pools turn an
always-on instance into a sub-second cold-start pause per return-after-absence, at a fraction
of the cost.

## Cleanup

```bash
kubectl delete namespace dsh-demo
```
