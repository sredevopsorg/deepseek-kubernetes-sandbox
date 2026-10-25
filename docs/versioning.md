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
| `DSH_VERSION` | The upstream DeepSeek Harness git ref being packaged, e.g. `dsh-v0.2.1-rc.2`. It mirrors upstream's own tag, which is what "mirrored-DSH scheme" means. |
| `RELEASE_REVISION` | An optional repository revision at that DSH ref: empty for the first release, `rN` (`N >= 2`) for a repository-only re-release. |

Examples:

- `DSH_VERSION=dsh-v0.2.1-rc.2`, `RELEASE_REVISION=` → identity `dsh-v0.2.1-rc.2`
  (first release at that upstream ref).
- `DSH_VERSION=dsh-v0.2.1-rc.2`, `RELEASE_REVISION=r2` → identity
  `dsh-v0.2.1-rc.2-r2` (documentation, CI or gateway changes on top of the same
  upstream DSH; no DSH upgrade).

A DSH upgrade produces a new base identity (for example the next upstream ref
with an empty revision); a repository-only change at the same DSH ref increments
`rN`.

## Why the current identity is `dsh-v0.2.1-rc.2-r2`

The image tag `dsh-v0.2.1-rc.2` was already published to GHCR for an earlier
repository state. **Container tags are immutable** in this project: re-pushing a
tag would silently change what an existing deployment runs. The mirrored-DSH
scheme therefore starts its first identity at `-r2`: the plain tag is
conceptually revision 1 and is taken, so the first publishable repository
revision at the same upstream ref is `r2`.

The identity is stored in [versions.env](../versions.env) and consumed everywhere:
the Dockerfile label, the example manifests, the Makefile and the release
workflow. The legacy `dsh-v0.2.1-rc.2` tag remains published but is not updated;
new deployments use `dsh-v0.2.1-rc.2-r2`.

The gateway image moved from the repository-level `:gateway` tag to a nested
package (`<repository>/dsh-agents-as-a-service/gateway`). For exactly one
release the release workflow also republishes the legacy
`<repository>:gateway` tag as a deprecated alias; it is removed in the next
release, so deployments should pin the nested package.

## Channels

| Channel | Image tag | Meaning |
| --- | --- | --- |
| Release | `dsh-v0.2.1-rc.2-r2` | The current immutable release identity. What production should pin (preferably by digest). |
| `latest` | `latest` | Alias for the most recent release; moves only when a new release is published. |
| `main` | `main` | Development channel, rebuilt from every push to `main`. Never treat it as a release; it exists so `main` can be tested. |

The same channels exist for every published package, including the nested
example component image.

## Compatibility matrix

Values from [versions.env](../versions.env) at the current identity:

| Component | Pin | Source |
| --- | --- | --- |
| Upstream DSH source | `DSH_REPO` | [versions.env](../versions.env) |
| DSH ref | `dsh-v0.2.1-rc.2` | `DSH_VERSION` |
| Repository revision | `r2` | `RELEASE_REVISION` |
| pnpm | `11.28.5` | `PNPM_VERSION` |
| agent-sandbox (CRDs) | `v1.0.7` | `AGENT_SANDBOX_VERSION`, used by the examples and CI dry-run |
| Kubernetes Gateway API | `v1.2.1` | `GATEWAY_API_VERSION`, used by the SSO exposure variant |
| oauth2-proxy | `quay.io/oauth2-proxy/oauth2-proxy:v7.15.5` | `OAUTH2_PROXY_IMAGE` |
| Main image | `ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox` | `GHCR_IMAGE` |
| Gateway image | `ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox/dsh-agents-as-a-service/gateway` | `GATEWAY_IMAGE`, nested below `GHCR_IMAGE` |

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
- Repository-only change at the same DSH ref: keep `DSH_VERSION`, set
  `RELEASE_REVISION=r(N+1)`.

## Release checklist

1. `main` is green and `make check`, `make test`, `make lint` pass locally.
2. `make image` and `make gateway` build; smoke-test the images.
3. Bump the pins with `make set-version` (commit that change through a PR).
4. `make release-dry-run` — verifies the identity, prints the image names, checks
   the tag does not already exist, and previews the generated notes.
5. Create and push the annotated `dsh-*` release tag (or dispatch
   `release.yaml` with an existing tag). The workflow validates that the tag
   matches [versions.env](../versions.env).
6. The workflow builds and pushes multi-arch images for the main image and the
   example components (the identity tag plus `latest`), creates the GitHub
   Release from the git-cliff notes, records the published digests, and opens a
   `chore(changelog)` PR that refreshes `CHANGELOG.md` on `main`.
7. Merge the changelog PR. Verify:
   `docker buildx imagetools inspect <image>:<identity>` shows both platforms,
   and the GitHub Release references the tag and the digest.
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

Release identities are mutable only in theory; deployments should still pin the
resolved digest for reproducibility and supply-chain safety:

```bash
docker buildx imagetools inspect ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:dsh-v0.2.1-rc.2-r2
# then, in the deployment:
#   image: ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox@sha256:<digest>
```

The example manifests use the identity tag so they stay readable and upgrade with
`versions.env`; production overlays should pin the digest. Release images carry
SBOM and provenance attestations (see [SECURITY.md](../SECURITY.md)).
