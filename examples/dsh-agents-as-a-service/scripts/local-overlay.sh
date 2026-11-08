#!/usr/bin/env bash
# Render (default) or apply the cluster-specific local overlay for this example.
#
# The shipped manifests are portable: gVisor, node-pool labels, storage class,
# the public host, SSO hop counts and image pins are per-install values. Record
# them in manifests/local/values.env (copy values.env.example) and run this
# script; without --apply it only prints the kubectl commands.
#
# Read-only unless --apply is given; idempotent (every patch sets a field).
#
# Usage: scripts/local-overlay.sh [--apply] [--values FILE]
set -euo pipefail
cd "$(dirname "$0")/.."

APPLY=0
VALUES_FILE=manifests/local/values.env

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --values)
      shift
      [ "$#" -gt 0 ] || { echo "FAIL  --values needs a file" >&2; exit 2; }
      VALUES_FILE=$1
      ;;
    -h | --help) usage; exit 0 ;;
    *) echo "FAIL  unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

command -v kubectl >/dev/null 2>&1 || { echo "FAIL  kubectl is required" >&2; exit 1; }
[ -f "$VALUES_FILE" ] || {
  echo "FAIL  missing $VALUES_FILE; copy manifests/local/values.env.example and edit it" >&2
  exit 1
}

# shellcheck source=/dev/null
. "$VALUES_FILE"

NAMESPACE=${NAMESPACE:-dsh-demo}
TEMPLATE_NAME=${TEMPLATE_NAME:-dsh-web}
POOL_NAME=${POOL_NAME:-dsh-pool}
GATEWAY_DEPLOY=${GATEWAY_DEPLOY:-dsh-gateway}
OAUTH_DEPLOY=${OAUTH_DEPLOY:-oauth2-proxy}

say() { printf '%s\n' "$*"; }

# kc: print the kubectl command in dry-run mode, execute it with --apply.
kc() {
  if [ "$APPLY" -eq 1 ]; then
    kubectl "$@"
  else
    printf 'kubectl'
    printf ' %q' "$@"
    printf '\n'
  fi
}

patch_json() {
  local kind=$1 name=$2 patch=$3
  kc -n "$NAMESPACE" patch "$kind" "$name" --type=json -p "$patch"
}

patch_merge() {
  local kind=$1 name=$2 patch=$3
  kc -n "$NAMESPACE" patch "$kind" "$name" --type=merge -p "$patch"
}

# --- Preconditions: the overlay patches existing resources, it never creates --

if ! kubectl -n "$NAMESPACE" get sandboxtemplate "$TEMPLATE_NAME" >/dev/null 2>&1; then
  echo "FAIL  SandboxTemplate $TEMPLATE_NAME not found in $NAMESPACE; apply the base manifests first (kubectl apply -f manifests/)." >&2
  exit 1
fi
if ! kubectl -n "$NAMESPACE" get sandboxwarmpool "$POOL_NAME" >/dev/null 2>&1; then
  echo "FAIL  SandboxWarmPool $POOL_NAME not found in $NAMESPACE; apply the base manifests first." >&2
  exit 1
fi
if ! kubectl -n "$NAMESPACE" get deployment "$GATEWAY_DEPLOY" >/dev/null 2>&1; then
  echo "FAIL  Deployment $GATEWAY_DEPLOY not found in $NAMESPACE; apply the base manifests first." >&2
  exit 1
fi

CONTAINER_NAME=$(kubectl -n "$NAMESPACE" get sandboxtemplate "$TEMPLATE_NAME" \
  -o jsonpath='{.spec.podTemplate.spec.containers[0].name}')
if [ "$CONTAINER_NAME" != "dsh-web" ]; then
  echo "FAIL  expected SandboxTemplate $TEMPLATE_NAME container[0] to be dsh-web, found '$CONTAINER_NAME'" >&2
  exit 1
fi

