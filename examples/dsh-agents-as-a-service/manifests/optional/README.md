# Optional manifests

Opt-in manifests that are **not** part of the top-level walkthrough.
`manifests/base/kustomization.yaml` does not list them, so
`kubectl apply -k manifests/base` (and the exposure overlays) do not apply this
directory. Apply a feature explicitly, or import it from your own overlay.

## network-policy/

A Kubernetes `NetworkPolicy` for the DSH sandboxes:

- ingress only from the gateway pods on TCP `3080` (HTTP + WebSocket);
- egress only to DNS and public HTTPS (provider APIs, package mirrors,
  git-over-HTTPS).

The directory is a Kustomize **Component**
(`apiVersion: kustomize.config.k8s.io/v1alpha1`, `kind: Component`) whose
`resources:` list the policy. Two ways to use it:

1. **Apply/remove the policy on its own.** A Component still builds
   standalone, so `-k` renders just the policy. This is additive: it never
   re-applies the base objects, and deleting it removes only this policy.

   ```bash
   kubectl apply  -k manifests/optional/network-policy
   kubectl delete -k manifests/optional/network-policy   # rollback
   ```

2. **Compose it into an overlay** (recommended, so the policy is part of the
   same reviewed/applied unit as the rest of the platform). Import it with
   `components:`; do not copy the file:

   ```yaml
   # manifests/local/kustomization.yaml
   components:
     - ../optional/network-policy
   ```

A Component is a reusable fragment, not a deployable root: it is only
meaningful when imported by an overlay (`components:`) or built explicitly as
above. Do not list it under `resources:`; that is not how Components compose.

It requires a CNI that enforces `NetworkPolicy`. The selector uses the
agent-sandbox controller's pod tracking label, so the policy applies to
existing warm-pool and claimed sandboxes without recreation.

**This is opt-in for the demo but required for production.** Without it every
tenant can reach every other tenant and cluster-internal endpoints. Production
also needs the gateway egress policy (cluster-specific API-server endpoint) and
the exposure variant's policy; see the
[production checklist](../../docs/optional-hardening.md#6-production-checklist).

## gVisor, nodeSelector, storage and provider keys

These are no longer documented as "uncomment it in the base": the base stays
portable. Cluster-specific hardening lives in the
[local overlay](../local/README.md) as inert patch files that you enable in
`manifests/local/kustomization.yaml`:

- **gVisor:** `patches/sandboxtemplate-runtime-class.yaml` (and
  `gateway-runtime-class.yaml`) add `runtimeClassName: gvisor`.
- **nodeSelector:** `patches/sandboxtemplate-node-selector.yaml` and
  `gateway-node-selector.yaml` pin the pods to a dedicated node pool; replace
  the placeholder label with one your nodes carry.
- **Storage:** `patches/sandboxtemplate-storage-class.yaml` and
  `sandboxtemplate-storage-size.yaml`.
- **Failed-Pod recovery:** `patches/sandboxtemplate-failed-pod-policy.yaml`
  sets `failedPodPolicy: Recreate` (agent-sandbox v1.0.8+) so a Failed Pod is
  replaced instead of parked.
- **Provider keys:** unchanged. Create the optional `dsh-provider-keys-extra`
  Secret; the base `dsh-provider-keys` Secret remains the one required key.

Full prerequisites, exact commands, verification and caveats:
[optional hardening](../../docs/optional-hardening.md).
