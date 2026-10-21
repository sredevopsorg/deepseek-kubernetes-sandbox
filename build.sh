#!/usr/bin/env bash
# Build the DSH Web and/or gateway images locally.
#
# Usage:
#   ./build.sh              # build the DSH Web image, then run it interactively
#   ./build.sh web          # build the DSH Web image only
#   ./build.sh gateway      # build the gateway image (examples/.../gateway/)
#
# Overridable environment:
#   DSH_VERSION    DSH git ref to build    (default dsh-v0.2.1-rc.2)
#   PNPM_VERSION   pnpm version            (default 11.28.5)
#   IMAGE          DSH Web image tag       (default dsh-web:<version>)
#   DSH_PORT       host port for docker run (default 3080)
#   GATEWAY_IMAGE  gateway image tag       (default ghcr.io/...:gateway)
#   RUN            0 to build without running (default 1)
set -euo pipefail
cd "$(dirname "$0")"

DSH_VERSION="${DSH_VERSION:-dsh-v0.2.1-rc.2}"
PNPM_VERSION="${PNPM_VERSION:-11.28.5}"
IMAGE="${IMAGE:-dsh-web:${DSH_VERSION#dsh-v}}"
GATEWAY_IMAGE="${GATEWAY_IMAGE:-ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:gateway}"
DSH_PORT="${DSH_PORT:-3080}"
RUN="${RUN:-1}"
GATEWAY_DIR=examples/dsh-agents-as-a-service/gateway

build_web() {
  docker build \
    --build-arg "DSH_VERSION=${DSH_VERSION}" \
    --build-arg "PNPM_VERSION=${PNPM_VERSION}" \
    -t "${IMAGE}" \
    "$@" \
    .
}

build_gateway() {
  docker build -t "${GATEWAY_IMAGE}" "$@" "${GATEWAY_DIR}/"
}

usage() {
  echo "usage: $0 [run|web|gateway] [extra docker build args...]" >&2
  exit 2
}

case "${1:-run}" in
  run)
    shift
    build_web "$@"
    if [ "${RUN}" = "1" ]; then
      docker run --rm -it \
        -p "${DSH_PORT}:3080" \
        -v dsh-home:/home/node/.dsh \
        -e DEEPSEEK_API_KEY \
        "${IMAGE}"
    fi
    ;;
  web)
    shift
    build_web "$@"
    ;;
  gateway)
    shift
    build_gateway "$@"
    ;;
  *)
    usage
    ;;
esac
