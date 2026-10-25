#!/usr/bin/env bash
# Rewrite every version pin in one pass, then leave the tree ready for
# "make check".
#
#   ./scripts/set-version.sh dsh-v0.2.2-rc.1        # new DSH ref, no revision
#   ./scripts/set-version.sh dsh-v0.2.1-rc.3 r3    # same ref, new repo revision
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
old_version="$(. ./versions.env; printf '%s' "$DSH_VERSION")"
# shellcheck source=/dev/null
old_revision="$(. ./versions.env; printf '%s' "${RELEASE_REVISION:-}")"

old_identity="$old_version"
[ -z "$old_revision" ] || old_identity="$old_version-$old_revision"
new_identity="$new_version"
[ -z "$new_revision" ] || new_identity="$new_version-$new_revision"

if [ "$old_identity" = "$new_identity" ]; then
  echo "set-version: already at $new_identity"
  exit 0
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

# versions.env is authoritative: write both keys explicitly so an empty
# revision is stored as an empty value.
sed -i "s/^DSH_VERSION=.*/DSH_VERSION=$new_version/" versions.env
sed -i "s/^RELEASE_REVISION=.*/RELEASE_REVISION=$new_revision/" versions.env

# Values stored separately from the full identity: Dockerfile ARG defaults and
# the human-facing sample cannot be reached by the identity replacement above.
sed -i "s/^ARG DSH_VERSION=.*/ARG DSH_VERSION=$new_version/" Dockerfile
sed -i "s/^ARG RELEASE_REVISION=.*/ARG RELEASE_REVISION=$new_revision/" Dockerfile
sed -i "s/^DSH_VERSION=.*/DSH_VERSION=$new_version/" .env.sample
sed -i "s/^RELEASE_REVISION=.*/RELEASE_REVISION=$new_revision/" .env.sample

echo "set-version: $old_identity -> $new_identity"
echo "set-version: run 'make check' and review the diff"
