# Local overlay (cluster-specific values)

The shipped manifests stay portable and contain no cluster-specific active
state (see the "Cluster-specific state stays out" rule in
[`docs/examples.md`](../../../../docs/examples.md)). gVisor, node-pool labels,
the storage class and size, the public hostname, SSO proxy hop counts and image
pins differ per install, so this directory is the documented place to record
them for an existing cluster.

The mechanism is plain `kubectl` (no Kustomize, no Helm):

1. Copy the template and edit it with your cluster's values:
   `cp manifests/local/values.env.example manifests/local/values.env`
   (`values.env` is git-ignored; never put Secrets in it).
2. Apply the base resources first (`kubectl apply -f manifests/` and, if used,
   one exposure variant).
3. Preview the patches, then apply them:

```bash
./scripts/local-overlay.sh                  # prints the kubectl commands
./scripts/local-overlay.sh --apply          # applies them, idempotently
```

The script patches only fields present in `values.env`; empty values are
skipped. It requires the template, pool and gateway Deployment to exist, and it
refuses to guess when the object shape differs (for example more than one
volume claim template). It is read-only unless `--apply` is passed.

### Migrating an existing install

An earlier install may run a hand-made template (for example a second
`SandboxTemplate` created because the repo blueprint needed gVisor or a node
label) with the warm pool bound to it. The overlay is also the migration path:

1. Set `TEMPLATE_NAME` to the repo blueprint (`dsh-web`) and fill in the
   cluster values, including a `NODE_SELECTOR_KEY`/`VALUE` the nodes already
   carry (an existing label such as `kubernetes.io/hostname` avoids relabeling
   nodes).
2. `./scripts/local-overlay.sh --apply`. It patches the blueprint (including
   the `automountServiceAccountToken: false` baseline), points the warm pool
   back at it and sets the replica count. Running sandboxes keep their stamped
   pod template, so there is no downtime.
3. Wait for a new warm-pool spare from the blueprint, then delete the old
   template:
   `kubectl -n dsh-demo get sandboxes -l agents.x-k8s.io/warm-pool-sandbox -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.annotations.agents\.x-k8s\.io/sandbox-template-ref}{"\n"}{end}'`
4. Re-run `scripts/cluster-drift.sh`.

The readiness probe is a special case: the shipped manifest uses `/readyz`,
but an image built before this change only serves `/healthz`. Update the image
first, then set `GATEWAY_READINESS_PATH=/readyz` (or apply the new
`manifests/50-gateway.yaml` on a fresh install).

Why this exists: without a recorded overlay, a cluster ends up running a
template or pool that is not in Git (for example a `dsh-web-2` created by hand
because the shipped `dsh-web` needed gVisor or a node label). That state is
invisible to review, not reproducible, and re-applying the repo manifests
silently reverts it. Keep the local values here, then check the live state with
[`scripts/cluster-drift.sh`](../../scripts/cluster-drift.sh).
