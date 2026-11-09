# Exposing the gateway

The base manifests (`manifests/base/`) deploy the gateway as a ClusterIP
Service (`dsh-gateway:8080`) and expose nothing outside the cluster. WebSocket
upgrades on `/u/<user>/**` are part of the contract, so the entry point must
forward `Upgrade`/`Connection` headers (Traefik does by default).

There are two **mutually exclusive** exposure variants. Pick exactly one:

| Variant | Entry point | Authentication | TLS |
| --- | --- | --- | --- |
| [`ingress/`](ingress/kustomization.yaml) | Traefik `Ingress` | the gateway's own signup/session cookie | optional, recommended |
| [`gateway-api-sso/`](gateway-api-sso/kustomization.yaml) | Gateway API `Gateway` + `HTTPRoute` | GitHub OAuth via oauth2-proxy, then the gateway cookie | required (the Gateway listener terminates it) |

Applying both gives the gateway two entry points, and the SSO variant is only
safe when the gateway is reachable **exclusively** through oauth2-proxy. Do not
mix them.

> **Production requirement.** Each variant's NetworkPolicy is part of the
> variant, and applying the variant without it is demo-only. The sandbox
> NetworkPolicy and the gateway egress policy (the API server is cluster-specific)
> are separate and equally required; see the
> [production checklist](../../docs/optional-hardening.md#6-production-checklist)
> and the
> [gateway egress recipe](../../docs/optional-hardening.md#gateway-egress-required-for-production).
> For SSO, run [`scripts/preflight-sso.sh`](../../scripts/preflight-sso.sh)
> after applying the variant and before sending real traffic.

Each variant also ships a NetworkPolicy, applied by the same overlay:

- `ingress/61-networkpolicy.yaml` allows only Traefik pods to reach the gateway
  on `8080`.
- `gateway-api-sso/75-networkpolicies.yaml` allows only oauth2-proxy to reach
  the gateway, only the Gateway implementation (Traefik) to reach oauth2-proxy,
  and limits oauth2-proxy egress to DNS, public HTTPS (GitHub) and the gateway.

The policies assume Traefik's `app.kubernetes.io/name: traefik` pod label.
Adjust the selector for another implementation or a `hostNetwork` install; see
[optional hardening](../../docs/optional-hardening.md) for the probe caveat and
verification commands. Keep the policies: an exposure without its NetworkPolicy
is demo-only.

## What each overlay does

Both variants are [Kustomize](https://kustomize.io/) overlays. Each
`kustomization.yaml` **includes the portable base** (`../../base/`), so one
`kubectl apply -k` creates the base platform and the variant together, and each
overlay also patches the base so the entry point and the platform agree:

| Overlay | Adds | Patches |
| --- | --- | --- |
| `ingress/` | `60-ingress.yaml`, `61-networkpolicy.yaml` | gateway `COOKIE_SECURE="true"` and removes `ALLOW_INSECURE_COOKIES`; copies the Ingress rule host into the TLS SAN and into the SandboxTemplate `DSH_TRUSTED_HOSTS` |
| `gateway-api-sso/` | `70-gateway.yaml`, `71-httproute.yaml`, `72-oauth2-proxy.yaml`, `75-networkpolicies.yaml` | gateway `AUTH_PROXY_HEADER=X-Forwarded-User`, `COOKIE_SECURE="true"`, `TRUSTED_PROXY_HOPS="2"` and removes `ALLOW_INSECURE_COOKIES`; copies the HTTPRoute hostname into the SandboxTemplate `DSH_TRUSTED_HOSTS` |

Consequences:

- **No manual `kubectl set env` and no hand-editing `DSH_TRUSTED_HOSTS`.** The
  public host lives in exactly one place per variant (`60-ingress.yaml`'s rule
  host, or `71-httproute.yaml`'s `hostnames`); the overlay propagates it.
- The base `../base/50-gateway.yaml` stays the plain-HTTP port-forward demo (`COOKIE_SECURE=false`
  + `ALLOW_INSECURE_COOKIES=1`); only the overlays turn on the HTTPS settings.
- Preview a variant without a cluster with
  `kubectl kustomize manifests/exposure/ingress` (or `.../gateway-api-sso`) and
  read the rendered stream. That is also the review artifact for a change.
- The `*.yaml.example` Secret templates are **not** kustomization resources, so
  they are never applied. Create the real Secrets imperatively (commands below);
  the placeholders (`dsh.example.com`, `dsh-demo.example.com`, `your-github-org`)
  stay in the variant YAMLs until you replace them.

## Applying a variant

Run these commands from the example root
(`examples/dsh-agents-as-a-service/`), the same working directory the example
README uses.

Apply exactly the chosen overlay; it includes the base resources:

```bash
# Plain Ingress exposure:
kubectl apply -k manifests/exposure/ingress

# ... or SSO exposure (create the two Secrets and replace the example.com
# placeholders first — see below):
kubectl apply -k manifests/exposure/gateway-api-sso
```

Preview what an overlay will change with
`kubectl diff -k manifests/exposure/<variant>` (needs a cluster); the
cluster-free equivalent is `kubectl kustomize <variant>`.

Do not apply both variants: that hands out a second, unauthenticated path to
the same gateway. Applying `manifests/base` separately is allowed (it is the
same objects) but unnecessary: every overlay already includes it.

## `ingress/`: plain Ingress, no SSO

`60-ingress.yaml` routes a host through the cluster's Traefik
(`ingressClassName: traefik`; k3s ships one) to the gateway Service.

1. Change the public host in **one place only**: the rule host in
   `ingress/60-ingress.yaml`. The overlay copies it into the Ingress TLS SAN and
   into the SandboxTemplate `DSH_TRUSTED_HOSTS` (DSH fences `/api` on
   Host/Origin; see `../base/10-sandboxtemplate.yaml`). Replace the
   `dsh-example-tls` Secret name with your TLS Secret as well.
2. Apply the overlay (above). It also sets `COOKIE_SECURE="true"` and removes
   `ALLOW_INSECURE_COOKIES` on `deploy/dsh-gateway`, because a `Secure` session
   cookie must not travel over HTTP.
3. Serving **plain HTTP only** (local demo): delete the `tls:` block from
   `60-ingress.yaml` **and** the Ingress TLS replacement target in
   `ingress/kustomization.yaml`; then set `COOKIE_SECURE` back to `"false"` and
   replace the `ALLOW_INSECURE_COOKIES` `$patch: delete` entry with
   `value: "1"` in that patch. Never do this on an internet-facing deployment:
   the session cookie would travel in clear text.
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

- The `dsh-demo` namespace. The overlay's `apply -k` creates it with the
  base, but the Secrets must exist before the oauth2-proxy pod can start: the
  simplest order is `kubectl create namespace dsh-demo`, create both Secrets,
  then apply the overlay. If the namespace already exists (base applied), skip
  the first command.
- Gateway API CRDs and a Gateway implementation installed;
  `gatewayClassName: traefik` matches the Traefik Gateway provider. The CRD
  version is pinned in `versions.env` (`GATEWAY_API_VERSION`).
- The OAuth App credentials and a cookie secret, from
  `gateway-api-sso/73-oauth2-proxy-secret.yaml.example`.
- A TLS certificate/key for the public host, from
  `gateway-api-sso/74-tls-secret.yaml.example`.
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

# 2. Replace the placeholders that stay in the variant YAMLs:
#    dsh-demo.example.com (70-gateway certificate host, 71-httproute hostnames,
#    72 --redirect-url) and your-github-org (72 --github-org).
#    The overlay copies the HTTPRoute hostname into DSH_TRUSTED_HOSTS.

# 3. Apply the overlay: it includes the base, enables SSO on the gateway
#    (AUTH_PROXY_HEADER=X-Forwarded-User, COOKIE_SECURE="true",
#    TRUSTED_PROXY_HOPS="2", ALLOW_INSECURE_COOKIES removed) and applies the
#    NetworkPolicies. No manual `kubectl set env` is needed.
kubectl apply -k manifests/exposure/gateway-api-sso

# 4. Preflight: fails loudly on missing Secrets, a bad oauth2-proxy cookie
#    secret, missing gateway SSO env, a dangling TLS reference, leftover
#    placeholders, a second path to the gateway, a Role without `patch` on
#    sandboxclaims, or missing SSO NetworkPolicies; warns when
#    TRUSTED_PROXY_HOPS is unset or 0.
./scripts/preflight-sso.sh
```

The preflight is read-only and exits non-zero on any failure, naming the exact
fix. Run it again after rotating a Secret or changing the public host; it also
verifies that the oauth2-proxy cookie secret is a valid AES key length (16, 24
or 32 raw bytes, or URL-safe unpadded base64 that decodes to one of those).

### SSO trust boundary

`AUTH_PROXY_HEADER` is opt-in and deliberately unset in the base gateway
manifest: the gateway ignores identity headers until it is set. The
`gateway-api-sso/` overlay is the supported way to set it; enabling SSO takes
all of these:

1. `AUTH_PROXY_HEADER=X-Forwarded-User` on `deploy/dsh-gateway`, rendered by
   the overlay. Without it, oauth2-proxy's login has no effect on the gateway
   and users are still asked for the gateway's own token.
2. The gateway Service must be reachable **only through oauth2-proxy**: keep it
   ClusterIP, route all browser traffic through the `HTTPRoute` →
   oauth2-proxy → `dsh-gateway` chain, and never apply `ingress/` (or any
   other path to the Service) while SSO is enabled. A client that can reach
   `dsh-gateway:8080` directly can set `X-Forwarded-User` itself and
   impersonate any user.
3. Keep `75-networkpolicies.yaml` with the variant. It enforces the boundary in
   the network layer (gateway ← oauth2-proxy ← the Gateway implementation's
   namespace, and limited oauth2-proxy egress). The shipped
   `oauth2-proxy-ingress` names the `traefik` namespace, not every namespace;
   adjust the selector to wherever your Gateway implementation runs. This is
   required, not optional: an exposure without the policy is demo-only, and a
   custom exposure without an equivalent policy has no network
   enforcement.
4. Apply the gateway egress policy too: the gateway talks to the Kubernetes API
   and to sandbox pods, and `75-networkpolicies.yaml` intentionally leaves that
   direction unrestricted because the API-server endpoint is cluster-specific.
   See
   [gateway egress](../../docs/optional-hardening.md#gateway-egress-required-for-production).
5. `TRUSTED_PROXY_HOPS` on `deploy/dsh-gateway` is set to `2` by the overlay,
   the number of proxies that append to `X-Forwarded-For` between the browser
   and the gateway (Traefik + oauth2-proxy, plus any CDN/tunnel in front that
   appends rather than replaces). Add that extra hop in
   `gateway-api-sso/kustomization.yaml` if you front Traefik with one. Without
   it the auth rate limit keys on the oauth2-proxy pod, so every user shares
   one bucket and a single client can `429` the signup/login routes for
   everyone. `scripts/preflight-sso.sh` warns when it is unset.

The shipped oauth2-proxy skips authentication for `/healthz` and `/readyz`
(`--skip-auth-route=^/(healthz|readyz)$`), so in-cluster and external probes can
use them. They expose no user data: `/healthz` is cluster-free and `/readyz`
returns only `200`/`503` plus the gateway's Kubernetes API status. Drop that
argument if health checks are only done in-cluster.

Also terminate TLS in front of the gateway (the Gateway listener does) and
leave `COOKIE_SECURE=true` so the session cookie never travels in clear text.
The overlay removes `ALLOW_INSECURE_COOKIES`, which is the base demo's
plain-HTTP acknowledgement and must not survive into production.

## Switching variants

Delete the old variant's plain YAML files by name, then apply the other
overlay. Do **not** use `kubectl delete -k <dir>`: the rendered overlay also
contains the base resources (namespace, template, warm pool, claims, gateway),
so `delete -k` would tear down the whole platform instead of one entry point.

```bash
# ingress -> SSO:
kubectl delete -f manifests/exposure/ingress/60-ingress.yaml \
                -f manifests/exposure/ingress/61-networkpolicy.yaml
kubectl apply -k manifests/exposure/gateway-api-sso

# SSO -> ingress:
kubectl delete -f manifests/exposure/gateway-api-sso/70-gateway.yaml \
                -f manifests/exposure/gateway-api-sso/71-httproute.yaml \
                -f manifests/exposure/gateway-api-sso/72-oauth2-proxy.yaml \
                -f manifests/exposure/gateway-api-sso/75-networkpolicies.yaml
kubectl apply -k manifests/exposure/ingress
```

The gateway env needs no manual revert: each overlay renders the complete
desired env, so `kubectl apply -k` reconciles `AUTH_PROXY_HEADER`,
`COOKIE_SECURE`, `TRUSTED_PROXY_HOPS` and `ALLOW_INSECURE_COOKIES` to the new
variant (the same `apply -k` also restores `DSH_TRUSTED_HOSTS` from the new
variant's host). If a previous install enabled SSO with a manual
`kubectl set env`, drop those values once with
`kubectl -n dsh-demo set env deploy/dsh-gateway AUTH_PROXY_HEADER- TRUSTED_PROXY_HOPS-`.

Verify with `kubectl -n dsh-demo get ingress,gateway,httproute` that exactly
one path to the gateway remains, and re-run `./scripts/preflight-sso.sh` after
switching to SSO.
