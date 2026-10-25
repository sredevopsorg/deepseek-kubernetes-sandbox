# DSH agents-as-a-service gateway

A single-file, browser-usable control + data plane for
[DeepSeek Harness Web (DSH)](https://github.com/deepseek-ai/deepseek-harness)
sandboxes running behind the `agent-sandbox` CRDs.

It does four things:

1. **Users** — creates a `SandboxClaim` from a warm pool, mints a bearer token
   (only its SHA-256 is stored as a claim annotation), reports derived state,
   and deletes the claim (cascading the sandbox and PVC).
2. **Browser sessions** — exchanges a user token for an HttpOnly session
   cookie so the DSH UI works in a browser at `/u/<user>/`. Automation can
   still use `Authorization: Bearer <token>`.
3. **Reverse proxy** — proxies HTTP (including SSE) and WebSocket traffic to
   the sandbox's DSH Web server on `:3080`, including DSH's own cookie
   handshake.
4. **Optional SSO** — when `AUTH_PROXY_HEADER` is set, trusts the identity
   header an authenticating reverse proxy (for example oauth2-proxy) forwards,
   auto-provisions that user, and exposes `POST /recreate` for a clean restart.

The DSH version pin lives in [`versions.env`](../../../versions.env)
(`DSH_VERSION` plus the optional repository `RELEASE_REVISION`); the example
manifests and published images use the release identity built from it (currently
`dsh-v0.2.1-rc.2-r2`). DSH binds `:3080`, serves `/`, gates `/api` behind a
`dsh-auth-*` cookie and requires a WebSocket at `/api/remote.mux`.

## Flow

```
browser / curl                    gateway                         cluster
    |                               |                                |
    |  POST /users {"user":"alice"} |                                |
    |------------------------------>| create SandboxClaim ---------->|
    |                               | wait Ready                    |
    |  201 {token} + session cookie |                                |
    |<------------------------------|                                |
    |  GET /u/alice/ (cookie)       |                                |
    |------------------------------>| claim -> sandbox -> pod        |
    |                               | read pod logs (pods/log)       |
    |                               | find "dsh web: ...?token=..."  |
    |                               | GET http://<fqdn>:3080/?token= |
    |                               | capture dsh-auth-* Set-Cookie  |
    |                               | proxy with Host/Origin kept    |
    |  HTML / SSE / WebSocket       |<----------------------------->|
    |<------------------------------|                                |
```

The launch token is printed by DSH to its pod stdout:

```
dsh web: http://<podIP>:3080/?token=<base64url>
```

The gateway reads the tail of the pod logs, parses the **last** such line and
performs the `?token=` handshake itself with the browser's `Host` header. The
resulting `dsh-auth-*` cookie is cached per `(user, pod UID, Host)` and injected
into proxied requests. The browser never sees the launch token or the bearer
token. A failed handshake returns `503` with `Retry-After: 10`.

## API

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/healthz` | none | Liveness/readiness. Never touches the cluster. |
| `GET` | `/` | none | Landing page with signup + token login forms. With SSO it auto-provisions the proxy-identified user and redirects to their workspace. |
| `POST` | `/users` | none | Signup. JSON body `{"user": "<dns-1123 label>"}` or form field `user`. JSON returns `201` when Ready, `202` when accepted but still Waking; form returns `303` to `/u/<user>/`. Both set the session cookie. `409` if the user exists, `400` if invalid, `413` if the body is too large. |
| `POST` | `/login` | none | Form (`user`, `token`) returns `303` to `/u/<user>/`; JSON returns `200`. Sets the session cookie. `401` on a bad token, `404` for an unknown user. |
| `POST` | `/logout` | none | Clears the session cookie and `303`s to `/`. |
| `POST` | `/recreate` | trusted proxy header | **SSO only.** Deletes the authenticated user's claim (cascading sandbox + PVC, so workspace data is wiped), waits out finalizers, provisions a fresh claim and `303`s to `/u/<user>/`. `401` without a valid identity header; cookie-authenticated calls must be same-origin. |
| `GET` | `/users/{user}` | bearer or cookie | Derived state: `Provisioning`, `Waking`, `Ready`, `Suspending`, `Suspended`. |
| `DELETE` | `/users/{user}` | bearer or cookie | Delete the claim (sandbox + PVC cascade). Cookie-auth mutations require a same-origin `Origin`/`Referer`. |
| `GET` | `/u/{user}` | — | `308` redirect to `/u/{user}/`. |
| `*` | `/u/{user}/{path}` | bearer or cookie | HTTP proxy to DSH. Waits for a suspended sandbox to wake. Wakes on connect. |
| `WS` | `/u/{user}/{path}` | bearer or cookie | WebSocket proxy (e.g. `/api/remote.mux`). |

### Auth

* `Authorization: Bearer <token>` is checked with a timing-safe SHA-256
  comparison against the `aaas.example.com/token-sha256` claim annotation.
* The browser session cookie is
  `dsh_gw_session=<user>:<token>` (HttpOnly, SameSite=Lax, Path=/, optional
  Secure). A session for user A never authorizes user B.
* Cookie-authenticated **mutations** (`POST`, `PUT`, `PATCH`, `DELETE`) and
  cookie-authenticated **WebSocket upgrades** require the request `Origin`
  (or `Referer` fallback) to match the `Host` header. Missing or mismatched
  cross-origin metadata is rejected (`403` HTTP, close code `4401` for WS).
* The gateway's bearer token, the browser `Cookie` header and any `?token=`
  query parameter are never forwarded upstream. Only the cached `dsh-auth-*`
  cookie is injected.
* Upstream `Set-Cookie` headers are dropped. `Location` headers that are
  absolute paths are rewritten under `/u/<user>/`, and any `token` query
  parameter is stripped.

### SSO (opt-in)

By default the gateway is the only authenticator: users sign up with a token or
the session cookie, and the gateway ignores all identity headers. Set
`AUTH_PROXY_HEADER` to enable SSO behind an authenticating reverse proxy such as
oauth2-proxy:

```bash
kubectl -n dsh-demo set env deploy/dsh-gateway AUTH_PROXY_HEADER=X-Forwarded-User
```

When set, the gateway treats a request carrying that header as authenticated as
that user, provided the value is a valid DNS-1123 label; the header is never
trusted when the variable is unset. The landing page then auto-provisions the
user (creates their claim idempotently, mints a rotated session token) and
redirects to `/u/<user>/`, and `POST /recreate` becomes available for a clean
restart. The base manifests leave it unset on purpose; the
[SSO exposure variant](../manifests/exposure/gateway-api-sso/) documents how to
enable it on the Deployment (`kubectl set env`).

> **Trust boundary — this is the security-critical part.** A trusted identity
> header is only safe when **nothing else can reach the gateway**. The gateway
> Service must stay ClusterIP and be reachable exclusively through the
> authenticating proxy; if a client can reach `dsh-gateway:8080` directly (a
> second Ingress/Gateway/Route, a port-forward on a shared host, a broad
> NetworkPolicy), it can send `X-Forwarded-User` itself and impersonate any user.
> Treat `AUTH_PROXY_HEADER` as "the proxy already authenticated this request",
> not as an authentication mechanism of its own. Disable SSO by removing the
> variable: the header is then ignored and bearer-token/session auth applies
> again.

### Proxy failure semantics

| Situation | Result |
| --- | --- |
| Unknown user / claim | `404` |
| Missing or invalid credentials | `401` |
| Cross-origin cookie mutation | `403` |
| Request body over `MAX_BODY_MB` | `413` |
| Sandbox not provisioned, wake timeout, handshake failure | `503` + `Retry-After: 10` |
| Upstream connection/protocol failure | `502` |
| WS auth failure (unknown user, bad token, cross-origin) | accept, then close `4401` |
| WS upstream/handshake failure | accept, then close `1011` |

A close code can only be delivered after the WebSocket upgrade is accepted;
Starlette would turn an early close into an HTTP 403, which is why WS auth
failures are `101`, then close `4401`.

## Configuration

All values are read from the environment by `Settings.from_env()`; every one
can also be injected into `create_app` for tests.

| Variable | Default | Meaning |
| --- | --- | --- |
| `NAMESPACE` | `dsh-demo` | Namespace holding the claims, sandboxes and pods. |
| `POOL` | `dsh-pool` | `warmPoolRef.name` for new claims. |
| `IDLE_TIMEOUT` | `60` | Seconds of inactivity before the sweeper suspends a sandbox. Only claims the gateway created (annotation `aaas.example.com/managed-by=dsh-gateway`) are ever suspended. |
| `WAKE_TIMEOUT` | `120` | Seconds to wait for a sandbox to become Ready (signup and wake-on-connect). |
| `DSH_PORT` | `3080` | Upstream DSH port. |
| `DSH_CONTAINER` | `dsh-web` | Container whose logs carry the launch token. |
| `DSH_LOG_TAIL_LINES` | `2000` | How many log lines to scan for the launch token. |
| `COOKIE_NAME` | `dsh_gw_session` | Browser session cookie name. |
| `COOKIE_SECURE` | `false` | Add `Secure` to the session cookie when served over TLS. |
| `COOKIE_MAX_AGE` | `604800` | Session cookie lifetime in seconds. |
| `MAX_BODY_MB` | `32` | Maximum buffered request body (and `413` threshold). |
| `HTTP_TIMEOUT` | `300` | Upstream connect/read timeout in seconds. Proxied streams (SSE, long responses) keep the connect/write budget but have **no read timeout**, so an idle event stream is not cut every five minutes. |
| `SWEEP_INTERVAL` | `15` | Seconds between idle sweeps. |
| `AUTH_PROXY_HEADER` | unset | Opt-in SSO: name of the trusted identity header set by an authenticating reverse proxy (e.g. `X-Forwarded-User`). Unset disables SSO and the header is never trusted. Enable only when the Service is reachable exclusively through that proxy (see [SSO](#sso-opt-in)). |

### Required RBAC

The gateway's ServiceAccount needs, in its namespace:

* `sandboxclaims` (`extensions.agents.x-k8s.io`): get, list, create, delete
* `sandboxes` (`agents.x-k8s.io`): get, patch
* `pods`: get
* `pods/log`: get

## Running locally

The module has no import-time cluster access and the Kubernetes adapter is
lazy, so `/healthz` works without a kubeconfig:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .

# Uvicorn factory (loads in-cluster credentials only on the first claim call)
uvicorn gateway:create_app --factory --host 0.0.0.0 --port 8080
```

Container image, from the repository root:

```bash
make gateway    # builds dsh-aaas-gateway:dev from this directory
```

The equivalent direct build is
`docker build -t dsh-aaas-gateway:dev examples/dsh-agents-as-a-service/gateway`.
For kind, retag it to the image reference in
`examples/dsh-agents-as-a-service/manifests/50-gateway.yaml` and `kind load` it.
Published images are nested below the main image as
`<GHCR_IMAGE>/dsh-agents-as-a-service/gateway:<identity>`, where the identity
comes from [`versions.env`](../../../versions.env) — see
[`docs/versioning.md`](../../../docs/versioning.md).

The image runs as UID `10000`, sets `PYTHONDONTWRITEBYTECODE=1` and
`HOME=/tmp` (read-only-rootfs compatible), listens on `8080`, and disables
uvicorn's access log so bearer tokens in URLs are not logged.

## Known limitations

* **Log scraping is fragile.** The launch token is discovered from pod logs.
  Log rotation, a very chatty container, or DSH changing its startup message
  breaks the handshake. Prefer a first-class token/credential API when DSH
  offers one.
* **Single replica, in-memory state.** The handshake cookie cache, idle
  activity clocks and in-flight counters live in one process. Running more
  than one replica would need shared state (or sticky routing), otherwise each
  replica handshakes independently and the sweeper may race another replica.
* **In-memory token cache has no TTL.** It is keyed by pod UID, so pod
  restarts invalidate it naturally; otherwise entries survive until the pod or
  claim is deleted.
* **No TLS termination.** Run behind an ingress/load balancer; set
  `COOKIE_SECURE=true` there.
* **Origin-based CSRF protection only.** Cookie-auth mutations require a
  same-origin `Origin`/`Referer`; there is no per-form CSRF token, rate
  limiting or login throttling.
* **HTTP request bodies are buffered** up to `MAX_BODY_MB`; uploads are not
  streamed. Responses (including SSE) are streamed.
* **Upstream cookies are not forwarded.** DSH cannot rotate its own
  `dsh-auth-*` cookie through the gateway; on an upstream `401` the gateway
  re-runs the handshake once and replays the request.
* **Only DSH on `:3080` is proxied.** There is no dashboard/API-port split and
  no platform API key injection.
* **SSO trusts a header.** With `AUTH_PROXY_HEADER` set, any client that can
  reach the gateway Service directly can spoof the identity header. The gateway
  cannot enforce the trust boundary itself: keep the Service ClusterIP and
  reachable only through the authenticating proxy. There is no mTLS or signed
  identity assertion.
