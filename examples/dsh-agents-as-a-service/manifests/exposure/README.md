# Exposing the gateway

The base manifests deploy the gateway as a ClusterIP Service
(`dsh-gateway:8080`) and expose nothing outside the cluster. WebSocket
upgrades on `/u/<user>/**` are part of the contract, so the entry point must
forward `Upgrade`/`Connection` headers (Traefik does by default).

There are two **mutually exclusive** exposure variants. Pick exactly one:

| Variant | Entry point | Authentication | TLS |
| --- | --- | --- | --- |
| [`ingress/60-ingress.yaml`](ingress/60-ingress.yaml) | Traefik `Ingress` | the gateway's own signup/session cookie | optional, recommended |
| [`gateway-api-sso/`](gateway-api-sso/72-oauth2-proxy.yaml) | Gateway API `Gateway` + `HTTPRoute` | GitHub OAuth via oauth2-proxy, then the gateway cookie | required (the Gateway listener terminates it) |

Applying both gives the gateway two entry points, and the SSO variant is only
safe when the gateway is reachable **exclusively** through oauth2-proxy. Do not
mix them.

Each variant also ships a NetworkPolicy, applied by the same directory apply:

- `ingress/61-networkpolicy.yaml` allows only Traefik pods to reach the gateway
  on `8080`.
- `gateway-api-sso/75-networkpolicies.yaml` allows only oauth2-proxy to reach
  the gateway, only the Gateway implementation (Traefik) to reach oauth2-proxy,
  and limits oauth2-proxy egress to DNS, public HTTPS (GitHub) and the gateway.

The policies assume Traefik's `app.kubernetes.io/name: traefik` pod label.
Adjust the selector for another implementation or a `hostNetwork` install; see
[optional hardening](../../docs/optional-hardening.md) for the probe caveat and
verification commands.

## Applying a variant

Run these commands from the example root
(`examples/dsh-agents-as-a-service/`), the same working directory the example
README uses.

`kubectl apply -f manifests/` is **not recursive**: it applies only the
top-level numbered files and does not descend into `exposure/`. Apply the base
resources first, then the chosen variant explicitly:

```bash
# Base resources: namespace, template, warm pool, claims, gateway.
kubectl apply -f manifests/

# Plain Ingress exposure:
kubectl apply -f manifests/exposure/ingress/

# ... or SSO exposure (create the two Secrets and replace the example.com
# placeholders first — see below):
kubectl apply -f manifests/exposure/gateway-api-sso/
```

**Never use `kubectl apply -R`** (recursive): it would apply both variants at
once and hand out a second, unauthenticated path to the same gateway. Apply the
variant directory on purpose, or the individual files by name.

`kubectl` only reads `.yaml`, `.yml` and `.json` files from a directory, so
the `*.yaml.example` secret templates are skipped automatically; create the real
Secrets imperatively (commands below) before applying the directory.

## `ingress/`: plain Ingress, no SSO

`60-ingress.yaml` routes a host through the cluster's Traefik
(`ingressClassName: traefik`; k3s ships one) to the gateway Service.

1. Replace the `dsh.example.com` host and the `dsh-example-tls` Secret name
   with real values, or delete the `tls:` block to serve plain HTTP.
2. Set `DSH_TRUSTED_HOSTS` in the SandboxTemplate to the same public host:
   the gateway forwards the browser's `Host`, and DSH rejects unknown hosts on
   `/api` (see `../10-sandboxtemplate.yaml`).
3. Set `COOKIE_SECURE=true` on the gateway once the Ingress serves HTTPS:
   `kubectl -n dsh-demo set env deploy/dsh-gateway COOKIE_SECURE=true`. The
   base manifest keeps `false` so the plain-HTTP port-forward demo works; a
   `Secure` cookie is never sent over HTTP, which silently breaks login.
4. Do not attach buffering or compression middlewares to this router: they
   break WebSocket upgrades and streaming.
