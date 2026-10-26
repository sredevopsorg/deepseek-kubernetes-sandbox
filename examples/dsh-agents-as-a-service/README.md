# DeepSeek Harness Web as a Service: the multi-user platform pattern

This example distills the upstream
[hermes-agents-as-a-service](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/examples/hermes-agents-as-a-service)
platform pattern into pure agent-sandbox resources + kubectl, running **DeepSeek
Harness Web (DSH)** instead of Hermes. Every user of such a platform gets a
personal, long-lived DSH Web instance that is **provisioned in ~2 seconds from a
warm pool, suspended when idle to save cost, and woken transparently when the
user returns** — with all state (conversations, sessions, config) surviving on a
per-user PVC at `/home/node/.dsh`.

It ships in three layers:

1. **The resource layer** (steps 1–7): pure manifests + kubectl, so every
   mechanism is visible.
2. **The service layer** (step 8): a small included **gateway** (`gateway/`) that
   makes the platform browser-usable — a signup page/API, `HttpOnly` cookie
   sessions, a `/u/{user}/**` proxy for HTTP **and WebSocket** that transparently
   **wakes suspended instances on connect**, and an idle sweeper that suspends
   them again. The gateway performs the DSH launch-token handshake server-side,
   so the browser never sees the token.
3. **The exposure layer** (step 9): choose exactly one way in — a plain Traefik
   Ingress, or Gateway API + oauth2-proxy (GitHub SSO).

All manifests live under [`manifests/`](manifests/):

```
manifests/
+-- 00-prereqs.yaml              # namespace + empty provider-key Secret
+-- 10-sandboxtemplate.yaml      # the DSH Web blueprint
+-- 20-sandboxwarmpool.yaml      # 2 pre-warmed spares
+-- 30-claim-alice.yaml          # one user
+-- 40-claim-rejected.yaml       # negative demo: env injection rejected
+-- 50-gateway.yaml              # Deployment/RBAC/Service for the gateway
+-- optional/
|   +-- README.md                # opt-in manifests, applied by path
|   +-- network-policy/10-sandbox-networkpolicy.yaml
+-- exposure/
    +-- README.md                 # the two exposure variants, when to use which
    +-- ingress/60-ingress.yaml
    +-- ingress/61-networkpolicy.yaml
    +-- gateway-api-sso/70-gateway.yaml ... 75-networkpolicies.yaml
```

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
- **Single port**: DSH Web serves everything on `3080`; the gateway proxies to one
  upstream instead of splitting `/v1/*` (API) from the dashboard.
- **Shared provider keys**: `DEEPSEEK_API_KEY` plus any keys in the optional
  `dsh-provider-keys-extra` Secret are injected via `envFrom` into every
  sandbox. There is no per-tenant key, dashboard basic-auth, session-cookie
  secret, or API-server key.
