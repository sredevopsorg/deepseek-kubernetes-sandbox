# Gateway API — external access for `dsh-demo/dsh-gateway`

Exposes the `dsh-gateway` service (`dsh-demo`, port `8080`) outside the cluster
using the Kubernetes Gateway API (Traefik Gateway controller).

## Resources

| File | Kind | Purpose |
|---|---|---|
| `gateway.yaml` | `Gateway` | HTTPS listener with TLS termination |
| `httproute.yaml` | `HTTPRoute` | Routes the hostname → `dsh-gateway:8080` |
| `tls-secret.yaml.example` | `Secret` | Example TLS cert secret (not applied as-is) |

## Prerequisites

1. **Hostname** — replace the placeholder in `httproute.yaml` with the real FQDN.
2. **TLS secret** — create `dsh-demo-tls` in `dsh-demo` (see `tls-secret.yaml.example`).
3. **Ingress and DNS** — configure the upstream ingress and public DNS for the hostname.

## Apply

```bash
kubectl diff -f gateway-api/dsh-demo/
kubectl apply -f gateway-api/dsh-demo/ --dry-run=server
kubectl apply -f gateway-api/dsh-demo/
```

> ⚠️ A write-enabled kubeconfig is required to create Gateways and HTTPRoutes.

## Verify

```bash
kubectl get gateway dsh-gateway -n dsh-demo -o yaml   # expect Programmed=True
kubectl get httproute -n dsh-demo -o wide             # expect Accepted
kubectl get endpoints dsh-gateway -n dsh-demo         # backend present
kubectl -n dsh-demo exec deploy/dsh-gateway -- wget -qO- http://localhost:8080/healthz
```

## Rollback

```bash
kubectl delete -f gateway-api/dsh-demo/
```

## Notes

- No `ReferenceGrant` is required: the Gateway, HTTPRoute, and backend Service are
  all in `dsh-demo`.
- The backend (`dsh-gateway`) serves plain HTTP on 8080 and has a `/healthz` probe.
