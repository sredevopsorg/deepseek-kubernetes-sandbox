# Gateway API — external access for `dsh-demo/dsh-gateway` (GitHub SSO)

Exposes the `dsh-gateway` service (`dsh-demo`, port `8080`) outside the cluster
using the Kubernetes Gateway API (Traefik Gateway controller), fronted by
**oauth2-proxy** so access requires a GitHub login restricted to a configured
GitHub organization. The gateway inherits the authenticated GitHub username via
the `X-Forwarded-User` header (SSO).

## Request flow

```
Browser ──▶ Gateway dsh-gateway (HTTPS, TLS-terminate)
  ──▶ HTTPRoute <your-hostname>
    ──▶ oauth2-proxy:4180   (GitHub OAuth, org-restricted)
      ──▶ dsh-gateway:8080  (X-Forwarded-User = GitHub login → SSO)
```

## Resources

| File | Kind | Purpose |
|---|---|---|
| `gateway.yaml` | `Gateway` | HTTPS listener with TLS termination |
| `httproute.yaml` | `HTTPRoute` | Routes the hostname → `oauth2-proxy:4180` |
| `oauth2-proxy.yaml` | `Deployment`/`Service` | GitHub OAuth reverse proxy |
| `oauth2-proxy-secret.yaml.example` | `Secret` | Example OAuth credentials (not applied as-is) |
| `tls-secret.yaml.example` | `Secret` | Example TLS cert secret (not applied as-is) |

## Prerequisites

1. **GitHub OAuth App** — create one at
   `https://github.com/settings/developers` with:
   - Homepage URL: `https://dsh-demo.example.com`
   - Authorization callback URL: `https://dsh-demo.example.com/oauth2/callback`
   (replace `dsh-demo.example.com` with the actual public FQDN). Note the client
   ID and client secret.
2. **oauth2-proxy Secret** — `kubectl -n dsh-demo create secret generic oauth2-proxy`
   with `client-id`, `client-secret`, and a `cookie-secret` (URL-safe, unpadded
   base64 of 32 bytes: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`).
   See `oauth2-proxy-secret.yaml.example`.
3. **TLS secret** — create `dsh-demo-tls` in `dsh-demo` (see `tls-secret.yaml.example`).
4. **Gateway app env** — the `dsh-gateway` Deployment must run the SSO-capable
   image with `AUTH_PROXY_HEADER=X-Forwarded-User` and `COOKIE_SECURE=true`
   (see `examples/dsh-agents-as-a-service/50-gateway.yaml`).
5. **Ingress and DNS** — configure the upstream ingress and public DNS for the
   hostname.

## Apply

```bash
kubectl diff -f gateway-api/dsh-demo/
kubectl apply -f gateway-api/dsh-demo/ --dry-run=server
kubectl apply -f gateway-api/dsh-demo/
```

> ⚠️ A write-enabled kubeconfig is required to create Gateways, HTTPRoutes,
> Deployments, and Secrets.

## Verify

```bash
kubectl get gateway dsh-gateway -n dsh-demo -o yaml        # expect Programmed=True
kubectl get httproute -n dsh-demo -o wide                  # backend oauth2-proxy:4180, Accepted
kubectl get deploy oauth2-proxy -n dsh-demo                # 1/1 Ready
kubectl get endpoints oauth2-proxy -n dsh-demo             # backend present
kubectl -n dsh-demo logs deploy/oauth2-proxy --tail=50     # no config errors
```

Browser check: open `https://<your-hostname>/` — you should be redirected
to GitHub, and after signing in as a member of the allowed organization your
sandbox opens with the session showing your GitHub login. A non-member is denied
before reaching the gateway.

## Rollback

```bash
kubectl delete -f gateway-api/dsh-demo/
```

## Notes

- **SSO is opt-in and additive.** The gateway honors `X-Forwarded-User` only when
  `AUTH_PROXY_HEADER` is set; unset it to restore bearer-token/session-only auth.
- **Trust boundary.** After this change the `dsh-gateway` Service must be reachable
  **only** through oauth2-proxy (it is a ClusterIP Service). A second Route/Ingress
  that exposes `dsh-gateway:8080` directly would let clients spoof the header.
- **GitHub login → DSH user.** GitHub usernames are lowercase alphanumerics and
  hyphens, so they map 1:1 onto the gateway's dns-1123 `user` validation.
- **Sign-out.** The gateway `/logout` clears `dsh_gw_session`; oauth2-proxy's own
  session can be cleared at `/oauth2/sign_out` if desired.
- No `ReferenceGrant` is required: the Gateway, HTTPRoute, oauth2-proxy, and the
  backend Service are all in `dsh-demo`.
