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
2. **The service layer** (step 8): a small included **gateway** (`gateway/`) that makes the
   platform browser-usable — a signup page/API, `HttpOnly` cookie sessions, a `/u/{user}/**`
   proxy for HTTP **and WebSocket** that transparently **wakes suspended instances on
   connect**, and an idle sweeper that suspends them again. The gateway performs the DSH
   launch-token handshake server-side, so the browser never sees the token.

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
- **Launch-token handshake**: DSH gates `/` behind a token it prints only in its logs. The
  gateway reads it, performs the handshake, and keeps the resulting auth cookie server-side;
  the browser authenticates to the gateway with its own cookie. See
  [How the DSH handshake works](#how-the-dsh-handshake-works).
- **Host allow-list**: DSH rejects unknown `Host` headers. The gateway forwards the browser's
  `Host`, so the public hostname must be added to `DSH_TRUSTED_HOSTS` in the template.
- **Probes target `/favicon.svg`**: `/` returns `401` without the launch token, so a probe on
  `/` crash-loops the pod. `/favicon.svg` is unauthenticated, and a `startupProbe` gates
  liveness/readiness so a slow cold start (plugin activation, or a resumed sandbox) is not
  restarted mid-boot.

## Prerequisites

- A Kubernetes cluster with a default StorageClass (kind and GKE both work as-is).
- agent-sandbox with extensions installed:

  ```bash
  VERSION=v1.0.7
  kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox-with-extensions.yaml
  kubectl -n agent-sandbox-system rollout status deployment --timeout=180s
  ```

Or run everything below in one shot with `./run-test-kind.sh` (expects a kind cluster + the
manifest above already applied; it creates and deletes the namespace itself).

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

DSH Web needs its launch token on first contact — a bare `/` returns `401`. The token is in the
pod logs:

```bash
kubectl -n dsh-demo logs "$SB" | grep -m1 '?token='
# dsh web: http://<pod-ip>:3080/?token=...
kubectl -n dsh-demo port-forward "pod/$SB" 18081:3080
# open http://localhost:18081/?token=... (from the log line)
```

> **Local port note:** DSH Web listens on `3080` **in-cluster**. If `3080` is already in use
> on your workstation (e.g. by a local DSH), forward to a different local port (here `18081`),
> never `3080`. `localhost` is always in DSH's trusted-host list, so the forwarded request is
> accepted.

> **Why the pod stays up:** this claim was applied by hand, so it carries no gateway ownership
> annotation (`aaas.example.com/managed-by`). The gateway's idle sweeper only suspends claims it
> created through `POST /users`; the resource-layer claims from these manifests are never
> touched. You can deploy the gateway (step 8) and still keep a sandbox you are port-forwarding
> to.

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

> **Why resume works:** the pod template sets `fsGroupChangePolicy: OnRootMismatch`. DSH requires
> its `.credentials.yaml` to be `0600`, while the kubelet's default fsGroup policy widens every
> file to `0660` on each pod start — a resumed sandbox would crash-loop. See
> [Troubleshooting](#troubleshooting) for the one-time fix on sandboxes created before this
> setting existed.

## 7. Injection policy rejects env-carrying claims

```bash
kubectl apply -f 40-claim-rejected.yaml
kubectl -n dsh-demo get sandboxclaim dsh-mallory \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].reason}'
# EnvVarsInjectionRejected — the claim never bound a sandbox
kubectl delete -f 40-claim-rejected.yaml
```

## 8. The gateway: signup, browser proxy, idle-suspend, wake-on-connect

### Deploy it

```bash
# Published image (on kind, load it into the cluster; on a real cluster it is
# pulled from GHCR):
docker pull ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:gateway
kind load docker-image ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:gateway --name <kind-cluster>

# ... or build it locally and tag it exactly as 50-gateway.yaml expects:
docker build -t ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:gateway gateway/
kind load docker-image ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:gateway --name <kind-cluster>

kubectl apply -f 50-gateway.yaml
kubectl -n dsh-demo rollout status deploy/dsh-gateway --timeout=120s
kubectl -n dsh-demo port-forward svc/dsh-gateway 18080:8080
```

> The gateway binds `8080`; the local port-forward uses `18080` and never `3080`.
>
> **Re-deploying after a new image?** The workflow publishes the mutable `:gateway` tag and the
> Deployment uses `imagePullPolicy: IfNotPresent`, so a node that already cached the tag keeps
> the old image across `kubectl apply`/`rollout restart`. Pin the digest for a reproducible
> rollout (the workflow's `Inspect image` step and `docker buildx imagetools inspect` both show
> it):
>
> ```bash
> kubectl -n dsh-demo set image deploy/dsh-gateway \
>   gateway=ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox@sha256:<digest>
> kubectl -n dsh-demo rollout status deploy/dsh-gateway --timeout=120s
> ```
>
> **Applying over an existing install?** Run `kubectl diff -f 10-sandboxtemplate.yaml`
> first. Cluster-specific fields such as `nodeAffinity` and the warm-pool replica count are
> intentionally not in the repo manifests; preserve them with a targeted patch instead of a
> blind apply. Moving the probes to `/favicon.svg` is the crash-loop fix.
>
> The template also sets `fsGroupChangePolicy: OnRootMismatch` (see the note in step 6).
> Existing `Sandbox` objects snapshot their pod template, so a template apply does not update
> them: patch each sandbox's `spec.podTemplate.spec.securityContext.fsGroupChangePolicy` and
> `chmod 600` its `.credentials.yaml`, or recreate it.

### Browser flow

Open <http://localhost:18080>. The landing page has a one-field signup form. Enter `bob`, and
the gateway:

1. creates the `SandboxClaim` over the warm pool,
2. waits for it to become `Ready`,
3. sets an `HttpOnly`, `SameSite=Lax` cookie `dsh_gw_session=<user>:<token>` on the browser,
4. redirects to `/u/bob/`, where the DSH Web UI loads through the gateway.

From then on every request under `/u/bob/` (assets, API calls, SSE, WebSocket) is proxied to
bob's sandbox. The DSH launch token and DSH's own auth cookie stay in the gateway; the browser
only holds the gateway cookie.

The same flow with `curl`, no JavaScript (the session cookie is the only credential):

```bash
JAR=$(mktemp)
# signup (form POST, exactly what the landing page sends)
curl -s -i -c "$JAR" -H 'Origin: http://localhost:18080' \
  -d 'user=bob' http://localhost:18080/users
# proxy: bob's DSH Web through the gateway
curl -s -b "$JAR" http://localhost:18080/u/bob/ | head -5
# without the cookie the gateway fails closed
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:18080/u/bob/   # 401
# derived state: Ready / Waking / Suspended / ...
curl -s -b "$JAR" http://localhost:18080/users/bob
# delete bob (cascades sandbox + PVC)
curl -s -b "$JAR" -H 'Origin: http://localhost:18080' -X DELETE http://localhost:18080/users/bob
```

API clients that prefer bearer tokens can use `POST /users` with a JSON body
(`{"user":"bob"}`) and the returned one-time token, `POST /login` to exchange a token for a
cookie, and `Authorization: Bearer <token>` on subsequent calls.

### Gateway API

| Route | Purpose |
| --- | --- |
| `GET /healthz` | Liveness/readiness; no cluster access |
| `GET /` | Landing page: signup, token login, logout |
| `POST /users` | Signup. JSON or form; `201` (JSON), `202` (waking), `303` (browser redirect), `409` if user exists; sets the session cookie |
| `POST /login` | Exchange a bearer token for the session cookie |
| `POST /logout` | Clear the session cookie |
| `GET /users/{user}` | Derived state (`Ready`/`Waking`/`Suspended`) |
| `DELETE /users/{user}` | Delete claim (cascades sandbox + PVC) |
| `GET /u/{user}` | `308` redirect to `/u/{user}/` |
| `GET /u/{user}/**` | HTTP/SSE proxy to the user's DSH Web |
| `WS /u/{user}/**` | WebSocket proxy to the user's DSH Web |

### How the DSH handshake works

DSH does not expose an API for its launch token; it prints a URL containing `?token=…` in the
pod logs. For each `(user, pod UID, Host)` the gateway:

1. resolves the claim → sandbox → pod (annotation `agents.x-k8s.io/pod-name`, or the sandbox
   name as pod name),
2. reads the pod's logs (last `DSH_LOG_TAIL_LINES` lines) and parses the most recent
   `dsh web: …?token=…` line,
3. requests `http://<serviceFQDN>:3080/?token=…` with the browser's `Host`,
4. captures DSH's `dsh-auth-*` `Set-Cookie` response and caches it,
5. injects that cookie (and strips the browser's `Cookie`/`Authorization` and any `?token=`)
   on every proxied HTTP and WebSocket request,
6. refreshes the handshake once if DSH answers `401`, and drops the cached cookie when the
   sandbox suspends or is deleted.

This is why the gateway's RBAC includes `pods` `get`/`list` and `pods/log` `get`. It is also
the most fragile part of the design; see [Limitations](#limitations).

### DSH_TRUSTED_HOSTS

DSH rejects requests whose `Host` is not allow-listed (`localhost` and `127.0.0.1` are always
trusted). Because the gateway forwards the browser's `Host`, set the public hostname in the
template:

```yaml
env:
  - name: DSH_TRUSTED_HOSTS
    value: "dsh.example.com"   # comma or space separated; empty for port-forward only
```

The same applies to a local `docker run`: the printed token URL uses `DSH_PUBLIC_URL` if set,
otherwise `http://localhost:${DSH_PORT}`.

### WebSocket

The gateway is a Starlette ASGI app and proxies WebSocket upgrades with a bidirectional pump, so
the DSH web GUI is fully interactive. Requirements:

- the ingress/load balancer in front of the gateway must pass `Upgrade`/`Connection` headers;
  `60-ingress.yaml` is a starting point (Traefik handles upgrades by default),
- idle suspension is disabled for users with in-flight proxied requests or open WebSockets, so
  a long-running chat stream is not cut off mid-turn,
- the upstream upgrade dials the sandbox **pod IP** and forwards no `Host`/`Origin`: DSH's Host
  fence only trusts its own bind address or a loopback/allow-listed host, and `websockets`
  derives `Host` from the URI (it cannot be overridden). Forwarding the browser's origin here
  makes DSH answer `403`, which shows up in the browser as an endless
  `[connection] connection lost, retry #N` loop,
- missing/invalid gateway auth is rejected right after the upgrade with close code `4401`
  (WebSocket auth has no HTTP `401` path); cross-origin or Origin-less upgrades with the cookie
  are rejected the same way (CSWSH protection), so clients must send a matching `Origin`;
  upstream bridging failures close with `1011`.

### Path routing

Each user's DSH Web is served under `/u/<user>/`. DSH injects `<base href="./">` and uses
relative asset/API URLs, so the UI works under a path prefix without a per-user hostname or
wildcard DNS. The gateway also rewrites absolute-path `Location` headers into `/u/<user>/…` and
redirects `/u/<user>` to the trailing-slash form with `308` so relative URLs resolve correctly.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Pod `CrashLoopBackOff`, probes failing with `401` | Probe path is `/`; change readiness/liveness to `/favicon.svg` |
| Browser shows `401` from DSH through the gateway | Gateway handshake failed — check `kubectl -n dsh-demo logs deploy/dsh-gateway` and that RBAC grants `pods/log` |
| `502` / `upstream unavailable` | Sandbox pod is not Ready (wake timed out) or the Service DNS is stale; check `kubectl get sandbox`, `WAKE_TIMEOUT` |
| `503` with `Retry-After` | The launch token was not found in the pod logs yet; the gateway retries after a short delay. Check the pod logs and `DSH_LOG_TAIL_LINES` |
| `409` on signup | User already has a claim; use `POST /login` with the existing token or delete the user first |
| Cookie set but browser keeps getting `401` | `COOKIE_SECURE=true` while serving over plain HTTP (secure cookies are not sent); use HTTPS or set `false` for local demos |
| WebSocket closes immediately (`4401`) | Missing/expired gateway cookie, or the upgrade's `Origin` does not match `Host` (CSWSH check); DSH rejecting `Host` instead surfaces as close `1011` — add the host to `DSH_TRUSTED_HOSTS` |
| Console spams `[connection] connection lost, retry #N` | The upstream WS upgrade is being rejected (DSH answers `403` to the forward, surfaced as close `1011`). The gateway dials the sandbox **pod IP** and forwards no `Host`/`Origin` for upgrades; a gateway image from before that fix, or a sandbox with an empty `status.podIPs`, hits the loop |
| `manifest.webmanifest` returns `401` in the console | Browsers fetch the Web App Manifest without credentials, so the gateway's session cookie is absent and the request fails closed. The UI is unaffected; only the PWA install prompt is lost |
| Signup is slow / `202` then `Waking` | Warm pool exhausted (`kubectl get sandboxes -l agents.x-k8s.io/warm-pool-sandbox`); scale the pool |
| `403` from the Kubernetes API in gateway logs | The gateway ServiceAccount is missing claim/sandbox/pod-log verbs (see `50-gateway.yaml`) |
| Step 3: `pod "$SB" not found` after deploying the gateway | The claim was suspended by the idle sweeper. Claims created by the gateway carry `aaas.example.com/managed-by=dsh-gateway`; hand-applied claims (steps 1–7) do not and are never swept. Upgrade the gateway to a build that honors the marker, or resume the sandbox: `kubectl -n dsh-demo patch sandbox "$SB" --type merge -p '{"spec":{"operatingMode":"Running"}}'` |
| Resume crash-loops: `credentials-local: … is readable beyond its owner (mode 660)` | The pod template lacks `fsGroupChangePolicy: OnRootMismatch`, so the kubelet's default fsGroup policy rewrote `.credentials.yaml` to `0660`; DSH requires `0600` and refuses to start. New installs get the policy from the template. For an existing sandbox: suspend it, run a throwaway pod on `dsh-home-$SB` to `chmod 600 /home/node/.dsh/.credentials.yaml`, patch the sandbox's `podTemplate` with the policy, then resume. |

## Limitations

Deliberately **not** production code:

- **Single replica.** Idle clocks, in-flight tracking, and the handshake cache are in memory.
  Running two gateway replicas would double-suspend or lose wake tracking; scale up only after
  moving that state to a shared store.
- **Log scraping.** The launch token is recovered from pod logs. Log rotation, a restarted pod,
  or a changed DSH log format can break the handshake until the next `401` refresh.
- **Shared provider key.** `DEEPSEEK_API_KEY` is shared by all tenants via the template. Real
  multi-tenancy needs per-tenant credentials or an egress proxy.
- **No TLS in the gateway.** Terminate TLS at an Ingress/reverse proxy and set
  `COOKIE_SECURE=true`.
- **In-memory user directory.** Users are claims + annotations; there is no database, password
  reset, or admin API. Tokens are stored only as SHA-256 hashes in claim annotations.
- **Bearer tokens in URLs.** Avoid putting the signup token in a URL/log; the browser flow uses
  the `HttpOnly` cookie instead.

## Cost model

The same economics as the upstream pattern: suspend-when-idle plus warm pools turn an
always-on instance into a sub-second cold-start pause per return-after-absence, at a fraction
of the cost.

## Cleanup

```bash
kubectl delete namespace dsh-demo
```
