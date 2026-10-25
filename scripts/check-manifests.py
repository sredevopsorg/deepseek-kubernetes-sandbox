#!/usr/bin/env python3
"""Parse every example manifest and assert the minimal Kubernetes shape."""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - environment guard
    sys.exit("check-manifests: PyYAML is required (python -m pip install pyyaml)")

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted(ROOT.glob("examples/*/manifests/**/*.yaml"))

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

if errors:
    print("\n".join(errors), file=sys.stderr)
    sys.exit(1)

print(f"check-manifests: ok ({len(FILES)} files, {documents} documents)")
