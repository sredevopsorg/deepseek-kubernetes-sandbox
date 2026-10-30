#!/usr/bin/env bash
# Enforce the example contract documented in docs/examples.md.
# Run by "make check" locally and by the CI lint job.
set -euo pipefail
cd "$(dirname "$0")/.."

index=examples/README.md
fail=0
err() { echo "check-examples: $*" >&2; fail=1; }

[ -f "$index" ] || err "missing $index"

for dir in examples/*/; do
  name="$(basename "$dir")"
  [ "$name" = "_template" ] && continue

  [ -f "${dir}README.md" ] || err "$name: missing README.md"
  [ -d "${dir}manifests" ] || err "$name: missing manifests/"

  if [ -d "${dir}manifests" ]; then
    count=$(find "${dir}manifests" -maxdepth 1 -type f -name '*.yaml' | wc -l | tr -d ' ')
    [ "$count" -gt 0 ] || err "$name: manifests/ has no top-level *.yaml files"
    for manifest in "${dir}"manifests/*.yaml; do
      [ -e "$manifest" ] || continue
      case "$(basename "$manifest")" in
        [0-9][0-9]-*.yaml) ;;
        *) err "$name: top-level manifest $(basename "$manifest") must be named NN-*.yaml" ;;
      esac
    done
  fi

  stray=$(find "$dir" -maxdepth 1 -type f -name '*.yaml' | wc -l | tr -d ' ')
  [ "$stray" -eq 0 ] || err "$name: manifests belong under manifests/, not at the example root"

  if [ -d "${dir}manifests/exposure" ] && [ ! -f "${dir}manifests/exposure/README.md" ]; then
    err "$name: manifests/exposure/ requires a README.md describing the variants"
  fi

  if [ -d "${dir}manifests/optional" ] && [ ! -f "${dir}manifests/optional/README.md" ]; then
    err "$name: manifests/optional/ requires a README.md describing the opt-in manifests"
  fi

  for component in "$dir"*/; do
    [ -d "$component" ] || continue
    case "$(basename "$component")" in
      manifests|scripts|docs) continue ;;
    esac
    if [ -f "${component}Dockerfile" ] && [ ! -f "${component}README.md" ]; then
      err "$name/$(basename "$component"): component with a Dockerfile needs a README.md"
    fi
  done

  if ! grep -Fq "examples/$name/" "$index"; then
    err "$name: not listed in examples/README.md"
  fi

  for script in "$dir"scripts/*.sh; do
    [ -e "$script" ] || continue
    [ -x "$script" ] || err "$script: must be executable"
  done
done

if [ "$fail" -ne 0 ]; then
  echo "check-examples: FAILED" >&2
  exit 1
fi
echo "check-examples: ok"
