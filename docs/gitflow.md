# Gitflow: branches, pull requests and CI

This repository uses **trunk-based development with release branches**. `main` is
always releasable; feature work happens on short-lived branches; releases are cut
from `main` onto a release branch and tagged with an immutable release identity.
See [versioning](versioning.md) for what the tags mean.

## Branches

| Branch | Branched from | Merged into | Purpose |
| --- | --- | --- | --- |
| `main` | — | — | The integration trunk. Protected, linear history, always releasable. |
| `feat/<slug>` | `main` | `main` (squash) | New capability or example. |
| `fix/<slug>` | `main` | `main` (squash) | Bug fix. |
| `docs/<slug>` | `main` | `main` (squash) | Documentation only. |
| `refactor/<slug>`, `chore/<slug>`, `test/<slug>` | `main` | `main` (squash) | Non-behavioral rework, maintenance, tests. |
| `release/<identity>` | `main` at the release commit | — | Release stabilization and tagging. Only release-blocking fixes land here. |
| `hotfix/<slug>` | The release branch (or the tagged commit) | `main` and the release branch | Urgent production fix. |

Keep branches short-lived. Rebase on `main` rather than merging `main` into the
branch; the PR is squash-merged, so individual branch commits are never part of
`main` history.

## Pull requests

1. **One scoped change per PR.** Do not mix a version bump with unrelated work,
   and update the docs the change touches in the same PR.
2. **Conventional PR title.** The title becomes the squash commit message, so it
   is linted by the `pr-title` check: `feat(scope): ...`, `fix(scope): ...`,
   `docs(scope): ...`, `build: ...`, `ci: ...`, `refactor: ...`, `test: ...`,
   `chore: ...`. Breaking changes use `feat(scope)!: ...` or a
   `BREAKING CHANGE:` footer.
3. **CI must be green.** All required checks below must pass before merge.
4. **Squash merge only.** `main` stays linear and protected; merged PRs are one
   commit each.
5. **No direct pushes to `main`** or to a release branch. Both are protected.
6. **Review for behavior changes.** Infrastructure, workflow, release and
   gateway-contract changes need a maintainer review; trivial docs changes still
   need the checks.

## Required checks

| Check | Workflow / job | What it verifies |
| --- | --- | --- |
| `lint` | `ci.yaml` | `scripts/check-versions.sh` (version pins and markdown tokens), `scripts/check-examples.sh` (example layout), shellcheck, actionlint, yamllint, hadolint and documentation links. |
| `python` | `ci-python.yaml` | Gateway ruff check/format and unit tests (`pytest` with coverage). Runs only when the gateway or this workflow changes. |
| `manifests` | `ci-manifests.yaml` | `scripts/check-manifests.py` (manifest shape) and a server-side dry-run of every example manifest against the agent-sandbox CRDs. Runs only when manifests, check scripts or `versions.env` change. |
| `pr-title` | `pr-title.yaml` | Conventional Commit PR title. |
| `CodeQL` | `codeql.yml` | Static analysis for Python and GitHub Actions. |
| `dependency-review` | `ci.yaml` | Dependency changes introduced by the PR (job `dependency-review`; runs on pull requests only). |

`python` and `manifests` use native `on.pull_request.paths` filtering, so PRs that
do not touch the gateway or the manifests skip them. The gateway image build and
`/healthz` are no longer smoke-tested in a dedicated job: `/healthz` is covered by
the gateway unit tests, the Dockerfile is linted by `lint` (hadolint), and the
multi-arch image build is published by `build-gateway.yaml` on merge.

Because a skipped path-filtered check is not a passing check, only the always-on
checks (`lint`, `pr-title`, `CodeQL`, `dependency-review`) should be enforced as
required status checks; `python` and `manifests` are on-demand checks that skip
when unrelated to the change.

The image build workflows (`_build-image.yaml`, `build-main.yaml`,
`build-gateway.yaml`) and `e2e-kind.yaml` are **not** required on every PR: the
image builds run where they are needed, and the kind end-to-end suite is reserved
for release validation and manual dispatch because it is slow.

## Releases

Releases are cut from `main`:

1. Confirm `main` is green and `make check`, `make test`, `make lint` pass.
2. Bump [versions.env](../versions.env) if the DSH ref changed, using
   `make set-version`; otherwise keep the same `DSH_VERSION` and bump
   `RELEASE_REVISION`.
3. Create `release/<identity>` from the release commit.
4. Run `make release-dry-run` and review the generated notes.
5. Create and push the annotated `dsh-*` release tag. The `release.yaml`
   workflow validates that the tag matches [versions.env](../versions.env), builds
   and pushes the multi-arch images (version + latest), creates the GitHub
   Release from the git-cliff notes, and opens a `chore(changelog)` PR that
   refreshes `CHANGELOG.md` on `main`.
6. Merge the changelog PR and any release-branch-only fixes back to `main`.

Full procedure: [versioning](versioning.md).

## Hotfixes

A released tag is **never** moved or re-tagged — tags and image identities are
immutable. To fix a broken release:

1. Branch `hotfix/<slug>` from the release tag (or its release branch).
2. Make the minimal fix, add a test, open a PR; the same required checks apply.
3. Publish a new identity with a higher `RELEASE_REVISION` (`-r(N+1)`). Do not
   reuse or delete the broken tag.
4. Merge the fix back into `main` (or cherry-pick it) so the trunk is fixed too.

Rollback is also a re-release, never a retag: see
[versioning](versioning.md#rollback).

## Tag protection

All release tags match `dsh-*` and are protected:

- only maintainers (the release workflow) may create them;
- they cannot be deleted or force-updated;
- they are annotated and point at the release commit on a release branch.

Image tags derive from the same identity and are equally immutable; the mutable
`latest` and `main` image channels always point at a release or a trunk build
respectively.

## One-time GitHub settings checklist

Configure these on the repository before relying on the workflow:

- [ ] **Branches → main**: require a pull request; require the checks `lint`,
      `python`, `manifests`, `pr-title`, `CodeQL`, `dependency-review`; require
      branches to be up to date; require conversation resolution; restrict direct
      pushes; allow only squash merges and enable "Automatically delete head
      branches".
- [ ] **Branches → release/***: same protections, plus allow the release workflow
      to push tags.
- [ ] **Tags → dsh-***: restrict creation/update/deletion to maintainers; a tag
      push triggers the release workflow.
- [ ] **Actions → General**: allow GitHub Actions; set workflow permissions to
      read-only by default; allow `GITHUB_TOKEN` to create pull requests only
      where required.
- [ ] **Actions → Runners**: make the arm64 runners (`ubuntu-24.04-arm`) available
      to the public/private repository as appropriate.
- [ ] **Packages**: grant the release workflow `packages: write` (workflow
      permissions do this) and protect the
      `ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox` packages from deletion.
- [ ] **Security**: enable CodeQL default setup **or** keep the advanced
      `codeql.yml` workflow (not both), enable Dependabot alerts and
      dependency review.
- [ ] **Secrets/Variables**: none are needed for image publishing; the workflows
      use `GITHUB_TOKEN`.