5. `61-networkpolicy.yaml` is part of this variant: it allows only Traefik pods
   to reach the gateway on `8080`. Adjust its selector for another ingress
   controller or `hostNetwork`; if kubelet probes fail, see the hardening doc.

Verify: `curl -sI https://<host>/healthz` returns `200`.

## `gateway-api-sso/`: TLS + GitHub OAuth

`70-gateway.yaml` defines the TLS `Gateway`, `71-httproute.yaml` sends every
request to oauth2-proxy, and `72-oauth2-proxy.yaml` authenticates the browser
against GitHub and proxies to the gateway with `X-Forwarded-User` set.
`75-networkpolicies.yaml` makes this the only network path to the gateway.

Prerequisites:

- Gateway API CRDs and a Gateway implementation installed;
  `gatewayClassName: traefik` matches the Traefik Gateway provider. The CRD
  version is pinned in `versions.env` (`GATEWAY_API_VERSION`).
- The OAuth App credentials and a cookie secret, from
  `73-oauth2-proxy-secret.yaml.example`.
- A TLS certificate/key for the public host, from
  `74-tls-secret.yaml.example`.
- oauth2-proxy is pinned in the manifest to
  `quay.io/oauth2-proxy/oauth2-proxy:v7.15.5` (`versions.env`:
  `OAUTH2_PROXY_IMAGE`).

Steps:

```bash
# 1. Real Secrets (the committed files are *.yaml.example templates).
kubectl -n dsh-demo create secret generic oauth2-proxy \
  --from-literal=client-id='<GitHub OAuth App client ID>' \
  --from-literal=client-secret='<GitHub OAuth App client secret>' \
  --from-literal=cookie-secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
kubectl -n dsh-demo create secret tls dsh-demo-tls --cert=tls.crt --key=tls.key

# 2. Replace dsh-demo.example.com (70/71/72) and your-github-org (72) with
#    the real public host and allowed GitHub organization.

# 3. Enable SSO on the gateway and trust the public host in the template.
kubectl -n dsh-demo set env deploy/dsh-gateway \
  AUTH_PROXY_HEADER=X-Forwarded-User COOKIE_SECURE=true
# then set DSH_TRUSTED_HOSTS=dsh-demo.example.com in the SandboxTemplate
# (see ../10-sandboxtemplate.yaml) and re-apply it.

# 4. Apply the variant.
kubectl apply -f manifests/exposure/gateway-api-sso/
```

### SSO trust boundary

`AUTH_PROXY_HEADER` is opt-in and deliberately unset in the base gateway
manifest: the gateway ignores identity headers until it is set. Enabling SSO
takes all of these:

1. `AUTH_PROXY_HEADER=X-Forwarded-User` on `deploy/dsh-gateway` (step 3
   above). Without it, oauth2-proxy's login has no effect on the gateway and
   users are still asked for the gateway's own token.
2. The gateway Service must be reachable **only through oauth2-proxy**: keep it
   ClusterIP, route all browser traffic through the `HTTPRoute` →
   oauth2-proxy → `dsh-gateway` chain, and never apply `ingress/` (or any
   other path to the Service) while SSO is enabled. A client that can reach
   `dsh-gateway:8080` directly can set `X-Forwarded-User` itself and
   impersonate any user.
3. Keep `75-networkpolicies.yaml` with the variant. It enforces the boundary in
   the network layer (gateway ← oauth2-proxy ← Traefik, and limited oauth2-proxy
   egress). A custom exposure without an equivalent policy has no network
   enforcement.

Also terminate TLS in front of the gateway (the Gateway listener does) and
leave `COOKIE_SECURE=true` so the session cookie never travels in clear text.

## Switching variants

Delete the variant you had before applying the other one, e.g.
`kubectl delete -f manifests/exposure/ingress/`, and revert
`AUTH_PROXY_HEADER`/`COOKIE_SECURE` on the gateway. Verify with
`kubectl -n dsh-demo get ingress,gateway,httproute` that exactly one path to
the gateway remains.
