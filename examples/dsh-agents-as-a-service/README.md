# DeepSeek Harness Web as a Service: the multi-user platform pattern

This example distills the upstream
[hermes-agents-as-a-service](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/examples/hermes-agents-as-a-service)
platform pattern into pure agent-sandbox resources + kubectl, running **DeepSeek
Harness Web (DSH)** instead of Hermes. Every user of such a platform gets a
personal, long-lived DSH Web instance that is **provisioned in ~2 seconds from a
warm pool, suspended when idle to save cost, and woken transparently when the
user returns** — with all state (conversations, sessions, config, and the agent
workspace) surviving on the per-user PVC at `/home/node/.dsh` + `/workspace`.
Suspend-when-idle plus the warm pool keep cost proportional to use: a return
after absence pays a short cold-start pause, not an always-on instance.

It ships in three layers:

1. **The resource layer** (steps 1–7): pure manifests + kubectl, so every
   mechanism is visible.
2. **The service layer** (step 8): a small included **gateway** (`gateway/`) that
   makes the platform browser-usable — a signup page/API, revocable server-side
   **opaque-cookie sessions**, auth rate limiting and an optional signup invite
   code, a `/u/{user}/**` proxy for HTTP **and WebSocket** that transparently
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
| `SandboxTemplate` with `volumeClaimTemplates` | Identical DSH pods; per-user state at `/home/node/.dsh` and the agent workspace at `/workspace`, on one PVC (the workspace is a `subPath` mount, so DSH state stays private) |
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
  sandbox. There is no per-tenant key, dashboard basic-auth, signed-session
  secret (sessions are opaque server-side ids), or API-server key.
