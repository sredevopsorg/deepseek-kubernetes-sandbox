#!/usr/bin/env bash
# Deprecated compatibility shim: "make" is the canonical entrypoint.
#
#   ./build.sh            -> make run   (build the main image, then run it)
#   ./build.sh web        -> make image
#   ./build.sh gateway    -> make gateway
#
# Use the Makefile targets directly for everything else (test, lint, check, e2e).
set -euo pipefail
cd "$(dirname "$0")"

echo "build.sh is deprecated; use 'make image', 'make run' or 'make gateway'." >&2

case "${1:-run}" in
  run) exec make run ;;
  web) exec make image ;;
  gateway) exec make gateway ;;
  *) echo "usage: $0 [run|web|gateway]" >&2; exit 2 ;;
esac
