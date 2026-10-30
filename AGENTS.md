# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository. Read it
before changing anything; it encodes the conventions the CI enforces.

## What this repository is

- **The root is the primary artifact.** The Dockerfile packages DeepSeek
  Harness Web (dsh web) as a slim, non-root, multi-arch image. Everything else
  exists to support it or to demonstrate an implementation on top of it.
- **Every implementation is a self-contained example** under examples/<name>/.
  There is exactly one example for the agents-as-a-service platform; do not
  split it into parallel directories.
- **versions.env is the single source of truth** for the DSH ref, pnpm, the
  control-plane versions and the image names.
- **Docs describe the shipped behavior**, not intentions. If you change a
  manifest, port, env var or workflow, update the docs in the same change.
- **CHANGELOG.md is generated** by git-cliff from Conventional Commits. Never
  edit it by hand.

## Golden rules

1. Keep the Dockerfile working for both linux/amd64 and linux/arm64; it must
   stay the first-class artifact.
2. Never hard-code a version, image tag or registry path. Edit versions.env and
   run make set-version when bumping.
3. Never commit a real secret or usable credential. Secret-shaped YAML lives in
   *.yaml.example files; keys are documented in .env.sample only.
4. Every manifest under examples/*/manifests/ must parse and pass a server-side
   dry-run in CI. Keep manifests numbered (NN-*.yaml), and keep exposure
   variants under manifests/exposure/<variant>/ documented by the single
   manifests/exposure/README.md (variants are mutually exclusive).
5. Do not edit CHANGELOG.md, and do not move or delete published tags.
   Releasing is documented in docs/versioning.md.
6. Keep the gateway HTTP/WebSocket contract stable unless the change is
   explicitly about it; clients and examples depend on it.

## Repository map

| Path | What lives there |
| --- | --- |
| Dockerfile | Main DSH Web image (build + runtime stages, entrypoint) |
| versions.env | Every pinned version and image name |
| Makefile | Canonical local entrypoint |
| build.sh | Deprecated shim that forwards to make |
| docs/ | main-image.md, examples.md, gitflow.md, versioning.md |
| examples/ | One implementation per directory (contract in docs/examples.md) |
| examples/dsh-agents-as-a-service/ | The multi-tenant platform example (manifests, gateway, docs, scripts) |
| examples/minimal-single-tenant/ | The just-one-DSH-Web quickstart |
| scripts/ | check-versions.sh, check-examples.sh, check-manifests.py, set-version.sh |
| .dsh/skills/ | Project-local DSH skills (auto-discovered); dsh-agent-sandbox-platform covers platform operation, MCP limits and extension recipes |
| .github/workflows/ | ci.yaml (lint, dependency-review), ci-python.yaml, ci-manifests.yaml, ci-image.yaml (amd64 build + smoke test on Dockerfile/versions.env changes), reusable _build-image.yaml, build-*, release.yaml, e2e-kind.yaml, pr-title.yaml, codeql.yml |

## Commands

    make check        # versions, example layout and manifest YAML (run before every commit)
    make test         # gateway unit tests (109+ tests)
    make lint         # ruff + shellcheck (+ yamllint/actionlint when installed)
    make image        # build the main DSH Web image (slow: clones and builds DSH)
    make gateway      # build the example gateway image (fast)
    make e2e          # full kind walkthrough; needs a cluster with agent-sandbox
    make changelog    # regenerate CHANGELOG.md with git-cliff (release only)
    make release-dry-run
    make set-version DSH_VERSION=dsh-vX.Y.Z RELEASE_REVISION=rN

No cluster is needed for check, test, lint, gateway or image. Only make e2e
needs kind plus the agent-sandbox CRDs (see the example README).

## Testing expectations per change

| Change | Minimum verification |
| --- | --- |
| Documentation only | make check; verify relative links |
| Example manifest | make check; kubectl apply --dry-run=server in CI |
| Optional hardening (gVisor, nodeSelector, NetworkPolicy, provider keys) | make check; server dry-run in CI; manual verification on a capable cluster |
| Gateway code | make test; make lint; add or update tests |
| Dockerfile / versions.env | make check; the CI `build-amd64` job (`ci-image.yaml`) builds and smoke-tests the image on the PR; run `make image` locally only when you need the image |
| Workflow | actionlint in CI; keep actions SHA-pinned; no hard-coded versions |
| New example | make check; all files described in docs/examples.md |
| Project skill (.dsh/skills/) | make check; frontmatter and size check; shellcheck skill scripts; load it with the skill tool |

## Versioning and releases

- Release identity = DSH_VERSION, optionally with a repository revision suffix
  for re-releases at the same DSH ref (current identity:
  dsh-v0.2.1-rc.5).
- The current identity is dsh-v0.2.1-rc.5 (stored in versions.env). It is the
  first release at this DSH ref, so it carries no revision suffix; a
  repository-only change at the same ref publishes -r2.
- Channels: release tags are immutable; latest follows the newest release;
  main is the development channel for both image packages.
- A broken release is fixed by publishing -r(N+1), never by re-tagging.
- The full procedure, compatibility table and rollback rules are in
  docs/versioning.md.

## Branch and PR conventions

- Trunk-based with release branches, documented in docs/gitflow.md.
- Branch names: feat/, fix/, docs/, chore/, refactor/, test/.
- Conventional Commit subjects and PR titles (feat(scope): ..., fix(scope): ...).
- PRs are squash-merged; main stays linear and protected.
- Required checks: lint, python, manifests, pr-title, CodeQL, dependency-review.
- Keep diffs scoped; update docs with the code; never mix a version bump with
  unrelated changes.

## Adding an example

1. Read docs/examples.md and examples/README.md.
2. Create examples/<name>/ with README.md, manifests/NN-*.yaml, optional
   manifests/exposure/<variant>/ (plus README), optional components with their
   own Dockerfile/README/tests, and scripts/ (executable, idempotent,
   self-cleaning).
3. Publish component images under <registry>/<owner>/<repo>/<example>/<component>.
4. Add the example to examples/README.md; scripts/check-examples.sh enforces
   the layout.

## Security notes

- AUTH_PROXY_HEADER is opt-in. Only enable it when the gateway Service is
  reachable exclusively through the authenticating proxy; otherwise any client
  can spoof X-Forwarded-User.
- The example gateway reads the DSH launch token from pod logs. Treat pod logs
  as sensitive and keep its RBAC namespace-scoped.
- The example shares provider keys (dsh-provider-keys plus the optional
  dsh-provider-keys-extra) across sandboxes; do not describe it as
  tenant-isolated.
- gVisor, nodeSelector and the NetworkPolicies are opt-in and are not active in
  the base manifests; do not describe the base example as hardened.
- TLS terminates outside the gateway; set COOKIE_SECURE=true for HTTPS and keep
  the base manifest safe for local HTTP.

## Ask before

- Changing release workflows, tag formats or published image names.
- Editing the gateway's public routes or auth semantics.
- Adding a new top-level tool, service or dependency to the main image.
- Rewriting generated files (CHANGELOG.md) or git history.

## Where to look first

- [docs/main-image.md](docs/main-image.md) - container contract and tags
- [docs/examples.md](docs/examples.md) - example authoring rules
- [docs/gitflow.md](docs/gitflow.md) - branches, PRs, required checks
- [docs/versioning.md](docs/versioning.md) - versioning, changelog, releases
- [.dsh/skills/dsh-agent-sandbox-platform/SKILL.md](.dsh/skills/dsh-agent-sandbox-platform/SKILL.md) - platform operations, MCP limits, extension recipes
- [CONTRIBUTING.md](CONTRIBUTING.md) - development setup
- [SECURITY.md](SECURITY.md) - vulnerability reporting and threat notes
