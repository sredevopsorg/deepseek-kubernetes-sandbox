# Optional manifests

Opt-in manifests that are **not** part of the top-level walkthrough.
`kubectl apply -f manifests/` is non-recursive, so it does not apply this
directory; apply a subdirectory explicitly.

## network-policy/

A Kubernetes `NetworkPolicy` for the DSH sandboxes:

- ingress only from the gateway pods on TCP `3080` (HTTP + WebSocket);
- egress only to DNS and public HTTPS (provider APIs, package mirrors,
  git-over-HTTPS).

Apply and delete it explicitly:

```bash
kubectl apply -f manifests/optional/network-policy/
kubectl delete -f manifests/optional/network-policy/
```

It requires a CNI that enforces `NetworkPolicy`. The selector uses the
agent-sandbox controller's pod tracking label, so the policy applies to
existing warm-pool and claimed sandboxes without recreation.

**This is opt-in for the demo but required for production.** Without it every
tenant can reach every other tenant and cluster-internal endpoints. Production
also needs the gateway egress policy (cluster-specific API-server endpoint) and
the exposure variant's policy; see the
[production checklist](../../docs/optional-hardening.md#6-production-checklist).

## gVisor, nodeSelector and provider keys

These are configured in the main manifests or with an extra Secret:

- **gVisor:** uncomment `runtimeClassName: gvisor` in the SandboxTemplate and
  the Deployments before applying, or patch an existing install.
- **nodeSelector:** uncomment the field and replace the placeholder node label.
- **Failed-Pod recovery:** uncomment `failedPodPolicy: Recreate` in the
  SandboxTemplate (agent-sandbox v1.0.8+) so a Failed Pod is replaced instead
  of parked.
- **Provider keys:** create the optional `dsh-provider-keys-extra` Secret; the
  base `dsh-provider-keys` Secret remains the one required key.

Full prerequisites, exact commands, verification and caveats:
[optional hardening](../../docs/optional-hardening.md).