- **Launch-token handshake**: DSH gates `/` behind a token it prints only in its
  logs. The gateway reads it, performs the handshake, and keeps the resulting
  auth cookie server-side; the browser authenticates to the gateway with its own
  cookie. See [How the DSH handshake works](#how-the-dsh-handshake-works).
- **Host allow-list**: DSH rejects unknown `Host` headers. The gateway forwards
  the browser's `Host`, so the public hostname must be added to
  `DSH_TRUSTED_HOSTS` in the template.
- **Probes target `/favicon.svg`**: `/` returns `401` without the launch token, so
  a probe on `/` crash-loops the pod. `/favicon.svg` is unauthenticated, and a
  `startupProbe` gates liveness/readiness so a slow cold start (plugin
  activation, or a resumed sandbox) is not restarted mid-boot.

## Prerequisites

- A Kubernetes cluster with a default StorageClass (kind and GKE both work as-is).
- agent-sandbox with extensions installed:

  ```bash
  VERSION=v1.0.8   # AGENT_SANDBOX_VERSION in versions.env
  kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/$VERSION/sandbox-with-extensions.yaml
  kubectl -n agent-sandbox-system rollout status deployment --timeout=180s
  ```

  For step 9's SSO variant you additionally need the Gateway API CRDs and a
  Gateway implementation (`gatewayClassName: traefik` in the manifests); see the
  [exposure README](manifests/exposure/README.md).

- Optional: run the whole resource + gateway walkthrough in one shot with
  [`scripts/e2e-kind.sh`](scripts/e2e-kind.sh). It expects a kind cluster with
  agent-sandbox installed, then creates and deletes the `dsh-demo` namespace
  itself. It stops after the gateway layer (step 8); exposure is not applied.

## 1. Install the platform resources

First the namespace, then the shared provider keys. The keys reach **every**
tenant sandbox (a warm-pool constraint: pod env is stamped before any user
exists), so they are **never committed** — generate fresh values per install.
`00-prereqs.yaml` already creates the empty `dsh-provider-keys` Secret, so use
an upsert:

```bash
kubectl apply -f manifests/00-prereqs.yaml
kubectl -n dsh-demo create secret generic dsh-provider-keys \
  --from-literal=DEEPSEEK_API_KEY="sk-..." \
  --dry-run=client -o yaml | kubectl apply -f -
```

Optional: additional providers (OpenRouter, Gemini, NVIDIA, OpenAI, Anthropic,
...) go in a second Secret. It is referenced with `optional: true`, so the
sandboxes start without it, and every key becomes an env var:

```bash
kubectl -n dsh-demo create secret generic dsh-provider-keys-extra \
  --from-literal=OPENROUTER_API_KEY="sk-or-..." \
  --from-literal=GEMINI_API_KEY="..." \
  --from-literal=NVIDIA_API_KEY="nvapi-..." \
  --dry-run=client -o yaml | kubectl apply -f -
```

Create both Secrets **before** the template/warm pool. A later Secret change
does not update running pods: delete the warm-pool spares and recreate claimed
pods to pick up new keys. Keys are shared by all tenants; a key saved in DSH's
credential store wins over env. See
[optional hardening](docs/optional-hardening.md) for the full env-name list and
the refresh procedure.

```bash
kubectl apply -f manifests/10-sandboxtemplate.yaml -f manifests/20-sandboxwarmpool.yaml
```

Watch the two warm spares boot (first time pulls the image — subsequent operations
never pay that again; that is the point of the pool):

```bash
kubectl -n dsh-demo get sandboxes -l agents.x-k8s.io/warm-pool-sandbox -w
```

Wait until both show `READY: True`. Each spare already has its PVC bound and DSH
Web serving on port `3080`.

## 2. "Signup": claim an instance (~2s)

A user signing up is one small claim — `warmPoolRef` plus a pod label:

```bash
kubectl apply -f manifests/30-claim-alice.yaml
kubectl -n dsh-demo wait sandboxclaim dsh-alice --for=condition=Ready --timeout=60s
```

Inspect what happened:

```bash
SB=$(kubectl -n dsh-demo get sandboxclaim dsh-alice -o jsonpath='{.status.sandbox.name}')
echo "alice's sandbox: $SB"
kubectl -n dsh-demo get sandboxes --show-labels
```

Three things worth noticing:

- **The sandbox keeps its pool-generated name** (`dsh-pool-…`), not the claim's
  name — adoption re-labels an existing warm sandbox rather than creating one. A
  platform must always resolve user → sandbox through `claim.status.sandbox.name`.
- The `agents.x-k8s.io/warm-pool-sandbox` label is **gone** from the adopted
  sandbox (it left the pool's inventory), and the pool has already started
  replenishing back to 2 spares.
- The pod carries the claim's `additionalPodMetadata` label
  (`sandbox.users.io/dsh-user=alice`), and
  `kubectl -n dsh-demo get sandbox $SB -o jsonpath='{.status.serviceFQDN}'` shows
  the stable DNS name the gateway proxies to.

## 3. Talk to the instance

DSH Web needs its launch token on first contact — a bare `/` returns `401`. The
token is in the pod logs:

```bash
kubectl -n dsh-demo logs "$SB" | grep -m1 '?token='
# dsh web: http://<pod-ip>:3080/?token=...
kubectl -n dsh-demo port-forward "pod/$SB" 18081:3080
# open http://localhost:18081/?token=... (from the log line)
```

> **Local port note:** DSH Web listens on `3080` **in-cluster**. If `3080` is
> already in use on your workstation (e.g. by a local DSH), forward to a
> different local port (here `18081`), never `3080`. `localhost` is always in
> DSH's trusted-host list, so the forwarded request is accepted.

> **Why the pod stays up:** this claim was applied by hand, so it carries no
> gateway ownership annotation (`aaas.example.com/managed-by`). The gateway's idle
> sweeper only suspends claims it created through `POST /users`; the resource-layer
> claims from these manifests are never touched. You can deploy the gateway
> (step 8) and still keep a sandbox you are port-forwarding to.

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

> **Why resume works:** the pod template sets `fsGroupChangePolicy: OnRootMismatch`.
> DSH requires its `.credentials.yaml` to be `0600`, while the kubelet's default
> fsGroup policy widens every file to `0660` on each pod start — a resumed sandbox
> would crash-loop. See [Troubleshooting](#troubleshooting) for the one-time fix
> on sandboxes created before this setting existed.

## 7. Injection policy rejects env-carrying claims

```bash
kubectl apply -f manifests/40-claim-rejected.yaml
kubectl -n dsh-demo get sandboxclaim dsh-mallory \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].reason}'
# EnvVarsInjectionRejected — the claim never bound a sandbox
kubectl delete -f manifests/40-claim-rejected.yaml
```

## 8. The gateway: signup, browser proxy, idle-suspend, wake-on-connect

### Deploy it

```bash
# Published image (on kind, load it into the cluster; on a real cluster the
# kubelet pulls it from GHCR):
GW=ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox/dsh-agents-as-a-service/gateway:dsh-v0.2.1-rc.3-r4
docker pull "$GW"
kind load docker-image "$GW" --name <kind-cluster>

# ... or build it locally and tag it exactly as manifests/50-gateway.yaml
# expects (make gateway builds dsh-aaas-gateway:dev):
make gateway
docker tag dsh-aaas-gateway:dev "$GW"
kind load docker-image "$GW" --name <kind-cluster>

kubectl apply -f manifests/50-gateway.yaml
kubectl -n dsh-demo rollout status deploy/dsh-gateway --timeout=120s
kubectl -n dsh-demo port-forward svc/dsh-gateway 18080:8080
```

> The gateway binds `8080`; the local port-forward uses `18080` and never `3080`.
>
> **Image pull policy.** `manifests/50-gateway.yaml` uses
> `imagePullPolicy: IfNotPresent` with the release identity, so a locally built
> image must be loaded into the cluster under exactly that name (kind:
> `kind load`). A node that already cached a different tag is the usual cause of
> "my new build is not running"; pin the digest for a reproducible rollout:
>
> ```bash
> kubectl -n dsh-demo set image deploy/dsh-gateway \
>   gateway=ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox/dsh-agents-as-a-service/gateway@sha256:<digest>
> kubectl -n dsh-demo rollout status deploy/dsh-gateway --timeout=120s
> ```
>
> **Applying over an existing install?** Run
> `kubectl diff -f manifests/10-sandboxtemplate.yaml` first. Cluster-specific
> fields such as `nodeAffinity` and the warm-pool replica count are intentionally
> not in the repo manifests; preserve them with a targeted patch instead of a
> blind apply. Moving the probes to `/favicon.svg` is the crash-loop fix.
>
> The template also sets `fsGroupChangePolicy: OnRootMismatch` (see the note in
> step 6). Existing `Sandbox` objects snapshot their pod template, so a template
> apply does not update them: patch each sandbox's
> `spec.podTemplate.spec.securityContext.fsGroupChangePolicy` and
> `chmod 600` its `.credentials.yaml`, or recreate it.
>
> The optional gVisor/`nodeSelector` fields are pod-spec changes too: existing
> Sandboxes snapshot them, and warm-pool spares are not replaced by a template
> edit. See [optional hardening](docs/optional-hardening.md) for the patch and
> recreation commands. A provider-key Secret change likewise needs pod
> recreation to take effect.

### Browser flow

Open <http://localhost:18080>. The landing page has a one-field signup form.
Enter `bob`, and the gateway:

1. creates the `SandboxClaim` over the warm pool,
2. waits for it to become `Ready`,
3. sets an `HttpOnly`, `SameSite=Lax` cookie `dsh_gw_session=<user>:<token>` on
   the browser,
4. redirects to `/u/bob/`, where the DSH Web UI loads through the gateway.

From then on every request under `/u/bob/` (assets, API calls, SSE, WebSocket) is
proxied to bob's sandbox. The DSH launch token and DSH's own auth cookie stay in
the gateway; the browser only holds the gateway cookie.

The same flow with `curl`, no JavaScript (the session cookie is the only
credential):

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
(`{"user":"bob"}`) and the returned one-time token, `POST /login` to exchange a
token for a cookie, and `Authorization: Bearer <token>` on subsequent calls.

### Gateway API

| Route | Purpose |
| --- | --- |
| `GET /healthz` | Liveness/readiness; no cluster access |
| `GET /` | Landing page: signup, token login, logout. With SSO it auto-provisions and connects instead. |
| `POST /users` | Signup. JSON or form; `201` (JSON), `202` (waking), `303` (browser redirect), `409` if user exists; sets the session cookie |
| `POST /login` | Exchange a bearer token for the session cookie |
| `POST /logout` | Clear the session cookie |
| `POST /recreate` | SSO only: delete the authenticated user's claim (wiping workspace data) and provision a fresh one; `303` to `/u/{user}/`. Cookie-authenticated calls must be same-origin. |
| `GET /users/{user}` | Derived state (`Ready`/`Waking`/`Suspended`) |
| `DELETE /users/{user}` | Delete claim (cascades sandbox + PVC) |
| `GET /u/{user}` | `308` redirect to `/u/{user}/` |
| `GET /u/{user}/**` | HTTP/SSE proxy to the user's DSH Web |
| `WS /u/{user}/**` | WebSocket proxy to the user's DSH Web |

The full API reference, configuration table, auth semantics and failure matrix
live in the [gateway README](gateway/README.md).

### How the DSH handshake works

DSH does not expose an API for its launch token; it prints a URL containing
`?token=…` in the pod logs. For each `(user, pod UID, Host)` the gateway:

1. resolves the claim → sandbox → pod (annotation `agents.x-k8s.io/pod-name`, or
   the sandbox name as pod name),
2. reads the pod's logs (last `DSH_LOG_TAIL_LINES` lines) and parses the most
   recent `dsh web: …?token=…` line,
3. requests `http://<serviceFQDN>:3080/?token=…` with the browser's `Host`,
4. captures DSH's `dsh-auth-*` `Set-Cookie` response and caches it,
5. injects that cookie (and strips the browser's `Cookie`/`Authorization` and any
   `?token=`) on every proxied HTTP and WebSocket request,
6. refreshes the handshake once if DSH answers `401`, and drops the cached cookie
   when the sandbox suspends or is deleted.

This is why the gateway's RBAC includes `pods` `get`/`list` and `pods/log`
`get`. It is also the most fragile part of the design; see
[Limitations](#limitations).

### DSH_TRUSTED_HOSTS

DSH rejects requests whose `Host` is not allow-listed (`localhost` and
`127.0.0.1` are always trusted). Because the gateway forwards the browser's
`Host`, set the public hostname in the template:

```yaml
env:
  - name: DSH_TRUSTED_HOSTS
    value: "dsh.example.com"   # comma or space separated; empty for port-forward only
```

The same applies to a local `docker run`: the printed token URL uses
`DSH_PUBLIC_URL` if set, otherwise `http://localhost:<DSH_PORT>`.

### WebSocket

The gateway is a Starlette ASGI app and proxies WebSocket upgrades with a
bidirectional pump, so the DSH web GUI is fully interactive. Requirements:

- the ingress/load balancer in front of the gateway must pass `Upgrade`/`Connection`
  headers; [`manifests/exposure/ingress/60-ingress.yaml`](manifests/exposure/ingress/60-ingress.yaml)
  is a starting point (Traefik handles upgrades by default),
- idle suspension is disabled for users with in-flight proxied requests or open
  WebSockets, so a long-running chat stream is not cut off mid-turn,
- the upstream upgrade dials the sandbox **pod IP**, forwards no `Host`/`Origin`,
  and performs the launch-token handshake under that same `pod-IP:3080` authority.
  DSH fences on `Host` (only its bind address, loopback, or an allow-listed host
  passes) and binds its auth cookie to the request authority
  (`payload.authority === authority`), while `websockets` derives `Host` from the
  URI and cannot be overridden. A browser-host cookie or forwarded origin makes
  the upgrade fail (`401`/`403`, surfaced as close `1011`), which the client
  retries forever as `[connection] connection lost, retry #N`,
- missing/invalid gateway auth is rejected right after the upgrade with close code
  `4401` (WebSocket auth has no HTTP `401` path); cross-origin or Origin-less
  upgrades with the cookie are rejected the same way (CSWSH protection), so
  clients must send a matching `Origin`; upstream bridging failures close with
  `1011`.

### Path routing

Each user's DSH Web is served under `/u/<user>/`. DSH injects `<base href="./">`
and uses relative asset/API URLs, so the UI works under a path prefix without a
per-user hostname or wildcard DNS. The gateway also rewrites absolute-path
`Location` headers into `/u/<user>/…` and redirects `/u/<user>` to the
trailing-slash form with `308` so relative URLs resolve correctly.

## 9. Expose it: choose exactly one variant

The base manifests expose nothing outside the cluster (`dsh-gateway` is a
ClusterIP Service on port `8080`). Pick **one** variant — they are alternatives,
not a stack. Details, commands and the switch procedure are in the
[exposure README](manifests/exposure/README.md).

> Apply the base resources first, then the variant **by path**. Do not use
> `kubectl apply -R`: it would apply both variants and create two paths to the
> same gateway, which is unsafe once SSO is enabled.

Both variants also ship NetworkPolicies: `ingress/61-networkpolicy.yaml` limits
gateway ingress to Traefik pods, and `gateway-api-sso/75-networkpolicies.yaml`
limits the gateway to oauth2-proxy and oauth2-proxy egress to DNS, public HTTPS
and the gateway. They are part of the variant; see the
[exposure README](manifests/exposure/README.md) and
[optional hardening](docs/optional-hardening.md).

### Option A — plain Ingress (gateway's own session auth)

```bash
# Replace the dsh.example.com host and TLS Secret (or drop the tls: block for
# plain HTTP), then:
kubectl apply -f manifests/exposure/ingress/
```

1. Set `DSH_TRUSTED_HOSTS` in `manifests/10-sandboxtemplate.yaml` to the same
   public host (the gateway forwards the browser's `Host`) and re-apply the
   template.
2. Set `COOKIE_SECURE=true` on the gateway once the Ingress serves HTTPS:
   `kubectl -n dsh-demo set env deploy/dsh-gateway COOKIE_SECURE=true`.
   (The base manifest keeps `false` so the plain-HTTP port-forward demo works.)
3. Verify: `curl -sI https://<host>/healthz` returns `200`; sign up in a browser
   at `https://<host>/`.
4. `manifests/exposure/ingress/61-networkpolicy.yaml` is part of this variant:
   it allows only Traefik pods to reach the gateway on `8080`. Adjust its
   selector if your ingress controller uses a different pod label or
   `hostNetwork`; if kubelet probes fail, see the hardening doc.

Keep the router free of buffering/compression middlewares: they break WebSocket
upgrades and streaming.

### Option B — Gateway API + GitHub SSO (oauth2-proxy)

`manifests/exposure/gateway-api-sso/` puts a TLS `Gateway` + `HTTPRoute` in
front of **oauth2-proxy**, which authenticates the browser against GitHub
(restricted to a configured organization) and forwards to the gateway with
`X-Forwarded-User` set. The gateway trusts that header only when
`AUTH_PROXY_HEADER` is set (SSO is **opt-in**).

Prerequisites:

- Gateway API CRDs and a Gateway implementation; the manifests assume
  `gatewayClassName: traefik` (the CRD version is pinned as
  `GATEWAY_API_VERSION` in `versions.env`).
- A GitHub OAuth App whose homepage/callback match the public FQDN
  (`https://<host>/` and `https://<host>/oauth2/callback`).
- The oauth2-proxy credentials Secret (template:
  `73-oauth2-proxy-secret.yaml.example`) and a TLS Secret for the Gateway
  listener (template: `74-tls-secret.yaml.example`).

Steps:

```bash
# 1. Real Secrets; the committed *.yaml.example files are templates only.
kubectl -n dsh-demo create secret generic oauth2-proxy \
  --from-literal=client-id='<GitHub OAuth App client ID>' \
  --from-literal=client-secret='<GitHub OAuth App client secret>' \
  --from-literal=cookie-secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
kubectl -n dsh-demo create secret tls dsh-demo-tls --cert=tls.crt --key=tls.key

# 2. Replace dsh-demo.example.com (70/71/72) and your-github-org (72).

# 3. Enable SSO on the gateway and trust the public host in the template.
kubectl -n dsh-demo set env deploy/dsh-gateway \
  AUTH_PROXY_HEADER=X-Forwarded-User COOKIE_SECURE=true
#    ... and set DSH_TRUSTED_HOSTS=dsh-demo.example.com in
#    manifests/10-sandboxtemplate.yaml, then re-apply the template.

# 4. Apply the variant.
kubectl apply -f manifests/exposure/gateway-api-sso/
```

Verification:

```bash
kubectl get gateway dsh-gateway -n dsh-demo -o yaml        # Programmed=True
kubectl get httproute -n dsh-demo -o wide                  # backend oauth2-proxy:4180, Accepted
kubectl get deploy oauth2-proxy -n dsh-demo                # 1/1 Ready
kubectl get endpoints oauth2-proxy -n dsh-demo             # backend present
kubectl -n dsh-demo logs deploy/oauth2-proxy --tail=50     # no config errors
```

Browser check: open `https://<host>/` — you are redirected to GitHub, and after
signing in as a member of the allowed organization the gateway auto-provisions
your sandbox (the first page can show `Provisioning`) and connects you. A
non-member is denied before reaching the gateway. GitHub logins are lowercase
DNS-1123 labels, so they map 1:1 onto the gateway's `user` validation. The SSO
landing page also offers **Recreate sandbox** (`POST /recreate`), which deletes
the claim — wiping the workspace PVC — and provisions a fresh one.

Sign-out: the gateway `/logout` clears `dsh_gw_session`; oauth2-proxy's session
can be cleared at `/oauth2/sign_out`.

> **SSO trust boundary — read this before enabling it.** After SSO is on, the
> `dsh-gateway` Service must be reachable **only** through oauth2-proxy (it is
> ClusterIP). The variant's `75-networkpolicies.yaml` restricts pod traffic
> accordingly: the gateway accepts traffic only from oauth2-proxy pods,
> oauth2-proxy only from the Gateway implementation, and oauth2-proxy egress is
> limited to DNS, public HTTPS and the gateway. Anyone who can reach
> `dsh-gateway:8080` directly can still send `X-Forwarded-User: <victim>` and be
> treated as that user, so never add a second path (the Ingress variant, a
> shared-host port-forward, a permissive policy). To turn SSO off, remove the
> variable (`kubectl -n dsh-demo set env deploy/dsh-gateway AUTH_PROXY_HEADER-`)
> and delete the SSO variant; the gateway falls back to bearer-token/session
> auth.

### Switching variants

Delete the variant you had before applying the other one
(`kubectl delete -f manifests/exposure/ingress/`), revert
`AUTH_PROXY_HEADER`/`COOKIE_SECURE`, and verify with
`kubectl -n dsh-demo get ingress,gateway,httproute` that exactly one path to the
gateway remains.

## Optional hardening

The base walkthrough is deliberately portable. Four opt-in isolation layers are
documented in [optional hardening](docs/optional-hardening.md):

- **gVisor** — uncomment `runtimeClassName: gvisor` before applying, or patch an
  existing install. Requires a `gvisor` RuntimeClass and capable nodes.
  `kubectl port-forward` does not work under gVisor; the gateway path does.
- **nodeSelector** — uncomment and replace the placeholder node label to pin
  sandboxes and the gateway to a dedicated pool; add a toleration for tainted
  pools.
- **Sandbox NetworkPolicy** —
  `kubectl apply -f manifests/optional/network-policy/` allows only
  gateway→sandbox on `3080` and sandbox→DNS/public `443`.
- **Provider keys** — create `dsh-provider-keys-extra` as shown in step 1.
- **Failed-Pod recovery** — uncomment `failedPodPolicy: Recreate` so the
  controller replaces a `Failed` sandbox Pod (node eviction) instead of parking
  the sandbox at `Finished=True`. Requires agent-sandbox v1.0.8+.

Apply each independently and re-run the walkthrough. `kubectl apply -f manifests/`
is non-recursive, so the optional manifest is not applied by the base step.

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
| Console spams `[connection] connection lost, retry #N` | The upstream WS upgrade is being rejected, surfaced as close `1011`. The gateway dials the sandbox **pod IP**, forwards no `Host`/`Origin`, and handshakes under the same `pod-IP:3080` authority DSH binds its cookie to; an older gateway image, or a sandbox with an empty `status.podIPs`, hits this loop |
| `manifest.webmanifest` returns `401` in the console | Browsers fetch the Web App Manifest without credentials, so the gateway's session cookie is absent and the request fails closed. The UI is unaffected; only the PWA install prompt is lost |
| Signup is slow / `202` then `Waking` | Warm pool exhausted (`kubectl get sandboxes -l agents.x-k8s.io/warm-pool-sandbox`); scale the pool |
| `403` from the Kubernetes API in gateway logs | The gateway ServiceAccount is missing claim/sandbox/pod-log verbs (see `manifests/50-gateway.yaml`) |
| Gateway pod `ErrImagePull` / `ImagePullBackOff` (especially on kind) | The manifest uses `imagePullPolicy: IfNotPresent` with the release identity. Build with `make gateway`, retag to exactly the manifest's image, `kind load docker-image <that-ref>`, or set the Deployment image to your local tag / pinned digest. On a real cluster, make sure the GHCR package is readable (public or a pull secret) |
| Step 3: `pod "$SB" not found` after deploying the gateway | The claim was suspended by the idle sweeper. Claims created by the gateway carry `aaas.example.com/managed-by=dsh-gateway`; hand-applied claims (steps 1–7) do not and are never swept. Resume the sandbox: `kubectl -n dsh-demo patch sandbox "$SB" --type merge -p '{"spec":{"operatingMode":"Running"}}'` |
| Resume crash-loops: `credentials-local: … is readable beyond its owner (mode 660)` | The pod template lacks `fsGroupChangePolicy: OnRootMismatch`, so the kubelet's default fsGroup policy rewrote `.credentials.yaml` to `0660`; DSH requires `0600` and refuses to start. New installs get the policy from the template. For an existing sandbox: suspend it, run a throwaway pod on `dsh-home-$SB` to `chmod 600 /home/node/.dsh/.credentials.yaml`, patch the sandbox's `podTemplate` with the policy, then resume |
| SSO: GitHub login succeeds but the gateway asks for a token / returns `401` to `/recreate` | `AUTH_PROXY_HEADER` is not set on `deploy/dsh-gateway` (SSO is opt-in). Set `AUTH_PROXY_HEADER=X-Forwarded-User` and confirm the request actually reaches the gateway through oauth2-proxy |
| SSO: gateway treats a caller as another user / bad `X-Forwarded-User` accepted | Trust boundary violation: something other than oauth2-proxy can reach `dsh-gateway:8080`. Remove the second path (Ingress/Gateway/port-forward), keep the Service ClusterIP, and only re-enable SSO once the proxy is the sole entry point. Never put a trusted identity header in front of a service that is directly reachable |
| SSO: oauth2-proxy `cookie_secret must be 16, 24, or 32 bytes ... but is 44 bytes` | The cookie secret was generated as standard base64. Use URL-safe unpadded base64 (see `73-oauth2-proxy-secret.yaml.example`) |
| SSO: redirect loop (`ERR_TOO_MANY_REDIRECTS`) | oauth2-proxy `--redirect-url` / callback does not match the public FQDN, or TLS is not terminating at the Gateway listener; fix the OAuth App URLs and the manifest placeholders |
| SSO: DSH rejects the public host behind the proxy | Add the public hostname to `DSH_TRUSTED_HOSTS` in `manifests/10-sandboxtemplate.yaml`; the gateway forwards the browser's `Host` |
| Pods `Pending` after setting `nodeSelector` | No node matches the label, the pool is full, or a tainted pool needs a toleration; inspect `kubectl describe pod` and the node labels |
| gVisor: `kubectl port-forward` fails or the connection is refused | Port-forward is incompatible with gVisor; reach DSH through the gateway (or a reachable Service/Sandbox Router) |
| Sandbox cannot reach a model API after applying the optional NetworkPolicy | The policy allows DNS and public TCP `443` only; a proxy, custom base URL, port `80` or non-443 endpoint needs an explicit egress rule |
| Gateway cannot reach a sandbox after applying an exposure NetworkPolicy | Check that the CNI enforces NetworkPolicy and that the Traefik/oauth2-proxy pod labels match; kubelet probes may also need the node CIDR |
| New provider key not visible in a sandbox | Pod env is stamped at creation: delete the warm-pool spares and recreate claimed pods. A key saved in DSH's credential store also wins over env |
| Sandbox reports `Finished=True` and every gateway request fails | The sandbox Pod reached phase `Failed` (node eviction, spot reclaim) and the default `Retain` keeps it. Delete the Pod so the controller recreates it, or set `failedPodPolicy: Recreate` (agent-sandbox v1.0.8+; see optional hardening) |

## Limitations

Deliberately **not** production code:

- **Single replica.** Idle clocks, in-flight tracking, and the handshake cache
  are in memory. Running two gateway replicas would double-suspend or lose wake
  tracking; scale up only after moving that state to a shared store.
- **Log scraping.** The launch token is recovered from pod logs. Log rotation, a
  restarted pod, or a changed DSH log format can break the handshake until the
  next `401` refresh.
- **Shared provider keys.** `DEEPSEEK_API_KEY` and any keys in the optional
  `dsh-provider-keys-extra` Secret are shared by all tenants via the template.
  Tenants can override a key through DSH's stored credential file on their PVC,
  but the platform does not manage that. Real multi-tenancy needs per-tenant
  credentials or an egress proxy.
- **No TLS in the gateway.** Terminate TLS at an Ingress or Gateway and set
  `COOKIE_SECURE=true`.
- **SSO trust is hop-based.** `AUTH_PROXY_HEADER` trusts whoever sets the header.
  The SSO variant ships `75-networkpolicies.yaml`, which restricts the gateway
  to oauth2-proxy and oauth2-proxy egress to the gateway/DNS/public HTTPS; a
  custom exposure without that policy has no signature, mTLS or network
  enforcement.
- **Isolation is opt-in.** gVisor, the sandbox NetworkPolicy and the exposure
  policies are opt-in; without them, sandboxes share the host kernel and can
  reach the cluster network. The base example is a demo, not a hardened
  multi-tenant platform.
- **OAuth org is the only authorization.** Any GitHub user in the configured
  organization gets an auto-provisioned sandbox; there is no per-user allow-list,
  quota, or admin API.
- **In-memory user directory.** Users are claims + annotations; there is no
  database, password reset, or admin API. Tokens are stored only as SHA-256
  hashes in claim annotations.
- **Bearer tokens in URLs.** Avoid putting the signup token in a URL/log; the
  browser flow uses the `HttpOnly` cookie instead.

## Cost model

The same economics as the upstream pattern: suspend-when-idle plus warm pools turn
an always-on instance into a sub-second cold-start pause per return-after-absence,
at a fraction of the cost.

## Cleanup

```bash
# Delete an exposure variant first if you applied one (optional; namespace
# deletion below removes everything):
kubectl delete -f manifests/exposure/ingress/          # or gateway-api-sso/
# Then the whole example:
kubectl delete namespace dsh-demo
```

If you created the oauth2-proxy/TLS Secrets or the GitHub OAuth App for step 9,
delete the Secrets with the namespace and revoke the OAuth App in GitHub.

## More

- [Gateway README](gateway/README.md) — API, configuration, auth/SSO, failure
  semantics, limitations.
- [Architecture](docs/architecture.md) — container/sequence diagrams, ADRs,
  security and RBAC, failure modes, evolution path.
- [Exposure README](manifests/exposure/README.md) — the two variants in detail.
- [Optional hardening](docs/optional-hardening.md) — gVisor, nodeSelector,
  NetworkPolicy, extra provider keys and failed-Pod recovery, with verification
  and rollback.
