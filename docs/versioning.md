# Versioning, releases and changelog

[versions.env](../versions.env) is the **single source of truth** for the DSH ref,
the repository revision, the pnpm version, the control-plane component versions
and the image names. Nothing else may hard-code a version:
[scripts/check-versions.sh](../scripts/check-versions.sh) fails when a pin drifts,
and it also verifies that prose mentions only the current release identity or its
base DSH tag.

## Release identity

```
release identity = DSH_VERSION + optional "-" + RELEASE_REVISION
```

| Piece | Meaning |
| --- | --- |
| `DSH_VERSION` | The upstream DeepSeek Harness git ref being packaged, e.g. `dsh-v0.2.1-rc.5-r6`. It mirrors upstream's own tag. |
| `RELEASE_REVISION` | An optional repository revision at that DSH ref: empty for the first release, `rN` (`N >= 2`) for a repository-only re-release. |

Examples:

- `DSH_VERSION=dsh-v0.2.1-rc.5-r6`, `RELEASE_REVISION=` → identity `dsh-v0.2.1-rc.5-r6`
  (first release at that upstream ref).
- `DSH_VERSION=<ref>`, `RELEASE_REVISION=rN` → identity `<ref>-rN` (for example
  documentation, CI or gateway changes on top of an already-released ref; no DSH
  upgrade). Use `r2` for the first re-release: revision 1 is the plain tag.

A DSH upgrade produces a new base identity (for example the next upstream ref
with an empty revision); a repository-only change at the same DSH ref increments
`rN`.

## Why release identities are immutable

**Container tags are immutable** in this project: re-pushing a tag would silently
change what an existing deployment runs. A fix therefore publishes a new identity;
see the rules above.

The current identity is `dsh-v0.2.1-rc.5-r6`, the first release at this upstream
ref, so it carries no revision suffix. The identity is stored in
[versions.env](../versions.env) and consumed everywhere: the Dockerfile labels
(version and revision), the example manifests, the Makefile and the release
workflow.

## Channels

| Channel | Image tag | Meaning |
| --- | --- | --- |
| Release | `dsh-v0.2.1-rc.5-r6` | The current immutable release identity. What production should pin (preferably by digest). |
| `latest` | `latest` | Alias for the most recent release; moves only when a new release is published. |
| `main` | `main` | Development channel, rebuilt when image or gateway sources change on `main`. Never treat it as a release; it exists so `main` can be tested. |

The same channels exist for every published package, including the nested
example component image.

## Compatibility matrix

Values from [versions.env](../versions.env) at the current identity:

| Component | Pin | Source |
| --- | --- | --- |
| Upstream DSH source | `DSH_REPO` | [versions.env](../versions.env) |
| DSH ref | `dsh-v0.2.1-rc.5-r6` | `DSH_VERSION` |
| DSH commit | `b830ec950faba0a46dda564be3e2d854afed94f1` | `DSH_COMMIT`; the main image build fails unless the clone's `HEAD` matches |
| Repository revision | `(empty)` | `RELEASE_REVISION` |
| pnpm | `11.28.5` | `PNPM_VERSION` |
| Node build image | `docker.io/node:24-trixie@sha256:8c761eca2bf7eec965f946cf47b8090143cb41f45f2f8699af0cc0298d2d2d19` | `NODE_BUILD_IMAGE`, digest-pinned |
| Node runtime image | `docker.io/node:24-trixie-slim@sha256:54f84b4cf8432b21aaffd5d3ce8d41e12cea0d10d33c09d2f3c9879d49d6b446` | `NODE_RUNTIME_IMAGE`, digest-pinned |
| Gateway Python image | `docker.io/python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159` | `GATEWAY_PYTHON_IMAGE`, digest-pinned |
| agent-sandbox (CRDs) | `v1.0.9` | `AGENT_SANDBOX_VERSION`, used by the examples and CI dry-run |
| Kubernetes Gateway API | `v1.2.1` | `GATEWAY_API_VERSION`, used by the SSO exposure variant |
| oauth2-proxy | `quay.io/oauth2-proxy/oauth2-proxy:v7.15.5@sha256:8498b0d0ef0a7b29686414000a08aee467f02d0299c9ed1e006a8f33fc017916` | `OAUTH2_PROXY_IMAGE`, digest-pinned and used by the SSO exposure variant |
| Main image | `ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox` | `GHCR_IMAGE` |
| Gateway image | `ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox/dsh-agents-as-a-service/gateway` | `GATEWAY_IMAGE`, nested below `GHCR_IMAGE` |

Every container base image is referenced as `name:tag@sha256:<index digest>`, not
by tag alone: `check-versions.sh` rejects a pin without a digest, so a re-pushed
tag cannot change what we build from. The example gateway's Python dependencies
are fully hash-pinned in
[requirements.txt](../examples/dsh-agents-as-a-service/gateway/requirements.txt)
and
[requirements-dev.txt](../examples/dsh-agents-as-a-service/gateway/requirements-dev.txt)
and installed with `pip install --require-hashes`.

