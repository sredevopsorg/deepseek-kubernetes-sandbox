# deepseek-kubernetes-sandbox based on hermes-agents-as-a-service

Original: https://github.com/kubernetes-sigs/agent-sandbox/tree/main/examples/hermes-agents-as-a-service

After analyzing the `hermes-agents-as-a-service` example, its core pattern maps directly onto your `dsh-web` container. The example's key mechanisms are: **`SandboxTemplate` defines a reusable Pod blueprint, `SandboxWarmPool` pre-warms instances, `SandboxClaim` lets each user claim a persistent Sandbox, and `service: true` gives each Sandbox a stable network identity for gateway routing.** Your DSH container is a strong fit for this model — it needs a persistent `DSH_HOME` (like Hermes needs a persistent data directory) and exposes a web port. The main differences are how secrets are injected and how the port is surfaced to end users.

### 🔑 Mechanism Mapping: Hermes → DSH

| Hermes example mechanism | Purpose | Adaptation for DSH |
| :--- | :--- | :--- |
| **`SandboxTemplate`** | Reusable Pod blueprint | Change image to `dsh-web:0.2.1-rc.2`, expose port `3080` |
| **`volumeClaimTemplates`** | Auto-create a PVC per user, persisting state | Mount point becomes `/home/node/.dsh` (DSH's state directory) |
| **`SandboxWarmPool`** | Pre-warmed instances for sub-second provisioning | Keep as-is; size the pool for expected concurrent users |
| **`SandboxClaim` + `warmPoolRef`** | User "registration" claims a lightweight object, adopts a pooled instance | Keep exactly as-is; one Claim per DSH user |
| **`service: true`** | Creates a headless Service per Sandbox with a stable DNS name | **Enable it** so a front-end gateway or Ingress can reliably route to each user's DSH Web |
| **`envVarsInjectionPolicy`** | Controls whether a Claim may inject/override env vars | DSH needs `DEEPSEEK_API_KEY` injected, so either allow it or reference a Secret from the template |
| **`operatingMode`** | Cost switch: `Running` / `Suspended` (deletes Pod, keeps PVC + Service) | Reuse directly — suspend idle DSH instances to save cost |

### 📦 Equivalent Kubernetes Manifests

The manifests below mirror the Hermes example's organization. Apply them in order, all in a dedicated namespace (e.g. `dsh-demo`).

#### 1. Secret: DSH API key

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: dsh-api-secret
  namespace: dsh-demo
type: Opaque
stringData:
  DEEPSEEK_API_KEY: "sk-..." # replace with a real key
```

#### 2. SandboxTemplate: DSH Web blueprint

This is the core resource, corresponding to `10-sandboxtemplate.yaml` in the example. Key settings: **`service: true`** for stable DNS, **`volumeClaimTemplates`** to persist `/home/node/.dsh`, and a security context that runs as the non-root `node` user (UID 1000).

```yaml
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: dsh-web-template
  namespace: dsh-demo
spec:
  service: true # headless Service per Sandbox → stable DNS
  podTemplate:
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
      containers:
        - name: dsh-web
          image: dsh-web:0.2.1-rc.2 # replace with your registry path
          ports:
            - containerPort: 3080
              name: web
          env:
            - name: DEEPSEEK_API_KEY
              valueFrom:
                secretKeyRef:
                  name: dsh-api-secret
                  key: DEEPSEEK_API_KEY
            - name: DSH_PUBLIC_URL
              value: "http://localhost:3080" # change to the external URL when behind a gateway/Ingress
          volumeMounts:
            - name: dsh-home
              mountPath: /home/node/.dsh
          readinessProbe:
            httpGet:
              path: /
              port: 3080
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /
              port: 3080
            initialDelaySeconds: 15
            periodSeconds: 20
      # If your cluster uses gVisor / Kata, set the runtime class here
      # runtimeClassName: gvisor
  volumeClaimTemplates:
    - metadata:
        name: dsh-home
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 5Gi # adjust to your DSH state size
```

#### 3. SandboxWarmPool: pre-warmed pool

Corresponds to `20-sandboxwarmpool.yaml`. Pooled Sandboxes pull the image and boot ahead of time; a user Claim adopts one instantly.

```yaml
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxWarmPool
metadata:
  name: dsh-web-pool
  namespace: dsh-demo
spec:
  replicas: 2 # number of pre-warmed instances
  sandboxTemplateRef:
    name: dsh-web-template
```

#### 4. SandboxClaim: per-user claim

Corresponds to `40-claim.yaml`. Each user creates one lightweight Claim and receives a pooled instance with its own PVC and stable DNS.

```yaml
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxClaim
metadata:
  name: user-alice-dsh
  namespace: dsh-demo
spec:
  sandboxTemplateRef:
    name: dsh-web-template
  warmPoolRef:
    name: dsh-web-pool
  # If the template allows env injection (envVarsInjectionPolicy: Allowed),
  # you can override/add variables here:
  # env:
  #   - name: DSH_PUBLIC_URL
  #     value: "https://alice.dsh.example.com"
```

### 🌐 Networking: How Users Reach DSH Web

Unlike Hermes agents, which are mainly consumed by an internal gateway, DSH Web is a **user-facing web UI** — each Sandbox's `3080` port must be reachable by end users. Two patterns:

#### Pattern A: Direct via headless Service (simple)

With `service: true`, each Sandbox gets a headless Service whose DNS name is exposed in the Sandbox's `status.serviceFQDN`. You can reach it from inside the cluster via that FQDN, or add an Ingress/upper-layer gateway that routes a path to the corresponding Service.

#### Pattern B: Gateway-proxied (recommended, mirrors Hermes's service layer)

The Hermes example ships a ~250-line Python gateway (`gateway/gateway.py`) that handles **per-user routing, auth, and auto-waking suspended Sandboxes**. You can build a simpler equivalent for DSH:

1. Accept external requests (e.g. `/u/{user}/**`).
2. Look up the `status.serviceFQDN` of that user's Sandbox.
3. Reverse-proxy to `http://{serviceFQDN}:3080`.
4. If the Sandbox is `Suspended`, flip its `operatingMode` back to `Running`, wait for `Ready`, then proxy.

If you don't want a gateway yet, you can manually create one Ingress per user pointing at their Service — less automated, but functional.

### ⚙️ Key Differences and Caveats

1. **Secret-injection policy.** The Hermes example sets `envVarsInjectionPolicy: Disallowed` to strictly limit what Claims can inject. DSH needs `DEEPSEEK_API_KEY` injected, so you have two choices:
    - **Set it to `Allowed`**, then reference the Secret from `SandboxClaim.env` (more flexible, but requires trusting Claim creators).
    - **Keep it `Disallowed`** and reference the Secret directly inside the `SandboxTemplate`'s `podTemplate` (stricter, but all users share the same key — fine for a single-tenant internal setup).
    Pick based on your multi-tenancy model.

2. **`DSH_PUBLIC_URL` configuration.** Your entrypoint defaults to `http://localhost:3080`. When accessed via a headless Service or gateway, set `DSH_PUBLIC_URL` to the real external URL (e.g. `https://alice.dsh.example.com`), otherwise links DSH generates may be unreachable. You can override it in `SandboxClaim.env` (if injection is allowed) or rewrite URLs in the gateway.

3. **Image pull and platform.** As with the Hermes example, push `dsh-web` to a registry reachable by the cluster. Your image is built for `amd64`; node architecture must match.

4. **PVC size.** DSH state size (sessions, configs, etc.) depends on usage; Hermes allocated a PVC for `/opt/data`. Adjust the `storage` request in `volumeClaimTemplates` accordingly.

5. **Auto-suspend via `operatingMode`.** Hermes's gateway includes an idle sweeper that suspends idle instances automatically. If your DSH gateway implements a similar sweeper, you get the same cost model: `Running` while active, `Suspended` when idle (PVC + Service retained), auto-woken when the user returns.

### ✅ Deployment Order

```bash
kubectl create namespace dsh-demo
kubectl apply -f 00-secret.yaml
kubectl apply -f 10-sandboxtemplate.yaml
kubectl apply -f 20-sandboxwarmpool.yaml
kubectl apply -f 40-claim.yaml   # one per user
```

After applying, watch the warm pool spin up with `kubectl -n dsh-demo get sandboxes -w`, and inspect adoption and `status.serviceFQDN` with `kubectl -n dsh-demo get sandboxclaim user-alice-dsh -o yaml`. At that point you have a platform structurally identical to the Hermes example, but running DSH Web for multiple users.
