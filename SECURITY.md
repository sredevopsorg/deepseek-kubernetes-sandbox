# Security policy

## Supported versions

Only the latest published release is supported. Image tags are immutable;
fixes ship as a new release (see docs/versioning.md).

## Reporting a vulnerability

Report vulnerabilities privately through GitHub Security Advisories:

https://github.com/sredevopsorg/deepseek-kubernetes-sandbox/security/advisories/new

Do not open a public issue, and do not include live credentials in a report.
You can expect an acknowledgement within a few days and a coordinated
disclosure once a fix is available.

## Threat notes specific to this project

- **Launch token in pod logs.** The example gateway reads DSH's launch token
  from pod logs (pods/log). Anyone with access to those logs can impersonate a
  tenant. Keep the gateway's RBAC namespace-scoped and treat pod logs as
  sensitive. Replacing log scraping with an upstream token API is tracked in
  the [example architecture document](examples/dsh-agents-as-a-service/docs/architecture.md#upstream-feature-request-remove-log-scraping).
- **Browser sessions are server-side.** The gateway session cookie holds an
  opaque session id, not a user name or token; it is not accepted as a bearer
  token. Sessions are revoked on logout, user delete, `/recreate` and expiry,
  but they live in gateway memory, so a gateway restart logs every browser out
  and scaling beyond one replica requires a shared store. Do not treat the
  cookie as durable or cross-replica state.
- **SSO header trust boundary.** AUTH_PROXY_HEADER makes the gateway trust an
  identity header (for example X-Forwarded-User). Enable it only when the
  gateway Service is reachable exclusively through the authenticating proxy
  and its NetworkPolicy; otherwise any client can spoof the header. The SSO
  exposure variant's policy is required, not optional: an exposure without it
  is demo-only. Run `examples/dsh-agents-as-a-service/scripts/preflight-sso.sh`
  after applying the variant; it checks the Secrets, gateway env, TLS
  reference, leftover placeholders and accidental second paths.
- **Insecure-cookie guard.** TLS terminates outside the gateway. Set
  COOKIE_SECURE=true for HTTPS and remove ALLOW_INSECURE_COOKIES; the gateway
  refuses to start with COOKIE_SECURE=false unless ALLOW_INSECURE_COOKIES=1 is
  explicitly set, which exists only for the plain-HTTP local demo. Never expose
  that combination outside localhost.
- **Abuse controls are in-process.** AUTH_RATE_LIMIT/AUTH_RATE_WINDOW,
  SIGNUP_INVITE_CODE, MAX_IN_FLIGHT_PER_USER and WS_MAX_MESSAGE_BYTES bound
  casual abuse and resource exhaustion on the single gateway replica. They
  reset on restart and are not an ingress rate limiter, WAF, quota system or
  audit log; add those for an internet-facing deployment.
- **Shared provider keys.** The example injects DEEPSEEK_API_KEY and any keys in
  the optional dsh-provider-keys-extra Secret into every tenant sandbox (a
  warm-pool constraint). Treat tenants as mutually trusting until per-tenant
  credentials or an egress proxy exist.
- **The base example is demo-only, not hardened.** gVisor, nodeSelector, the
  sandbox/exposure NetworkPolicies, the gateway egress policy and extra provider
  keys are opt-in. Without gVisor and the network policies there is no
  tenant-to-tenant isolation; the sandboxes share the host kernel and can reach
  cluster-internal endpoints. Production requirements are enumerated in the
  [production checklist](examples/dsh-agents-as-a-service/docs/optional-hardening.md#6-production-checklist).
- **No secrets in the repository.** The repository only ships empty Secret
  manifests and *.yaml.example files.

## Dependency and supply chain

GitHub Actions are pinned by commit SHA. The CodeQL workflow scans Python and
Actions. The release workflow publishes images with SBOM and provenance
attestations.
