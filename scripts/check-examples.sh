#!/usr/bin/env bash
# Enforce the example contract documented in docs/examples.md.
# Run by "make check" locally and by the CI lint job.
#
# Two manifest layouts are accepted:
#   * plain:    manifests/NN-*.yaml, applied with kubectl apply -f
#   * kustomize: manifests/base/NN-*.yaml + manifests/base/kustomization.yaml;
#     overlays/components live in sibling directories under
#     manifests/exposure/<variant>/ and manifests/optional/<feature>/, each
#     with its own kustomization.yaml, and are applied with kubectl apply -k
# kustomization.yaml / kustomization.yml / Kustomization files describe a build,
# not a Kubernetes object, so they are never checked against the NN-*.yaml rule.
set -euo pipefail
cd "$(dirname "$0")/.."

index=examples/README.md
fail=0
err() { echo "check-examples: $*" >&2; fail=1; }

is_kustomization() {
  case "$(basename "$1")" in
    kustomization.yaml|kustomization.yml|Kustomization) return 0 ;;
    *) return 1 ;;
  esac
}

# A manifest directory must hold at least one numbered manifest and nothing that
# pretends to be one but is not named NN-*.yaml. Kustomization files are ignored.
check_manifest_dir() { # <example> <directory>
  local name="$1" dir="$2" rel="${2#examples/}" count=0 manifest
  for manifest in "$dir"/*.yaml; do
    [ -e "$manifest" ] || continue
    if is_kustomization "$manifest"; then
      continue
    fi
    count=$((count + 1))
    case "$(basename "$manifest")" in
      [0-9][0-9]-*.yaml) ;;
      *) err "$name: manifest $(basename "$manifest") in $rel/ must be named NN-*.yaml" ;;
    esac
  done
  [ "$count" -gt 0 ] || err "$name: $rel/ has no top-level NN-*.yaml manifests"
}

for dir in examples/*/; do
  name="$(basename "$dir")"
  [ "$name" = "_template" ] && continue

  [ -f "${dir}README.md" ] || err "$name: missing README.md"
  [ -d "${dir}manifests" ] || err "$name: missing manifests/"

  if [ -d "${dir}manifests" ]; then
    if [ -d "${dir}manifests/base" ]; then
      # Kustomize layout: the portable base is the numbered tree; exposure and
      # optional roots are kustomization overlays, not numbered manifests.
      check_manifest_dir "$name" "${dir}manifests/base"
    else
      check_manifest_dir "$name" "${dir}manifests"
    fi
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
