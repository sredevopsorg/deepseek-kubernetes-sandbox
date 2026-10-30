# Workflow security (GitHub Actions)

The CI/CD surface lives entirely in [`.github/workflows/`](../.github/workflows/)
and runs on GitHub-hosted runners. This page describes the controls those
workflows enforce and records the most recent audit. They complement the threat
notes in [SECURITY.md](../SECURITY.md) and the release procedure in
[versioning.md](versioning.md).

## Controls

| Control | Where | What it does |
| --- | --- | --- |
| Least-privilege tokens | Every workflow | A top-level `permissions:` block defaults to read-only; `packages: write`, `contents: write`, `id-token: write` and `attestations: write` are granted only to the jobs that publish images, attestations or releases. |
| Pinned actions | Every `uses:` | Third-party actions and reusable workflows are pinned to a full 40-character commit SHA (the version is kept in the trailing comment). Dependabot updates the pins weekly. The only unpinned reference is the in-repository `./.github/workflows/_build-image.yaml`. |
| No privileged untrusted triggers | Trigger blocks | No workflow uses `pull_request_target` or `workflow_run`. PR-triggered workflows run untrusted code with a read-only token and no secrets. |
| Untrusted input is data | `release.yaml` | The release tag (from a tag push or `workflow_dispatch` input) is passed through `env:` and read as a shell variable; it is never interpolated into a `run:` script, so metacharacters in a tag name cannot execute. |
| Checkout credential hygiene | Checkout steps | `persist-credentials: false` is set wherever the job does not need to push. The changelog job keeps credentials because it pushes a branch and opens a PR with `GITHUB_TOKEN`. |
| Download integrity | `ci.yaml`, `release.yaml` | actionlint, hadolint and git-cliff are downloaded with `curl --proto '=https'` and verified against a pinned SHA-256 with `sha256sum -c` before execution. |
| Cluster manifest integrity | `ci-manifests.yaml`, `e2e-kind.yaml` | The agent-sandbox and Gateway API release manifests are downloaded and checked against the `*_MANIFEST_SHA256` pins in [versions.env](../versions.env) before `kubectl apply`. |
| Log and artifact hygiene | `ci-image.yaml`, `e2e-kind.yaml` | Pod logs are truncated and token material is redacted before logs are printed or diagnostic files are uploaded as artifacts. |
| Supply-chain monitoring | Repository settings | Dependabot (GitHub Actions, pip, Docker), CodeQL advanced setup and the dependency-review action run on every applicable change. CODEOWNERS covers the whole repository, including `.github/workflows/`. |

## Maintaining the pins

- The tool versions and their SHA-256 digests live together in the install step
  (`ACTIONLINT_SHA256`, `HADOLINT_SHA256`, `GIT_CLIFF_SHA256`); update both
  values when bumping a version.
- Control-plane versions are in `versions.env`; update
  `AGENT_SANDBOX_MANIFEST_SHA256` or `GATEWAY_API_MANIFEST_SHA256` together with
  the corresponding version, otherwise CI fails the digest check by design.
- GitHub Actions pins are updated by Dependabot; never replace a SHA with a tag.

## Audit record (2026-10-30)

Scope: all 11 workflows under `.github/workflows/`, plus `.github/CODEOWNERS`
and `.github/dependabot.yml`. Findings are grouped by the categories in
GitHub's [Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use).

| # | Category | File | Severity | Finding | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | Script injection | `release.yaml` | High | Tag name / dispatch input interpolated into a privileged `run:` block | Fixed: passed through `env` |
| 2 | Token permissions | `release.yaml` | Medium | `meta` and `tests` inherited workflow-level write scopes | Fixed: workflow default is `contents: read` |
| 3 | Token permissions | `build-main.yaml`, `build-gateway.yaml` | Low | `meta` inherited `packages`/`id-token`/`attestations` write | Fixed: workflow default is `contents: read` |
| 4 | Checkout | `_build-image.yaml` | Low | Checkout persisted the token in `.git/config` | Fixed: `persist-credentials: false` |
| 5 | Checkout | `codeql.yml` | Low | Checkout persisted the token in `.git/config` | Fixed: `persist-credentials: false` |
| 6 | Supply chain | `ci.yaml` | Medium | actionlint and hadolint were executed without a checksum | Fixed: pinned SHA-256 verified before install |
| 7 | Supply chain | `release.yaml` | Medium | git-cliff was executed without a checksum in the release job | Fixed: pinned SHA-256 verified before install |
| 8 | Supply chain | `ci-manifests.yaml`, `e2e-kind.yaml` | Medium | Upstream CRD/controller manifests applied without integrity checks | Fixed: pinned SHA-256 in `versions.env` |
| 9 | Secret hygiene | `e2e-kind.yaml` | Low | Raw gateway logs uploaded in a 7-day artifact | Fixed: `token=` values redacted |
| 10 | Repository controls | Repository settings | Low | No environment approval gate on publishing; single CODEOWNER | Recommendation: see below |

Checked and clean: no `pull_request_target`/`workflow_run`, no self-hosted
runners, all third-party actions pinned, PR workflows read-only, no secrets in
workflow files, no secret interpolation into scripts.

## Residual recommendations (repository settings)

These cannot be enforced from workflow files alone:

- Add a protected `release` environment with required reviewers and attach it to
  the `build-main`, `build-gateway` and `publish-release` jobs. This requires the
  environment to exist in repository settings first, so it is intentionally not
  wired into the workflow yet.
- Keep "Allow GitHub Actions to create and approve pull requests" limited to
  creating PRs (the changelog job needs create, not approve).
- Require review from Code Owners for `.github/workflows/` and add a second
  owner so workflow changes are never single-person.
- Optionally run OpenSSF Scorecard on a schedule for continuous checks on these
  controls.