CLAIM_TEMPLATES=$(kubectl -n "$NAMESPACE" get sandboxtemplate "$TEMPLATE_NAME" \
  -o jsonpath='{range .spec.volumeClaimTemplates[*]}{.metadata.name}{"\n"}{end}' | grep -c . || true)
if [ "$CLAIM_TEMPLATES" != "1" ]; then
  echo "FAIL  expected exactly one volumeClaimTemplate in $TEMPLATE_NAME, found $CLAIM_TEMPLATES" >&2
  exit 1
fi

# --- Sandbox template ------------------------------------------------------

say "== SandboxTemplate $TEMPLATE_NAME"

# Repo baseline, not a cluster value: tenant pods must never receive a
# ServiceAccount token (older installs predate this field).
patch_json sandboxtemplate "$TEMPLATE_NAME" \
  '[{"op":"add","path":"/spec/podTemplate/spec/automountServiceAccountToken","value":false}]'

if [ -n "${RUNTIME_CLASS:-}" ]; then
  patch_json sandboxtemplate "$TEMPLATE_NAME" \
    "[{\"op\":\"add\",\"path\":\"/spec/podTemplate/spec/runtimeClassName\",\"value\":\"$RUNTIME_CLASS\"}]"
fi

if [ -n "${NODE_SELECTOR_KEY:-}" ] && [ -n "${NODE_SELECTOR_VALUE:-}" ]; then
  # JSON pointer escaping: '~' -> '~0' first, then '/' -> '~1'.
  NODE_KEY_ESCAPED=${NODE_SELECTOR_KEY//~/~0}
  NODE_KEY_ESCAPED=${NODE_KEY_ESCAPED//\//~1}
  patch_json sandboxtemplate "$TEMPLATE_NAME" \
    "[{\"op\":\"add\",\"path\":\"/spec/podTemplate/spec/nodeSelector\",\"value\":{\"$NODE_KEY_ESCAPED\":\"$NODE_SELECTOR_VALUE\"}}]"
fi

if [ -n "${DSH_TRUSTED_HOSTS:-}" ]; then
  ENV_NAMES=$(kubectl -n "$NAMESPACE" get sandboxtemplate "$TEMPLATE_NAME" \
    -o jsonpath='{range .spec.podTemplate.spec.containers[0].env[*]}{.name}{"\n"}{end}')
  ENV_INDEX=$(printf '%s\n' "$ENV_NAMES" | grep -nxF 'DSH_TRUSTED_HOSTS' | cut -d: -f1 || true)
  if [ -n "$ENV_INDEX" ]; then
    patch_json sandboxtemplate "$TEMPLATE_NAME" \
      "[{\"op\":\"replace\",\"path\":\"/spec/podTemplate/spec/containers/0/env/$((ENV_INDEX - 1))/value\",\"value\":\"$DSH_TRUSTED_HOSTS\"}]"
  else
    patch_json sandboxtemplate "$TEMPLATE_NAME" \
      "[{\"op\":\"add\",\"path\":\"/spec/podTemplate/spec/containers/0/env/-\",\"value\":{\"name\":\"DSH_TRUSTED_HOSTS\",\"value\":\"$DSH_TRUSTED_HOSTS\"}}]"
  fi
fi

if [ -n "${STORAGE_CLASS:-}" ]; then
  patch_json sandboxtemplate "$TEMPLATE_NAME" \
    "[{\"op\":\"add\",\"path\":\"/spec/volumeClaimTemplates/0/spec/storageClassName\",\"value\":\"$STORAGE_CLASS\"}]"
fi

if [ -n "${STORAGE_SIZE:-}" ]; then
  patch_json sandboxtemplate "$TEMPLATE_NAME" \
    "[{\"op\":\"add\",\"path\":\"/spec/volumeClaimTemplates/0/spec/resources/requests/storage\",\"value\":\"$STORAGE_SIZE\"}]"
fi

if [ -n "${FAILED_POD_POLICY:-}" ]; then
  patch_json sandboxtemplate "$TEMPLATE_NAME" \
    "[{\"op\":\"add\",\"path\":\"/spec/failedPodPolicy\",\"value\":\"$FAILED_POD_POLICY\"}]"
fi

# --- Warm pool -------------------------------------------------------------

say "== SandboxWarmPool $POOL_NAME"
# The shipped warm pool references the repo blueprint; keep it aligned after a
# migration from a hand-made template.
patch_json sandboxwarmpool "$POOL_NAME" \
  "[{\"op\":\"add\",\"path\":\"/spec/sandboxTemplateRef/name\",\"value\":\"$TEMPLATE_NAME\"}]"

if [ -n "${WARM_POOL_REPLICAS:-}" ]; then
  case "$WARM_POOL_REPLICAS" in
    '' | *[!0-9]*)
      echo "FAIL  WARM_POOL_REPLICAS must be a non-negative integer, got '$WARM_POOL_REPLICAS'" >&2
      exit 1
      ;;
    *)
      patch_json sandboxwarmpool "$POOL_NAME" \
        "[{\"op\":\"add\",\"path\":\"/spec/replicas\",\"value\":$WARM_POOL_REPLICAS}]"
      ;;
  esac
