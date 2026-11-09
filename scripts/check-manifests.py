#!/usr/bin/env python3
"""Parse every example manifest and assert the minimal Kubernetes shape.

Kustomization files (kustomization.yaml, kustomization.yml, Kustomization) and
every file under a patches/ directory are skipped: a kustomization describes a
build and a patch document is a partial strategic-merge or JSON-6902 fragment,
so neither is a standalone Kubernetes object. The objects they compose are
validated as full manifests under examples/*/manifests/.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - environment guard
    sys.exit("check-manifests: PyYAML is required (python -m pip install pyyaml)")

ROOT = Path(__file__).resolve().parent.parent
KUSTOMIZATION_NAMES = {"kustomization.yaml", "kustomization.yml", "Kustomization"}


def is_build_input(path: Path) -> bool:
    """True for kustomization roots and patches/ fragments (not objects)."""
    return (
        path.name in KUSTOMIZATION_NAMES
        or "patches" in path.relative_to(ROOT).parts
    )


ALL_FILES = sorted(ROOT.glob("examples/*/manifests/**/*.yaml"))
FILES = [path for path in ALL_FILES if not is_build_input(path)]
SKIPPED = len(ALL_FILES) - len(FILES)

if not FILES:
    sys.exit("check-manifests: no manifests found under examples/*/manifests/")

errors: list[str] = []
documents = 0
for path in FILES:
    relative = path.relative_to(ROOT)
    try:
        loaded = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except yaml.YAMLError as exc:
        errors.append(f"{relative}: invalid YAML: {exc}")
        continue
    for position, document in enumerate(loaded, start=1):
        if document is None:
            continue
        documents += 1
        if not isinstance(document, dict):
            errors.append(f"{relative} document {position}: not a mapping")
            continue
        missing = [key for key in ("apiVersion", "kind", "metadata") if key not in document]
        if missing:
            errors.append(f"{relative} document {position}: missing {', '.join(missing)}")
            continue
        metadata = document["metadata"]
        if not isinstance(metadata, dict) or not metadata.get("name"):
            errors.append(f"{relative} document {position}: metadata.name is required")
        # A SandboxTemplate is the blueprint for every tenant pod, and the
        # gateway ServiceAccount can read pods/log in this namespace (DSH launch
        # tokens). The template must keep the SA token out of the sandbox.
        if document.get("kind") == "SandboxTemplate":
            pod_spec = ((document.get("spec") or {}).get("podTemplate") or {}).get("spec") or {}
            if pod_spec.get("automountServiceAccountToken") is not False:
                errors.append(
                    f"{relative} document {position}: SandboxTemplate must set "
                    "spec.podTemplate.spec.automountServiceAccountToken: false"
                )

if errors:
    print("\n".join(errors), file=sys.stderr)
    sys.exit(1)

print(
    f"check-manifests: ok ({len(FILES)} files, {documents} documents; "
    f"skipped {SKIPPED} kustomization/patch files)"
)
