# Local overlay (cluster-specific values)

The shipped manifests stay portable and contain no cluster-specific active
state (see the "Cluster-specific state stays out" rule in
[`docs/examples.md`](../../../../docs/examples.md)). gVisor, node-pool labels,
the storage class and size, the public hostname, SSO proxy hop counts and image
pins differ per install, so this directory is the documented place to record
them for an existing cluster.

It is a **static Kustomize overlay**: there is no `values.env` and no helper
script. By default it renders exactly the base (all cluster-specific entries
are commented out), so `kubectl apply -k manifests/local` is safe to run as-is.

```text
manifests/local/
├── kustomization.yaml   # resources: [../base]; every patch entry commented
├── patches/             # inert until referenced by kustomization.yaml
└── README.md
```

## Usage

1. Edit the TODO placeholder in the patch file(s) this cluster needs (each
   file's header says what it changes).
2. Uncomment the matching `patches:` entry in
   [`kustomization.yaml`](kustomization.yaml). The exact snippet is repeated in
   the patch file header. JSON-6902 patches (a top-level YAML list of
   `op`/`path`/`value`) **must** carry an explicit `target`; strategic-merge
   patches name the object in their body.
3. Review the render and the diff, then apply:

   ```bash
   kubectl kustomize manifests/local        # render only (no cluster)
   kubectl diff -k manifests/local          # what would change (needs a cluster)
   kubectl apply -k manifests/local         # apply base + enabled patches
   ```

4. Apply only the settings this cluster needs: `kubectl apply -k` is
   idempotent, so re-running after uncommenting another patch is safe.

Rollback: re-comment the entry and re-apply. `kubectl apply` tracks the last
applied configuration for each object, so rendering the base again (without the
field) removes a field that a previous `apply -k` added — confirm with
`kubectl diff -k manifests/local` first. If a field was set outside
`kubectl apply` (a manual `kubectl patch`), clear it with `op: remove` / a
`null` value instead. Never use `kubectl delete -k manifests/local`: it also
deletes the base (namespace and everything in it).

## Composition

- **Base (default):**

  ```yaml
  resources:
    - ../base
  ```

- **With one exposure variant.** Replace `../base` with **exactly one**
  variant; each exposure overlay already includes the base and patches the
  gateway environment and the sandbox `DSH_TRUSTED_HOSTS`:

  ```yaml
  resources:
    - ../exposure/ingress        # OR
    - ../exposure/gateway-api-sso
  ```

  Never list `../base` together with a variant, never list both variants (they
  are mutually exclusive), and never apply the base and an overlay separately:
  re-applying the base would revert the overlay's gateway-env and host patches.

- **Sandbox NetworkPolicy (optional component).** Uncomment:

  ```yaml
  components:
    - ../optional/network-policy
  ```

  A component is additive and only renders the policy; see
  [`../optional/README.md`](../optional/README.md). It can also be applied
  standalone without touching the base:
  `kubectl apply -k manifests/optional/network-policy`.

- **Image pin.** Uncomment the `images:` block to override the gateway (and/or
  sandbox) image tag or digest for this cluster.

The **kustomize cycle constraint**: a kustomization root may not be an ancestor
of the overlays that include it. That is why the overlays are siblings of
`manifests/base/` (`manifests/local/`, `manifests/exposure/<variant>/`), not
directories inside it. Keep it that way, and keep every root renderable with
`kubectl kustomize <root>`.

## Patch files

Every file under `patches/` is inert until its entry is uncommented.

| Patch | Purpose | Needs JSON `target` |
| --- | --- | --- |
| `sandboxtemplate-runtime-class.yaml` | run sandbox pods under gVisor | yes (SandboxTemplate) |
| `sandboxtemplate-node-selector.yaml` | pin sandboxes to a node pool | yes (SandboxTemplate) |
| `sandboxtemplate-storage-class.yaml` | pin the workspace StorageClass | yes (SandboxTemplate) |
| `sandboxtemplate-storage-size.yaml` | change the workspace PVC size (base 5Gi) | yes (SandboxTemplate) |
| `sandboxtemplate-failed-pod-policy.yaml` | `failedPodPolicy: Recreate` (agent-sandbox v1.0.8+) | yes (SandboxTemplate) |
| `sandboxtemplate-trusted-hosts.yaml` | custom `DSH_TRUSTED_HOSTS` (exposure overlays set it themselves) | yes (SandboxTemplate) |
| `sandboxwarmpool-replicas.yaml` | number of pre-warmed spares (base 2) | no (strategic merge) |
| `gateway-runtime-class.yaml` | run the gateway under gVisor | yes (Deployment) |
| `gateway-node-selector.yaml` | pin the gateway to a node pool | yes (Deployment) |
| `gateway-readiness-path.yaml` | readiness probe path override for an older image | yes (Deployment) |
| `gateway-image-pull-policy.yaml` | gateway `imagePullPolicy` | no (strategic merge) |
| `gateway-env.yaml` | idle/wake timeouts, cookie/TLS and SSO-proxy env; drops `ALLOW_INSECURE_COOKIES` | no (strategic merge) |
| `oauth2-proxy-github-org.yaml` | SSO-only: allowed GitHub organization | yes (Deployment `oauth2-proxy`) |
| `oauth2-proxy-redirect-url.yaml` | SSO-only: public OAuth callback URL | yes (Deployment `oauth2-proxy`) |
| `networkpolicy-oauth2-proxy-traefik-namespace.yaml` | SSO-only: scope oauth2-proxy ingress to the Gateway namespace | yes (NetworkPolicy) |

The three SSO-only patches match objects that exist only when the overlay
includes `../exposure/gateway-api-sso`; enabling them with the base or the
plain ingress variant makes the build fail (no matching object) rather than
silently doing nothing.

The patch target paths were written against the shipped base (namespace
`dsh-demo`, SandboxTemplate `dsh-web`, warm pool `dsh-pool`, gateway Deployment
`dsh-gateway`). If you rename those or change the namespace, update the
`target:` blocks and the JSON paths together.

## Readiness-path caveat

The base already points the readiness probe at `/readyz` (API-aware: a gateway
that cannot reach the Kubernetes API is removed from the Service endpoints).
`gateway-readiness-path.yaml` downgrades it to `/healthz` and exists only for an
**older** gateway image that does not serve `/readyz` — without it that pod
would never become Ready. Prefer updating the image; if you must downgrade,
expect an API outage to leave the gateway in the Service while it answers 500s.

## Migrating an existing install

An earlier install may run a hand-made template (for example a second
`SandboxTemplate` created because the repo blueprint needed gVisor or a node
label) with the warm pool bound to it. The old `values.env` +
`scripts/local-overlay.sh` mechanism is gone; the overlay is now the migration
path:

1. Translate the old values into the patch files: runtime class, node selector,
   storage class/size, failed-Pod policy, trusted hosts, gateway env, image
   pin/pull policy, readiness path, and (for SSO) the GitHub org, redirect URL
   and Traefik namespace. Keep an existing node label (for example
   `kubernetes.io/hostname`) to avoid relabeling nodes.
2. Enable those patch entries and check the render/diff:
   `kubectl kustomize manifests/local && kubectl diff -k manifests/local`.
3. `kubectl apply -k manifests/local`. It patches the repo blueprint (`dsh-web`)
   in place, points the warm pool back at it and keeps the
   `automountServiceAccountToken: false` baseline. Running sandboxes keep their
   stamped pod template, so there is no downtime for them.
4. Wait for a new warm-pool spare from the blueprint, then delete the old
   hand-made template **manually** (it is not part of the overlay, so apply
   will not remove it):
   `kubectl -n dsh-demo delete sandboxtemplate <old-name>`.
5. Re-run [`scripts/cluster-drift.sh`](../../scripts/cluster-drift.sh) to
   confirm the live state matches the repo.

Why this exists: without a recorded overlay, a cluster ends up running a
template or pool that is not in Git (for example a `dsh-web-2` created by hand
because the shipped `dsh-web` needed gVisor or a node label). That state is
invisible to review, not reproducible, and re-applying the repo manifests
silently reverts it. Record the values here, then check the live state with
[`scripts/cluster-drift.sh`](../../scripts/cluster-drift.sh).