fi

# --- Gateway ---------------------------------------------------------------

say "== Deployment $GATEWAY_DEPLOY"

if [ -n "${GATEWAY_IMAGE:-}" ]; then
  kc -n "$NAMESPACE" set image "deploy/$GATEWAY_DEPLOY" "gateway=$GATEWAY_IMAGE"
fi

if [ -n "${GATEWAY_IMAGE_PULL_POLICY:-}" ]; then
  kc -n "$NAMESPACE" patch deployment "$GATEWAY_DEPLOY" --type=strategic \
    -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"gateway\",\"imagePullPolicy\":\"$GATEWAY_IMAGE_PULL_POLICY\"}]}}}}"
fi

# Only set this once the deployed image actually serves /readyz (it ships in
# gateway builds after the readiness change): an older image answers 404 and the
# pod never becomes Ready.
if [ -n "${GATEWAY_READINESS_PATH:-}" ]; then
  kc -n "$NAMESPACE" patch deployment "$GATEWAY_DEPLOY" --type=json \
    -p "[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/readinessProbe/httpGet/path\",\"value\":\"$GATEWAY_READINESS_PATH\"}]"
fi

ENV_ARGS=()
if [ -n "${IDLE_TIMEOUT:-}" ]; then ENV_ARGS+=("IDLE_TIMEOUT=$IDLE_TIMEOUT"); fi
if [ -n "${WAKE_TIMEOUT:-}" ]; then ENV_ARGS+=("WAKE_TIMEOUT=$WAKE_TIMEOUT"); fi
if [ -n "${TRUSTED_PROXY_HOPS:-}" ]; then ENV_ARGS+=("TRUSTED_PROXY_HOPS=$TRUSTED_PROXY_HOPS"); fi
if [ -n "${COOKIE_SECURE:-}" ]; then ENV_ARGS+=("COOKIE_SECURE=$COOKIE_SECURE"); fi
if [ "${COOKIE_SECURE:-}" = "true" ]; then ENV_ARGS+=("ALLOW_INSECURE_COOKIES-"); fi
if [ -n "${AUTH_PROXY_HEADER:-}" ]; then
  ENV_ARGS+=("AUTH_PROXY_HEADER=$AUTH_PROXY_HEADER")
else
  # Non-SSO exposure: remove the identity header so it is never trusted.
  ENV_ARGS+=("AUTH_PROXY_HEADER-")
fi
if [ "${#ENV_ARGS[@]}" -gt 0 ]; then
  kc -n "$NAMESPACE" set env "deploy/$GATEWAY_DEPLOY" "${ENV_ARGS[@]}"
fi

# --- SSO exposure variant (optional; patched only when it exists) ----------

