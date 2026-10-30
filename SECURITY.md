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
- **SSO header trust boundary.** AUTH_PROXY_HEADER makes the gateway trust an
  identity header (for example X-Forwarded-User). Enable it only when the
  gateway Service is reachable exclusively through the authenticating proxy;
  otherwise any client can spoof the header. The SSO exposure variant ships a
  NetworkPolicy that restricts gateway access to oauth2-proxy pods; a custom
  exposure needs an equivalent.
- **Shared provider keys.** The example injects DEEPSEEK_API_KEY and any keys in
  the optional dsh-provider-keys-extra Secret into every tenant sandbox (a
  warm-pool constraint). Treat tenants as mutually trusting until per-tenant
  credentials or an egress proxy exist.
- **Optional isolation is opt-in.** gVisor, nodeSelector, the sandbox/exposure
  NetworkPolicies and extra provider keys are opt-in. Without a
  NetworkPolicy-capable CNI or a gVisor runtime, the base example is not
  tenant-isolated; see examples/dsh-agents-as-a-service/docs/optional-hardening.md.
- **TLS is external.** Terminate TLS at an Ingress or Gateway and set
  COOKIE_SECURE=true. Plain HTTP outside localhost exposes session cookies.
- **No secrets in the repository.** The repository only ships empty Secret
  manifests and *.yaml.example files.

## Dependency and supply chain

GitHub Actions are pinned by commit SHA. The CodeQL workflow scans Python and
Actions. The release workflow publishes images with SBOM and provenance
attestations.
