#!/usr/bin/env bash
# Verify that every version pin in the repository agrees with versions.env.
# Run by "make check" locally and by the CI lint job.
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck source=/dev/null
. ./versions.env

IDENTITY="$DSH_VERSION"
if [ -n "${RELEASE_REVISION:-}" ]; then
  IDENTITY="$DSH_VERSION-$RELEASE_REVISION"
fi

fail=0
err() { echo "check-versions: $*" >&2; fail=1; }
expect() { # <file> <literal>
  if ! grep -Fq -- "$2" "$1"; then
    err "$1: expected to contain: $2"
  fi
}

# Main Dockerfile build args
expect Dockerfile "ARG DSH_VERSION=$DSH_VERSION"
expect Dockerfile "ARG DSH_COMMIT=$DSH_COMMIT"
expect Dockerfile "ARG PNPM_VERSION=$PNPM_VERSION"
expect Dockerfile "ARG RELEASE_REVISION=$RELEASE_REVISION"
expect Dockerfile "ARG NODE_BUILD_IMAGE=$NODE_BUILD_IMAGE"
expect Dockerfile "ARG NODE_RUNTIME_IMAGE=$NODE_RUNTIME_IMAGE"
# The build stage must fail closed when the tag no longer resolves to DSH_COMMIT.
expect Dockerfile "git -C /src rev-parse HEAD"

# Example gateway build base image
expect examples/dsh-agents-as-a-service/gateway/Dockerfile \
  "ARG GATEWAY_PYTHON_IMAGE=$GATEWAY_PYTHON_IMAGE"

# Local configuration mirrored for humans
expect .env.sample "DSH_VERSION=$DSH_VERSION"
expect .env.sample "DSH_COMMIT=$DSH_COMMIT"
expect .env.sample "PNPM_VERSION=$PNPM_VERSION"
expect .env.sample "RELEASE_REVISION=$RELEASE_REVISION"

# Human-facing docs embed the resolved commit in the build example and table.
expect docs/main-image.md "$DSH_COMMIT"
expect docs/versioning.md "$DSH_COMMIT"

# Every container image is pinned to an immutable multi-arch index digest, not
# a mutable tag. This is a supply-chain invariant: don't "fix" a failing check
# by dropping the digest.
for image_var in NODE_BUILD_IMAGE NODE_RUNTIME_IMAGE GATEWAY_PYTHON_IMAGE OAUTH2_PROXY_IMAGE; do
  image_value="${!image_var:-}"
  if ! printf '%s' "$image_value" | grep -qE '@sha256:[0-9a-f]{64}$'; then
    err "versions.env: $image_var must be digest-pinned (name:tag@sha256:<64 hex>), got: ${image_value:-<unset>}"
  fi
done

if ! printf '%s' "${DSH_COMMIT:-}" | grep -qE '^[0-9a-f]{40}$'; then
  err "versions.env: DSH_COMMIT must be a full 40-character commit SHA, got: ${DSH_COMMIT:-<unset>}"
fi

# Build entrypoints must pass DSH_COMMIT rather than trusting the tag alone.
expect Makefile "--build-arg DSH_COMMIT=\$(DSH_COMMIT)"
for wf in build-main.yaml release.yaml ci-image.yaml; do
  expect ".github/workflows/$wf" "DSH_COMMIT="
done

# Image references in the example manifests
for f in \
  examples/dsh-agents-as-a-service/manifests/10-sandboxtemplate.yaml \
  examples/minimal-single-tenant/manifests/10-sandboxtemplate.yaml; do
  expect "$f" "image: $GHCR_IMAGE:$IDENTITY"
done
expect examples/dsh-agents-as-a-service/manifests/50-gateway.yaml "image: $GATEWAY_IMAGE:$IDENTITY"

# The oauth2-proxy exposure variant pins the digest, not the mutable tag.
expect examples/dsh-agents-as-a-service/manifests/exposure/gateway-api-sso/72-oauth2-proxy.yaml \
  "image: $OAUTH2_PROXY_IMAGE"

# The gateway image must live below the repository image (one package per
# example component, no unrelated top-level packages).
case "$GATEWAY_IMAGE" in
  "$GHCR_IMAGE"/*) ;;
  *) err "GATEWAY_IMAGE ($GATEWAY_IMAGE) must be nested below GHCR_IMAGE ($GHCR_IMAGE)" ;;
esac

# Build tooling must read versions.env instead of hard-coding a version.
# (scripts/ is excluded: set-version.sh documents usage examples in comments.)
while IFS= read -r hit; do
  [ -n "$hit" ] || continue
  err "hard-coded version outside versions.env: $hit"
done < <(grep -rnE 'dsh-v[0-9]' .github/workflows Makefile 2>/dev/null || true)

# Prose may mention versions, but only the current identity or its base tag.
while IFS= read -r token; do
  [ -n "$token" ] || continue
  case "$token" in
    "$IDENTITY"|"$DSH_VERSION") ;;
    *) err "stale version reference in docs: $token" ;;
  esac
done < <(grep -rhoE 'dsh-v[0-9][A-Za-z0-9._-]*' \
  --include='*.md' --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.uvcache \
  --exclude=CHANGELOG.md . | sort -u)

if [ "$fail" -ne 0 ]; then
  echo "check-versions: FAILED" >&2
  exit 1
fi
echo "check-versions: ok ($IDENTITY)"
