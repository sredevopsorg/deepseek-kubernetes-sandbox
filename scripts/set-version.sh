#!/usr/bin/env bash
# Rewrite every version pin in one pass, then leave the tree ready for
# "make check".
#
#   ./scripts/set-version.sh dsh-v0.2.2-rc.1        # new DSH ref, no revision
#   ./scripts/set-version.sh dsh-v0.2.1-rc.5 r3    # same ref, new repo revision
#
# For a new DSH ref the script resolves the tag's commit with "git ls-remote"
# (peeled ^{} commit first, tag ref as fallback) and rewrites DSH_COMMIT in
# versions.env, the Dockerfile ARG default and .env.sample. A revision-only bump
# at the same ref keeps the existing commit and needs no network access.
set -euo pipefail
cd "$(dirname "$0")/.."

new_version="${1:-}"
new_revision="${2:-}"

case "$new_version" in
  dsh-v[0-9]*) ;;
  *) echo "usage: $0 <dsh-vX.Y.Z> [rN]" >&2; exit 2 ;;
esac

if [ -n "$new_revision" ]; then
  case "$new_revision" in
    r[2-9]|r[1-9][0-9]*) ;;
    *) echo "revision must look like r2, r3, ..." >&2; exit 2 ;;
  esac
fi

# shellcheck source=/dev/null
. ./versions.env

old_version="$DSH_VERSION"
old_revision="${RELEASE_REVISION:-}"
old_commit="${DSH_COMMIT:-}"

old_identity="$old_version"
[ -z "$old_revision" ] || old_identity="$old_version-$old_revision"
new_identity="$new_version"
[ -z "$new_revision" ] || new_identity="$new_version-$new_revision"

if [ "$old_identity" = "$new_identity" ]; then
  echo "set-version: already at $new_identity"
  exit 0
fi

# Resolve the commit the new upstream tag points at. Annotated tags expose a
# peeled ^{} line (the commit) and a tag-object line; prefer the peeled commit
# and fall back to the tag ref for lightweight tags. A repository-only revision
# bump keeps the already pinned commit, so it needs no network.
new_commit="$old_commit"
if [ "$new_version" != "$old_version" ] || [ -z "$new_commit" ]; then
  if ! refs="$(git ls-remote --tags --exit-code "$DSH_REPO" \
        "refs/tags/$new_version^{}" "refs/tags/$new_version")"; then
    echo "set-version: upstream tag '$new_version' not found in $DSH_REPO" >&2
    exit 1
  fi
  new_commit="$(printf '%s\n' "$refs" \
    | awk -v t="refs/tags/$new_version^{}" '$2 == t {print $1}' | head -n1)"
  if [ -z "$new_commit" ]; then
    new_commit="$(printf '%s\n' "$refs" \
      | awk -v t="refs/tags/$new_version" '$2 == t {print $1}' | head -n1)"
  fi
  if ! printf '%s' "$new_commit" | grep -qE '^[0-9a-f]{40}$'; then
    echo "set-version: could not resolve a commit for '$new_version' in $DSH_REPO" >&2
    exit 1
  fi
fi

# Replace the identity first (it contains the base version), then the base
# version itself. CHANGELOG.md is generated from tags, so it is left alone.
while IFS= read -r file; do
  [ -f "$file" ] || continue
  case "$file" in
    .env.sample|Dockerfile|Makefile|*.md|*.yaml|*.yml|*.sh|*.py|*.toml|*.env) ;;
    *) continue ;;
  esac
  sed -i "s/$old_identity/$new_identity/g; s/$old_version/$new_version/g" "$file"
done < <(git ls-files)

# versions.env is authoritative: write these keys explicitly so an empty
# revision is stored as an empty value.
sed -i "s/^DSH_VERSION=.*/DSH_VERSION=$new_version/" versions.env
sed -i "s/^RELEASE_REVISION=.*/RELEASE_REVISION=$new_revision/" versions.env
sed -i "s/^DSH_COMMIT=.*/DSH_COMMIT=$new_commit/" versions.env

# Values stored separately from the full identity: Dockerfile ARG defaults and
# the human-facing sample cannot be reached by the identity replacement above.
sed -i "s/^ARG DSH_VERSION=.*/ARG DSH_VERSION=$new_version/" Dockerfile
sed -i "s/^ARG RELEASE_REVISION=.*/ARG RELEASE_REVISION=$new_revision/" Dockerfile
sed -i "s/^ARG DSH_COMMIT=.*/ARG DSH_COMMIT=$new_commit/" Dockerfile
sed -i "s/^DSH_VERSION=.*/DSH_VERSION=$new_version/" .env.sample
sed -i "s/^RELEASE_REVISION=.*/RELEASE_REVISION=$new_revision/" .env.sample
sed -i "s/^DSH_COMMIT=.*/DSH_COMMIT=$new_commit/" .env.sample

# The human-facing build examples embed the full SHA, which the version
# replacement above cannot see. Keep them in step; check-versions.sh verifies.
if [ -n "$old_commit" ] && [ "$old_commit" != "$new_commit" ]; then
  for doc in docs/main-image.md docs/versioning.md; do
    [ -f "$doc" ] || continue
    sed -i "s/$old_commit/$new_commit/g" "$doc"
  done
fi

echo "set-version: $old_identity -> $new_identity"
echo "set-version: DSH_COMMIT=$new_commit"
echo "set-version: run 'make check' and review the diff"