- **Launch-token handshake**: DSH gates `/` behind a token it prints only in its
  logs. The gateway reads it, performs the handshake, and keeps the resulting
  auth cookie server-side; the browser authenticates to the gateway with its own
  opaque session cookie, never the token. See
  [How the DSH handshake works](#how-the-dsh-handshake-works).
- **Host allow-list**: DSH rejects unknown `Host` headers. The gateway forwards
  the browser's `Host`, so the public hostname must be added to
  `DSH_TRUSTED_HOSTS` in the template.
- **Probes target `/favicon.svg`**: `/` returns `401` without the launch token, so
  a probe on `/` crash-loops the pod. `/favicon.svg` is unauthenticated, and a
  `startupProbe` gates liveness/readiness so a slow cold start (plugin
  activation, or a resumed sandbox) is not restarted mid-boot.

## Prerequisites

Run the walkthrough from this example's directory (`examples/dsh-agents-as-a-service/`).

- A Kubernetes cluster with a default StorageClass (kind and GKE both work as-is).
- agent-sandbox with extensions installed:

  ```bash
  . ../../versions.env
  kubectl apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/sandbox-with-extensions.yaml"
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
kubectl -n dsh-demo exec "$SB" -- printenv HOME                        # /workspace
kubectl -n dsh-demo exec "$SB" -- sh -c 'echo "remember me" > /home/node/.dsh/marker.txt'
kubectl -n dsh-demo exec "$SB" -- sh -c 'echo "remember me too" > /workspace/marker.txt'
```

`/workspace` (the DSH image's `WORKDIR`, where agents keep files) is the same
`dsh-home` PVC mounted again with `subPath: workspace`. The two trees stay
separate, so `.credentials.yaml` and the session store are never part of the
agent-visible workspace.

The template also sets `HOME=/workspace`. DSH's directory picker (the folder
explorer used to choose a workspace) has no start-path setting: when it opens,
its browse backend lists `$HOME`, so with the image default (`/home/node`, which
is not on a volume) it would start outside the persisted tree. Pointing `HOME`
at the workspace makes the picker open on it (labelled *Home* in its breadcrumbs)
and makes `~` in agent commands mean `/workspace`. The trade-off is that tools
keeping caches or config under `$HOME` (`npm`/`pnpm`, `uv`, `git`, `gh`, and
DSH's global `~/.agents` instructions and skills) write dotfiles onto the PVC
inside the agent workspace, where the agent and the file explorer can see them.
`DSH_HOME` is explicit (`/home/node/.dsh`), so sessions, config and credentials
are unaffected. Pod env is stamped when a sandbox is created: a sandbox from
before this setting keeps `HOME=/home/node` until it is recreated (see
[Troubleshooting](#troubleshooting)).

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
kubectl -n dsh-demo exec "$SB" -- cat /workspace/marker.txt        # "remember me too"
```

> **Why resume works:** the pod template sets `fsGroupChangePolicy: OnRootMismatch`;
> without it the kubelet widens `.credentials.yaml` to `0660` and DSH crash-loops.
> See [Troubleshooting](#troubleshooting) for the one-time fix on older sandboxes.

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
GW=ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox/dsh-agents-as-a-service/gateway:dsh-v0.2.1-rc.5
docker pull "$GW"
kind load docker-image "$GW" --name <kind-cluster>

# ... or build it locally and tag it exactly as manifests/50-gateway.yaml
# expects (make gateway builds dsh-aaas-gateway:dev):
make -C ../.. gateway
docker tag dsh-aaas-gateway:dev "$GW"
kind load docker-image "$GW" --name <kind-cluster>

kubectl apply -f manifests/50-gateway.yaml
kubectl -n dsh-demo rollout status deploy/dsh-gateway --timeout=120s
kubectl -n dsh-demo port-forward svc/dsh-gateway 18080:8080
```

> The gateway binds `8080`; the local port-forward uses `18080` and never `3080`.
>
> **Cookie/TLS guard.** The base manifest serves plain HTTP through port-forward,
> so it sets `COOKIE_SECURE=false` **and** `ALLOW_INSECURE_COOKIES=1`. The gateway
> refuses to start when `COOKIE_SECURE=false` unless
> `ALLOW_INSECURE_COOKIES=1` explicitly acknowledges that the session cookie
> travels in clear text; that prevents an HTTPS deployment from silently serving
> an insecure cookie. Before exposing the gateway over TLS, set
> `COOKIE_SECURE=true` and remove the acknowledgement:
> `kubectl -n dsh-demo set env deploy/dsh-gateway COOKIE_SECURE=true ALLOW_INSECURE_COOKIES-`.
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

Open <http://localhost:18080>. The landing page has a one-field signup form (plus
an invite-code field when `SIGNUP_INVITE_CODE` is configured). Enter `bob`, and
the gateway:

1. enforces the auth rate limit and, if configured, the signup invite code,
2. creates the `SandboxClaim` over the warm pool,
3. waits for it to become `Ready`,
4. mints a random, server-side session (the cookie value is an opaque `sid`, not
   a user name and not a token), stores `sid -> (user, expiry)` in memory, and
   sets the `HttpOnly`, `SameSite=Lax` cookie `dsh_gw_session=<sid>` on the
   browser (`__Host-dsh_gw_session` when `COOKIE_SECURE=true`),
5. redirects to `/u/bob/`, where the DSH Web UI loads through the gateway.

From then on every request under `/u/bob/` (assets, API calls, SSE, WebSocket) is
proxied to bob's sandbox. The DSH launch token and DSH's own auth cookie stay in
the gateway; the browser only holds the opaque gateway session id. The cookie is
useless as a bearer token.

**Sessions are revocable and mostly short-lived in practice.** `POST /logout`,
deleting the user, `POST /recreate` and cookie expiry (default 7 days,
`COOKIE_MAX_AGE`) invalidate the server-side session. The session table lives in
gateway memory, so **restarting the gateway logs every browser out** — expected
for this single-replica demo; a production deployment needs a shared session
store and sticky routing or per-request re-authentication. With SSO enabled, the
gateway mints a session for the identity oauth2-proxy asserts instead of asking
for a token.

The same flow with `curl`, no JavaScript (the session cookie is the only
credential):

```bash
JAR=$(mktemp)
# signup (form POST, exactly what the landing page sends); add -d 'invite=...'
# when SIGNUP_INVITE_CODE is configured, or send X-Signup-Invite
curl -s -i -c "$JAR" -H 'Origin: http://localhost:18080' \
  -d 'user=bob' http://localhost:18080/users
# proxy: bob's DSH Web through the gateway
curl -s -b "$JAR" http://localhost:18080/u/bob/ | head -5
# without the cookie the gateway fails closed
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:18080/u/bob/   # 401
# derived state: Ready / Waking / Suspended / ...
curl -s -b "$JAR" http://localhost:18080/users/bob
# sign out (revokes the server-side session)
curl -s -b "$JAR" -H 'Origin: http://localhost:18080' -X POST http://localhost:18080/logout
# delete bob (cascades sandbox + PVC and revokes every session for bob)
curl -s -b "$JAR" -H 'Origin: http://localhost:18080' -X DELETE http://localhost:18080/users/bob
```

API clients that prefer bearer tokens can use `POST /users` with a JSON body
(`{"user":"bob"}`) and the returned one-time token, `POST /login` to exchange a
token for a cookie, and `Authorization: Bearer <token>` on subsequent calls. A
cookie/session value is never accepted as a bearer token, and a token for one
user cannot read another user's path.

### Auth abuse controls and proxy limits

The gateway is a single process, so these limits are per replica. They stop
casual abuse and one tenant monopolizing the service; an internet-facing
deployment still needs ingress-level rate limiting and an audit trail.

| Setting | Default | Behavior |
| --- | --- | --- |
| `AUTH_RATE_LIMIT` / `AUTH_RATE_WINDOW` | `30` per `60` s | Throttles `/users` and `/login` per client (rightmost `X-Forwarded-For` when behind a proxy, else the TCP peer) and answers `429` + `Retry-After` before reading the body or touching the cluster. `0` disables it. The base manifest sets it explicitly. |
| `SIGNUP_INVITE_CODE` | unset | When set, `POST /users` requires the code as form field `invite` or header `X-Signup-Invite` (constant-time compare, `403` before user lookup). Put it in a Secret and restart the gateway to rotate. |
| `MAX_IN_FLIGHT_PER_USER` | `32` | Caps concurrent proxied requests + WebSockets per user; excess HTTP gets `503` + `Retry-After`, excess WebSocket upgrades close with `1011`. |
| `WS_MAX_MESSAGE_BYTES` | `16 MiB` | Largest WebSocket message received from DSH; oversized frames close the connection. Browser-to-DSH frames are bounded by uvicorn's own limit. HTTP/SSE streaming is not capped by this. |
| `MAX_BODY_MB` | `32` | Largest proxied HTTP request body (`413`). |

The gateway also strips client-supplied `Forwarded`, `X-Forwarded-User` and
`X-Auth-Request-*` headers (and `x-forwarded-*` / `x-auth-request-*` prefixes)
before proxying, so a tenant cannot inject identity or spoof hop headers into
DSH. `UPSTREAM_HEADER_ALLOWLIST` can replace that denylist with an explicit
comma-separated allowlist. See the [gateway README](gateway/README.md) for the
full configuration.

### Gateway routes

The browser flow above uses the gateway's main routes. The full route table,
status codes, configuration and failure matrix live in the
[gateway README](gateway/README.md).

### How the DSH handshake works

DSH prints its launch token only in pod logs. The gateway resolves
claim → sandbox → pod, reads the pod-log tail, performs the `?token=` handshake
under the browser's `Host`, caches `dsh-auth-*` per `(user, pod UID, Host)`, and
refreshes once on a `401`. That is why its RBAC includes `pods` `get`/`list` and
`pods/log` `get`; it is also the most fragile part of the design (see
[Limitations](#limitations) and the
[architecture ADR](docs/architecture.md#adr-3-server-side-launch-token-handshake-instead-of-exposing-the-token)).

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

The gateway proxies WebSocket upgrades with a bidirectional pump, so the DSH web
GUI is fully interactive. The entry point must pass `Upgrade`/`Connection`
headers; [`manifests/exposure/ingress/60-ingress.yaml`](manifests/exposure/ingress/60-ingress.yaml)
is a starting point (Traefik handles upgrades by default). Idle suspension is
disabled while a proxied request or socket is in flight, so a long-running turn is
not cut off.

The upstream upgrade dials the sandbox **pod IP**, forwards no `Host`/`Origin`,
and performs the launch-token handshake under the same `pod-IP:3080` authority.
Missing or cross-origin auth closes with `4401`; upstream bridging failures close
with `1011`, as does exceeding `MAX_IN_FLIGHT_PER_USER` (per-user socket cap).
Messages received from DSH larger than `WS_MAX_MESSAGE_BYTES` (16 MiB by
default) close the connection instead of being buffered; browser-to-DSH frames
are bounded by uvicorn's own limit. See the
[gateway README](gateway/README.md#proxy-failure-semantics) for the full matrix.

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
and the gateway. They are part of the variant **and required for production**:
an exposure applied without its policy is demo-only. The sandbox side needs a
separate policy, and the gateway needs an egress policy; both are covered in
[optional hardening](docs/optional-hardening.md) (see the production checklist)
and summarized in the [exposure README](manifests/exposure/README.md).

### Option A — plain Ingress (gateway's own session auth)

1. Set the Ingress host/TLS (or drop the `tls:` block), and set
   `DSH_TRUSTED_HOSTS` in `manifests/10-sandboxtemplate.yaml` to the same public
   host; re-apply the template.
2. Set `COOKIE_SECURE=true` and drop the plain-HTTP acknowledgement once the
   Ingress serves HTTPS:
   `kubectl -n dsh-demo set env deploy/dsh-gateway COOKIE_SECURE=true ALLOW_INSECURE_COOKIES-`.
3. `kubectl apply -f manifests/exposure/ingress/`; verify
   `curl -sI https://<host>/healthz` returns `200`.

Keep the router free of buffering/compression middlewares: they break WebSocket
upgrades and streaming. The variant's `61-networkpolicy.yaml` allows only Traefik
pods to reach the gateway; see the [exposure README](manifests/exposure/README.md)
for selector and probe caveats.

### Option B — Gateway API + GitHub SSO (oauth2-proxy)

`manifests/exposure/gateway-api-sso/` puts a TLS `Gateway` + `HTTPRoute` in
front of **oauth2-proxy**, which authenticates the browser against GitHub
(restricted to a configured organization) and forwards to the gateway with
`X-Forwarded-User` set. The gateway trusts that header only when
`AUTH_PROXY_HEADER` is set (SSO is **opt-in**).

1. Install Gateway API CRDs and a Gateway implementation; the manifests assume
   `gatewayClassName: traefik` (the CRD version is `GATEWAY_API_VERSION` in
   `versions.env`).
2. Create a GitHub OAuth App whose homepage/callback match the public FQDN, and
   create the oauth2-proxy and TLS Secrets from
   `73-oauth2-proxy-secret.yaml.example` and `74-tls-secret.yaml.example`.
3. Replace `dsh-demo.example.com` and `your-github-org` in the variant
   manifests, set `DSH_TRUSTED_HOSTS` in the template, and enable SSO:
   `kubectl -n dsh-demo set env deploy/dsh-gateway AUTH_PROXY_HEADER=X-Forwarded-User COOKIE_SECURE=true ALLOW_INSECURE_COOKIES-`.
4. `kubectl apply -f manifests/exposure/gateway-api-sso/`; then run
   [`scripts/preflight-sso.sh`](scripts/preflight-sso.sh) and fix everything it
   reports **before** pointing real traffic at the Gateway. It verifies the
   Secrets (including a valid oauth2-proxy cookie secret length), the gateway's
   `AUTH_PROXY_HEADER`/`COOKIE_SECURE`, that the Gateway's TLS certificate
   reference resolves, and that no `your-github-org`/`dsh-demo.example.com`
   placeholder was left applied. Full verification steps are in the
   [exposure README](manifests/exposure/README.md).

> **Exposure without the variant NetworkPolicy is demo-only.** The SSO
> variant's `75-networkpolicies.yaml` is not optional in practice: it is the
> only thing that makes oauth2-proxy the sole path to the gateway. Applying
> only the Gateway/HTTPRoute/oauth2-proxy objects leaves the trust boundary
> unenforced, so anyone who can reach `dsh-gateway:8080` can send
> `X-Forwarded-User` and impersonate the user they claim to be.
>
> **SSO trust boundary — read this before enabling it.** After SSO is on, the
> `dsh-gateway` Service must be reachable **only** through oauth2-proxy (it is
> ClusterIP). The variant's `75-networkpolicies.yaml` restricts pod traffic
> accordingly: the gateway accepts traffic only from oauth2-proxy pods,
> oauth2-proxy only from the Gateway implementation, and its egress only to DNS,
> public HTTPS and the gateway. The gateway's own egress (Kubernetes API,
> sandbox pods) is not restricted by that file because the API-server endpoint is
> cluster-specific; add the
> [gateway egress policy](docs/optional-hardening.md#gateway-egress-required-for-production)
> for production. Anyone who can reach `dsh-gateway:8080` directly can send
> `X-Forwarded-User: <victim>`, so never add a second path. To turn SSO off,
> remove the variable
> (`kubectl -n dsh-demo set env deploy/dsh-gateway AUTH_PROXY_HEADER-`) and
> delete the SSO variant.

### Switching variants

Delete the variant you had before applying the other one, revert
`AUTH_PROXY_HEADER`/`COOKIE_SECURE`, and verify with
`kubectl -n dsh-demo get ingress,gateway,httproute` that exactly one path to the
gateway remains. Full commands and verification are in the
[exposure README](manifests/exposure/README.md).

## Optional hardening

The base walkthrough is deliberately portable, which means it is **not
hardened**: without the policies and gVisor it is demo-only, not a safe
multi-tenant platform. [Optional hardening](docs/optional-hardening.md) has the
settings and a production checklist. In short:

- **gVisor** — uncomment `runtimeClassName: gvisor` before applying, or patch an
  existing install. Requires a `gvisor` RuntimeClass and capable nodes.
  `kubectl port-forward` does not work under gVisor; the gateway path does.
  Required for untrusted agents in production.
- **nodeSelector** — uncomment and replace the placeholder node label to pin
  sandboxes and the gateway to a dedicated pool; add a toleration for tainted
  pools.
- **Sandbox NetworkPolicy** —
  `kubectl apply -f manifests/optional/network-policy/` allows only
  gateway→sandbox on `3080` and sandbox→DNS/public `443`. Required for
  production; exposure without it is demo-only.
- **Gateway egress policy** — the gateway needs DNS, the Kubernetes API and
  `3080` to sandbox pods. It is not shipped because the API-server endpoint is
  cluster-specific; use the
  [recipe](docs/optional-hardening.md#gateway-egress-required-for-production).
  Required for production.
- **Provider keys** — create `dsh-provider-keys-extra` as shown in step 1.
- **Failed-Pod recovery** — uncomment `failedPodPolicy: Recreate` so the
  controller replaces a `Failed` sandbox Pod (node eviction) instead of parking
  the sandbox at `Finished=True`. Requires agent-sandbox v1.0.8+.

The **exposure variants' NetworkPolicies** are not optional in production
either; see step 9. Apply each setting independently and re-run the walkthrough.
`kubectl apply -f manifests/` is non-recursive, so the optional manifests are not
applied by the base step.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Pod `CrashLoopBackOff`, probes failing with `401` | Probe path is `/`; change readiness/liveness to `/favicon.svg` |
| Browser shows `401` from DSH through the gateway | Gateway handshake failed — check `kubectl -n dsh-demo logs deploy/dsh-gateway` and that RBAC grants `pods/log` |
| `502` / `upstream unavailable` | Sandbox pod is not Ready (wake timed out) or the Service DNS is stale; check `kubectl get sandbox`, `WAKE_TIMEOUT` |
| `503` with `Retry-After` | Either the launch token was not found in the pod logs yet (the gateway retries after a short delay; check the pod logs and `DSH_LOG_TAIL_LINES`) or the user hit `MAX_IN_FLIGHT_PER_USER` (default 32 concurrent requests/WebSockets); reduce client concurrency or raise the limit deliberately |
| `429` with `Retry-After` on signup/login | The per-client auth rate limit (`AUTH_RATE_LIMIT`, default explicitly `30` per `AUTH_RATE_WINDOW=60` s) rejected the attempt before reading the body. Wait for the window, or raise the limit; behind a proxy make sure the real client IP is the rightmost `X-Forwarded-For` entry, not a shared NAT address |
| `403` on signup: invite required/invalid | `SIGNUP_INVITE_CODE` is configured. Send the code as form field `invite` or header `X-Signup-Invite`; the landing page shows the field when enabled. A wrong code is rejected before the user lookup |
| Gateway pod `CrashLoopBackOff` with `refusing to start with an insecure session cookie` | Intentional guard: plain HTTP is only allowed when explicitly acknowledged. For the local demo set `ALLOW_INSECURE_COOKIES=1` (the base manifest does); for HTTPS set `COOKIE_SECURE=true` and remove `ALLOW_INSECURE_COOKIES` |
| Every browser is logged out after a gateway rollout/restart | Expected: sessions are opaque ids in gateway memory and a restart drops them. Sign in again; a production deployment needs shared session state or per-request re-authentication. The DSH sandboxes and their data are unaffected |
| WebSocket closes with `1011` for a busy client | `MAX_IN_FLIGHT_PER_USER` (per-user connection cap) or an upstream/handshake failure closed the socket; check the gateway logs to tell them apart |
| WebSocket closes after a very large message from DSH | A message received from DSH exceeded `WS_MAX_MESSAGE_BYTES` (16 MiB), so the relay closed it instead of buffering; raise the limit only if DSH legitimately sends larger frames. Browser-to-DSH frames are bounded by uvicorn's own limit |
| `scripts/preflight-sso.sh` exits non-zero | It names the failing item: a missing Secret/key, a bad oauth2-proxy cookie-secret length, missing `AUTH_PROXY_HEADER`/`COOKIE_SECURE`, a dangling Gateway TLS reference, a leftover `your-github-org`/`dsh-demo.example.com` placeholder, or a second path to the gateway (an Ingress). Fix all items before exposing the SSO variant |
| `409` on signup | User already has a claim; use `POST /login` with the existing token or delete the user first |
| Cookie set but browser keeps getting `401` | `COOKIE_SECURE=true` while serving over plain HTTP (secure cookies are not sent); use HTTPS or set `false` **and** `ALLOW_INSECURE_COOKIES=1` for local demos |
| WebSocket closes immediately (`4401`) | Missing/expired gateway cookie, or the upgrade's `Origin` does not match `Host` (CSWSH check); DSH rejecting `Host` instead surfaces as close `1011` — add the host to `DSH_TRUSTED_HOSTS` |
| Console spams `[connection] connection lost, retry #N` | The upstream WS upgrade is being rejected, surfaced as close `1011`. The gateway dials the sandbox **pod IP**, forwards no `Host`/`Origin`, and handshakes under the same `pod-IP:3080` authority DSH binds its cookie to; an older gateway image, or a sandbox with an empty `status.podIPs`, hits this loop |
| `manifest.webmanifest` returns `401` in the console | Browsers fetch the Web App Manifest without credentials, so the gateway's session cookie is absent and the request fails closed. The UI is unaffected; only the PWA install prompt is lost |
| Signup is slow / `202` then `Waking` | Warm pool exhausted (`kubectl get sandboxes -l agents.x-k8s.io/warm-pool-sandbox`); scale the pool |
| `403` from the Kubernetes API in gateway logs | The gateway ServiceAccount is missing claim/sandbox/pod-log verbs (see `manifests/50-gateway.yaml`) |
| Gateway pod `ErrImagePull` / `ImagePullBackOff` (especially on kind) | The manifest uses `imagePullPolicy: IfNotPresent` with the release identity. Build with `make -C ../.. gateway`, retag to exactly the manifest's image, `kind load docker-image <that-ref>`, or set the Deployment image to your local tag / pinned digest. On a real cluster, make sure the GHCR package is readable (public or a pull secret) |
| Step 3: `pod "$SB" not found` after deploying the gateway | The claim was suspended by the idle sweeper. Claims created by the gateway carry `aaas.example.com/managed-by=dsh-gateway`; hand-applied claims (steps 1–7) do not and are never swept. Resume the sandbox: `kubectl -n dsh-demo patch sandbox "$SB" --type merge -p '{"spec":{"operatingMode":"Running"}}'` |
| Resume crash-loops: `credentials-local: … is readable beyond its owner (mode 660)` | The pod template lacks `fsGroupChangePolicy: OnRootMismatch`, so the kubelet's default fsGroup policy rewrote `.credentials.yaml` to `0660`; DSH requires `0600` and refuses to start. New installs get the policy from the template. For an existing sandbox: suspend it, run a throwaway pod on `dsh-home-$SB` to `chmod 600 /home/node/.dsh/.credentials.yaml`, patch the sandbox's `podTemplate` with the policy, then resume |
| Agents get `Permission denied` writing to `/workspace` on a fresh or resumed pod | The `/workspace` mount is the `dsh-home` PVC's `workspace/` subPath, and the kubelet is known to leave a `subPath` directory at `0755` if it is interrupted between creating and chmodding it ([kubernetes#142464](https://github.com/kubernetes/kubernetes/issues/142464)); later mounts never correct it. Suspend the sandbox, run a throwaway pod on `dsh-home-$SB` mounted at `/data`, `chmod 2775 /data/workspace` (do **not** chown/chmod recursively — that rewrites `.credentials.yaml`), then resume. If you recreate the sandbox instead, its current `/workspace` contents go with the PVC |
| Every bash command fails: `SANDBOX_UNAVAILABLE`, `sandbox mode "workspace-write" is requested but no sandbox backend is usable` | DSH's own command sandbox found neither backend: `bubblewrap` ships in the image but is unsetuid, so it needs an unprivileged user namespace this hardened pod does not grant, and no Landlock runner exists on this kernel/runtime — the confined executor fails closed. The template sets `DSH_PERMISSION_MODE=danger-full-access` to keep agents usable inside the pod boundary. To restore confinement, confirm a backend inside the pod (`bwrap --ro-bind / / true`; `grep -o landlock /sys/kernel/security/lsm`), set `DSH_PERMISSION_MODE=workspace-write` (or `read-only`), and recreate the pod — the value is stamped at creation. A session that explicitly chose a mode keeps that choice |
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
| Folder picker opens on `/home/node` instead of the workspace | The sandbox was created before the template set `HOME=/workspace`, and pod env is stamped at creation. Verify with `kubectl -n dsh-demo exec "$SB" -- printenv HOME`; patch the sandbox's `podTemplate` with the env (or recreate the sandbox — the PVC keeps DSH state and workspace) and resume |
| File explorer and `ls /workspace` show `.cache`, `.config`, `.local`, `.npm`, `.agents` | Expected with `HOME=/workspace`: `~`-relative tool caches and DSH's global agent instructions/skills live on the PVC inside the workspace. Remove the `HOME` env from the template (and recreate the sandbox) if you would rather keep the picker on the ephemeral image home; point caches elsewhere per tool if only they are unwanted |
| Sandbox reports `Finished=True` and every gateway request fails | The sandbox Pod reached phase `Failed` (node eviction, spot reclaim) and the default `Retain` keeps it. Delete the Pod so the controller recreates it, or set `failedPodPolicy: Recreate` (agent-sandbox v1.0.8+; see optional hardening) |

## Limitations

Deliberately **not** production code:

- **Single replica.** Idle clocks, in-flight tracking, the handshake cache and
  the browser session table are in memory. Running two gateway replicas would
  double-suspend or lose wake tracking, and sessions would land on the wrong
  replica; scale up only after moving that state to a shared store.
- **Sessions die with the process.** Browser sessions are opaque ids held in
  gateway memory (revocable on logout/delete/recreate, but not durable). A
  gateway restart logs every browser out. There is no SSO refresh-token or
  password store; identity is re-established by oauth2-proxy on the next
  request.
- **Abuse controls are per-process and coarse.** `AUTH_RATE_LIMIT`, the signup
  invite code, `MAX_IN_FLIGHT_PER_USER` and `WS_MAX_MESSAGE_BYTES` limit casual
  abuse, but they are not a substitute for ingress rate limiting, WAF/quota, or
  an audit log, and the in-memory rate buckets reset on restart.
- **Log scraping.** The launch token is recovered from pod logs. Log rotation, a
  restarted pod, or a changed DSH log format can break the handshake until the
  next `401` refresh.
- **Shared provider keys.** `DEEPSEEK_API_KEY` and any keys in the optional
  `dsh-provider-keys-extra` Secret are shared by all tenants via the template.
  Tenants can override a key through DSH's stored credential file on their PVC,
  but the platform does not manage that. Real multi-tenancy needs per-tenant
  credentials or an egress proxy.
- **No TLS in the gateway.** Terminate TLS at an Ingress or Gateway and set
  `COOKIE_SECURE=true`, then remove `ALLOW_INSECURE_COOKIES`. The base manifest
  runs plain HTTP for the port-forward demo and is demo-only.
- **SSO trust is hop-based.** `AUTH_PROXY_HEADER` trusts whoever sets the header.
  The SSO variant ships `75-networkpolicies.yaml`, which restricts the gateway
  to oauth2-proxy and oauth2-proxy egress to the gateway/DNS/public HTTPS; a
  custom exposure without that policy has no signature, mTLS or network
  enforcement. Exposure without the policy is demo-only.
- **DSH's inner file sandbox is off here.** The template sets
  `DSH_PERMISSION_MODE=danger-full-access` because neither backend of DSH's own
  command sandbox is usable in this pod: `bubblewrap` is installed in the image
  but is unsetuid, so it needs an unprivileged user namespace that the pod's
  seccomp/capability surface does not grant (`capabilities.drop: ["ALL"]`), and
  the kernel/runtime exposes no Landlock runner. The executor therefore fails
  closed with `SANDBOX_UNAVAILABLE` and every bash command fails. The per-user
  pod remains the boundary — non-root,
  capabilities dropped, `seccompProfile: RuntimeDefault`, its own PVC and Service
  — but an agent command can now read its own `DSH_HOME` (including
  `.credentials.yaml`) and write anywhere in the container. Run sandboxes under
  gVisor in production. On hosts where a backend works, set `workspace-write`
  (or `read-only`) and verify before relying on the inner policy.
- **Isolation is opt-in.** gVisor, the sandbox NetworkPolicy, the gateway egress
  policy and the exposure policies are opt-in; without them, sandboxes share the
  host kernel and can reach the cluster network. The base example is a demo, not
  a hardened multi-tenant platform. See the
  [production checklist](docs/optional-hardening.md#6-production-checklist).
- **OAuth org is the only authorization.** Any GitHub user in the configured
  organization gets an auto-provisioned sandbox; there is no per-user allow-list,
  quota, or admin API. `SIGNUP_INVITE_CODE` gates the gateway's own signup form,
  not SSO auto-provisioning.
- **In-memory user directory.** Users are claims + annotations; there is no
  database, password reset, or admin API. Tokens are stored only as SHA-256
  hashes in claim annotations.
- **Bearer tokens in URLs.** Avoid putting the signup token in a URL/log; the
  browser flow uses the `HttpOnly` cookie instead. The cookie no longer contains
  the token, and a cookie value is never accepted as a bearer token.

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
- [SSO preflight](scripts/preflight-sso.sh) — read-only checks for the SSO
  variant (Secrets, gateway env, TLS reference, placeholders, second paths).
- [Optional hardening](docs/optional-hardening.md) — gVisor, nodeSelector,
  NetworkPolicy, extra provider keys and failed-Pod recovery, with verification,
  the production checklist and rollback.