Example component images are always nested below the main package:
`<GHCR_IMAGE>/<example>/<component>`. `check-versions.sh` rejects an unrelated
top-level package.

## Bumping versions

```bash
make set-version DSH_VERSION=dsh-vX.Y.Z RELEASE_REVISION=rN
make check
```

`set-version` updates [versions.env](../versions.env) through
[scripts/set-version.sh](../scripts/set-version.sh) and leaves every consuming
file to derive from it. Never edit the Dockerfile, manifests, Makefile or
workflows with a literal version.

- New upstream DSH ref: change `DSH_VERSION` and clear `RELEASE_REVISION`.
  `set-version` resolves the new ref's commit with `git ls-remote` (peeled `^{}`
  commit first, tag ref as fallback) and rewrites `DSH_COMMIT` in `versions.env`,
  the Dockerfile ARG default and `.env.sample`; the build then verifies it.
- Repository-only change at the same DSH ref: keep `DSH_VERSION` and `DSH_COMMIT`,
  set `RELEASE_REVISION=r(N+1)`.
- Base images (Node and Python) are bumped by hand: change the tag *and* the
  `@sha256:` digest in `versions.env` together, then run `make check` and a build.
  There is deliberately no automated digest refresh, because a silent base-image
  change is exactly what digest pinning exists to prevent.

## Release checklist

1. `main` is green and `make check`, `make test`, `make lint` pass locally.
2. `make image` and `make gateway` build; smoke-test the images.
3. Bump the pins with `make set-version` (commit that change through a PR).
4. `make release-dry-run` — verifies the identity, prints the image names, checks
   the tag does not already exist, and previews the generated notes.
5. Create and push the annotated `dsh-*` release tag (or dispatch
   `release.yaml` with an existing tag). The workflow validates that the tag
   matches [versions.env](../versions.env) and that the upstream ref still
   resolves to `DSH_COMMIT` before building anything.
6. The workflow builds and pushes multi-arch images for the main image and the
   example components (the identity tag plus `latest`), creates the GitHub
   Release from the git-cliff notes, and opens a `chore(changelog)` PR that
   refreshes `CHANGELOG.md` on `main`. The publish log shows each image manifest
   and digest.
7. Merge the changelog PR. Verify:
   `docker buildx imagetools inspect <image>:<identity>` shows both platforms;
   the GitHub Release references the tag, and the workflow log shows the digest.
8. Announce the identity and the digest; deployments pin the digest.

## Rollback

Tags and release identities are **immutable**, so a bad release is never re-tagged
or deleted. Fix forward:

1. Fix the problem on a `hotfix/<slug>` branch (see [gitflow](gitflow.md)).
2. Publish a new identity with the next revision, `-r(N+1)`, at the same
   `DSH_VERSION`.
3. Point deployments at the new digest; keep the broken tag for audit history.

Emergency rollback to the previous release is also a re-deploy of a **previous
digest**, not a move of `latest`.

## Changelog

`CHANGELOG.md` is generated by [git-cliff](https://git-cliff.org) from
Conventional Commits, configured in [cliff.toml](../cliff.toml):

```bash
make changelog   # regenerate CHANGELOG.md locally
```

- Do not edit `CHANGELOG.md` by hand; the release workflow regenerates it and opens a PR to refresh it on `main`.
- Commit messages and PR titles must be Conventional Commits so they are grouped
  correctly (Added / Fixed / Changed / Documentation / Build / CI / Tests /
  Maintenance).
- The changelog groups commits since the previous `dsh-*` tag; release notes use
  the same rendering.
- The changelog is excluded from the version-token check because it records
  historical identities on purpose.

## Digest pinning

Supply-chain inputs are pinned to immutable content, not to mutable tags:

- **Build inputs**: every container base image is referenced by multi-arch index
  digest in [versions.env](../versions.env), and the example gateway's Python
  dependencies are hash-pinned (`--require-hashes`).
- **Upstream source**: the Dockerfile clones `DSH_VERSION` and fails unless the
  clone's `HEAD` equals `DSH_COMMIT`, so a moved upstream tag cannot change what
  a rebuild packages.

Release identities are immutable in theory; deployments should still pin the
_resolved_ digest for reproducibility and supply-chain safety:

```bash
docker buildx imagetools inspect ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:dsh-v0.2.1-rc.5-r6
# then, in the deployment:
#   image: ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox@sha256:<digest>
```

The example manifests use the identity tag so they stay readable and upgrade with
`versions.env`; production overlays should pin the digest. Release images carry
SBOM and provenance attestations (see [SECURITY.md](../SECURITY.md)).