if kubectl -n "$NAMESPACE" get deployment "$OAUTH_DEPLOY" >/dev/null 2>&1; then
  say "== Deployment $OAUTH_DEPLOY"
  patch_merge deployment "$OAUTH_DEPLOY" \
    '{"spec":{"template":{"spec":{"automountServiceAccountToken":false}}}}'

  OAUTH_ARGS=$(kubectl -n "$NAMESPACE" get deployment "$OAUTH_DEPLOY" \
    -o jsonpath='{range .spec.template.spec.containers[0].args[*]}{@}{"\n"}{end}')

  ARG_PATCHES=()
  ensure_arg() {
    local prefix=$1 value=$2 index
    index=$(printf '%s\n' "$OAUTH_ARGS" | grep -nF -- "$prefix" | head -n1 | cut -d: -f1 || true)
    if [ -z "$index" ]; then
      ARG_PATCHES+=("{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args/-\",\"value\":\"$value\"}")
    fi
  }
  replace_arg() {
    local prefix=$1 value=$2 index
    index=$(printf '%s\n' "$OAUTH_ARGS" | grep -nF -- "$prefix" | head -n1 | cut -d: -f1 || true)
    if [ -n "$index" ]; then
      ARG_PATCHES+=("{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args/$((index - 1))\",\"value\":\"$value\"}")
    else
      echo "WARN  oauth2-proxy has no arg starting with '$prefix'; skipping" >&2
    fi
  }

  if [ -n "${OAUTH2_GITHUB_ORG:-}" ]; then
    replace_arg '--github-org=' "--github-org=$OAUTH2_GITHUB_ORG"
  fi
  if [ -n "${OAUTH2_EMAIL_DOMAIN:-}" ]; then
    replace_arg '--email-domain=' "--email-domain=$OAUTH2_EMAIL_DOMAIN"
  fi
  if [ -n "${OAUTH2_REDIRECT_URL:-}" ]; then
    replace_arg '--redirect-url=' "--redirect-url=$OAUTH2_REDIRECT_URL"
  fi
  # Repo hardening for the SSO variant: let probes and monitoring reach the
  # gateway's health endpoints through the proxy (they expose no user data).
  ensure_arg '--skip-auth-route=' '--skip-auth-route=^/(healthz|readyz)$'

  if [ "${#ARG_PATCHES[@]}" -gt 0 ]; then
    ARG_PATCH="[${ARG_PATCHES[0]}"
    for patch in "${ARG_PATCHES[@]:1}"; do
      ARG_PATCH+=",$patch"
    done
    ARG_PATCH+="]"
    patch_json deployment "$OAUTH_DEPLOY" "$ARG_PATCH"
  fi
fi

if kubectl -n "$NAMESPACE" get httproute dsh-gateway >/dev/null 2>&1; then
  if [ -n "${PUBLIC_HOST:-}" ]; then
    say "== HTTPRoute dsh-gateway"
    patch_json httproute dsh-gateway \
      "[{\"op\":\"replace\",\"path\":\"/spec/hostnames\",\"value\":[\"$PUBLIC_HOST\"]}]"
  fi
fi

if kubectl -n "$NAMESPACE" get networkpolicy oauth2-proxy-ingress >/dev/null 2>&1; then
  if [ -n "${TRAEFIK_NAMESPACE:-}" ]; then
    say "== NetworkPolicy oauth2-proxy-ingress"
    patch_merge networkpolicy oauth2-proxy-ingress \
      "{\"spec\":{\"ingress\":[{\"from\":[{\"namespaceSelector\":{\"matchLabels\":{\"kubernetes.io/metadata.name\":\"$TRAEFIK_NAMESPACE\"}},\"podSelector\":{\"matchLabels\":{\"app.kubernetes.io/name\":\"traefik\"}}}],\"ports\":[{\"protocol\":\"TCP\",\"port\":4180}]}]}}"
  fi
fi

if [ "$APPLY" -eq 0 ]; then
  say ""
  say "Dry run: nothing changed. Re-run with --apply to patch the cluster."
fi
