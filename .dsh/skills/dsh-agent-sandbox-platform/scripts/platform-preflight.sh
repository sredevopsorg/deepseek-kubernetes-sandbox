#!/usr/bin/env bash
# Read-only preflight for the DSH agents-as-a-service platform.
#
# Checks the kubectl target, agent-sandbox CRDs/version, warm pools, claims,
# sandboxes, gateway and (optionally) an agent-sandbox MCP server. It never
# mutates the cluster, never reads Secret data, and never port-forwards.
#
# Usage:
#   platform-preflight.sh [--context NAME] [--namespace NS] [--pool NAME]
#                         [--template NAME] [--mcp-url URL] [--help]
#
# Exit codes: 0 = no failures, 1 = at least one failure, 2 = usage error.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"

CTX=""
NS="${NAMESPACE:-dsh-demo}"
POOL=""
TEMPLATE=""
MCP_URL=""

PASS=0
WARN=0
FAIL=0

usage() {
  cat <<'EOF'
Read-only preflight for the DSH agents-as-a-service platform.

Options:
  --context NAME    kubectl context (default: current context)
  --namespace NS    namespace to inspect (default: $NAMESPACE or dsh-demo)
  --pool NAME       check only this warm pool
  --template NAME   check only this SandboxTemplate
  --mcp-url URL     also check an MCP server's /healthz and /readyz
  --help            show this help

The script makes no changes and never reads Secret data.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --context) CTX="${2:-}"; shift 2 ;;
    --namespace) NS="${2:-}"; shift 2 ;;
    --pool) POOL="${2:-}"; shift 2 ;;
    --template) TEMPLATE="${2:-}"; shift 2 ;;
    --mcp-url) MCP_URL="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$NS" ]; then
  echo "namespace must not be empty" >&2
  exit 2
fi

section() { printf '\n== %s ==\n' "$1"; }
pass() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
warn() { WARN=$((WARN + 1)); printf 'WARN  %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$1"; }
info() { printf 'INFO  %s\n' "$1"; }

command -v kubectl >/dev/null 2>&1 || {
  echo "FAIL  kubectl is not installed" >&2
  exit 1
}

if [ -n "$CTX" ]; then
  if ! kubectl config get-contexts -o name 2>/dev/null | grep -Fxq "$CTX"; then
    echo "FAIL  context '$CTX' is not in the kubeconfig" >&2
    exit 1
  fi
  KUBECTL=(kubectl --context "$CTX" --request-timeout=15s)
else
  KUBECTL=(kubectl --request-timeout=15s)
fi

k() { "${KUBECTL[@]}" "$@"; }

section "Target"
if current="$(k config current-context 2>/dev/null)"; then
  info "context: $current"
  pass "kubeconfig is readable"
else
  fail "cannot read the current kubectl context"
fi
if user="$(k auth whoami -o jsonpath='{.status.userInfo.username}' 2>/dev/null)" && [ -n "$user" ]; then
  info "identity: $user"
else
  fallback_user="$(k config view --minify -o jsonpath='{.users[0].name}' 2>/dev/null || true)"
  info "identity: ${fallback_user:-unknown}"
fi
if k get namespace "$NS" -o name >/dev/null 2>&1; then
  pass "namespace $NS exists"
else
  fail "namespace $NS is missing or not readable"
fi

if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/versions.env" ]; then
  # shellcheck source=/dev/null
  . "$REPO_ROOT/versions.env"
  info "expected agent-sandbox pin: ${AGENT_SANDBOX_VERSION:-unknown}"
else
  warn "versions.env not found; skipping the pin comparison"
fi

section "Capabilities (informational; never widen them)"
for check in "create sandboxclaims" "delete sandboxclaims" "create pods/exec"; do
  verb="${check%% *}"
  resource="${check#* }"
  if [ "$(k auth can-i "$verb" "$resource" -n "$NS" 2>/dev/null || echo unknown)" = "yes" ]; then
    info "can $verb $resource in $NS: yes"
  else
    info "can $verb $resource in $NS: no"
  fi
done
if [ "$(k auth can-i get secrets -n "$NS" 2>/dev/null || echo unknown)" = "yes" ]; then
  warn "RBAC allows reading Secrets; policy still forbids printing Secret data"
else
  info "can get secrets in $NS: no"
fi

section "Agent Sandbox CRDs"
for crd in \
  sandboxtemplates.extensions.agents.x-k8s.io \
  sandboxwarmpools.extensions.agents.x-k8s.io \
  sandboxclaims.extensions.agents.x-k8s.io \
  sandboxes.agents.x-k8s.io; do
  if k get crd "$crd" -o name >/dev/null 2>&1; then
    pass "CRD $crd"
  else
    fail "CRD $crd is missing or not readable"
  fi
done

section "Controller"
controller_ns="agent-sandbox-system"
controller_image="$(k -n "$controller_ns" get deployment agent-sandbox-controller \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
if [ -n "$controller_image" ]; then
  info "controller image: $controller_image"
  controller_tag="${controller_image##*:}"
  if [ -n "${AGENT_SANDBOX_VERSION:-}" ] && [ "$controller_tag" != "$AGENT_SANDBOX_VERSION" ]; then
    warn "controller tag '$controller_tag' differs from AGENT_SANDBOX_VERSION '$AGENT_SANDBOX_VERSION'"
  else
    pass "controller version matches versions.env"
  fi
else
  warn "controller deployment not found in $controller_ns; cannot verify the installed version"
fi

section "Templates"
templates="$(k -n "$NS" get sandboxtemplates \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)"
if [ -z "$templates" ]; then
  warn "no SandboxTemplates in $NS"
fi
if [ -n "$TEMPLATE" ] && ! printf '%s\n' "$templates" | grep -Fxq "$TEMPLATE"; then
  fail "SandboxTemplate $TEMPLATE not found in $NS"
fi
for tpl in $templates; do
  [ -n "$TEMPLATE" ] && [ "$tpl" != "$TEMPLATE" ] && continue
  desc="$(k -n "$NS" get sandboxtemplate "$tpl" \
    -o jsonpath='{range .spec.podTemplate.spec.containers[*]}{.name}{":"}{range .ports[*]}{.containerPort}{","}{end}{";"}{end}' \
    2>/dev/null || true)"
  info "template $tpl containers: ${desc:-<none>}"
  case "$desc" in
    *sandboxd*|*:8080,*|*:9090,*)
      pass "template $tpl exposes a sandboxd runtime (MCP sandboxd-in-cluster)" ;;
    *:8888,*)
      pass "template $tpl exposes a runtime HTTP port (MCP gateway/direct/in-cluster)" ;;
    *:3080,*)
      warn "template $tpl is DSH Web only (:3080); MCP execute_command cannot run against it" ;;
    *)
      warn "template $tpl exposes no known sandbox runtime port" ;;
  esac
done

section "Warm pools"
pools="$(k -n "$NS" get sandboxwarmpools \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)"
if [ -z "$pools" ]; then
  warn "no SandboxWarmPools in $NS; claims will provision cold"
fi
if [ -n "$POOL" ] && ! printf '%s\n' "$pools" | grep -Fxq "$POOL"; then
  fail "SandboxWarmPool $POOL not found in $NS"
fi
for pool in $pools; do
  [ -n "$POOL" ] && [ "$pool" != "$POOL" ] && continue
  replicas="$(k -n "$NS" get sandboxwarmpool "$pool" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  selector="$(k -n "$NS" get sandboxwarmpool "$pool" -o jsonpath='{.status.selector}' 2>/dev/null || true)"
  if [ -n "$selector" ]; then
    ready="$(k -n "$NS" get sandboxes -l "$selector" \
      -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
      2>/dev/null | grep -c '^True$' || true)"
  else
    ready="$(k -n "$NS" get sandboxes -l agents.x-k8s.io/warm-pool-sandbox \
      -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
      2>/dev/null | grep -c '^True$' || true)"
  fi
  replicas="${replicas:-0}"
  info "pool $pool: $ready/$replicas warm spares Ready"
  if [ "$replicas" -eq 0 ]; then
    warn "pool $pool has spec.replicas=0; signup will cold-provision"
  elif [ "$ready" -lt "$replicas" ]; then
    warn "pool $pool is below its desired spare count"
  else
    pass "pool $pool is at capacity"
  fi
done

section "Claims"
claims="$(k -n "$NS" get sandboxclaims \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\t"}{.status.conditions[?(@.type=="Ready")].reason}{"\t"}{.status.sandbox.name}{"\n"}{end}' \
  2>/dev/null || true)"
if [ -z "$claims" ]; then
  warn "no SandboxClaims in $NS"
else
  while IFS=$'\t' read -r name ready reason sandbox; do
    [ -n "$name" ] || continue
    if [ "$ready" != "True" ]; then
      warn "claim $name is not Ready (status='${ready:-unknown}', reason='${reason:-unknown}')"
    elif [ -z "$sandbox" ]; then
      warn "claim $name is Ready but has no bound sandbox"
    else
      pass "claim $name -> sandbox $sandbox"
    fi
  done <<< "$claims"
fi

section "Sandboxes"
sandboxes="$(k -n "$NS" get sandboxes \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.operatingMode}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
  2>/dev/null || true)"
if [ -z "$sandboxes" ]; then
  warn "no Sandboxes in $NS"
else
  while IFS=$'\t' read -r name mode ready; do
    [ -n "$name" ] || continue
    info "sandbox $name mode=${mode:-Running} ready=${ready:-unknown}"
  done <<< "$sandboxes"
fi

section "Storage and Services"
pvc_count="$(k -n "$NS" get pvc -o name 2>/dev/null | wc -l | tr -d ' ')"
svc_count="$(k -n "$NS" get service -o name 2>/dev/null | wc -l | tr -d ' ')"
info "persistent volume claims: ${pvc_count:-0}"
info "services: ${svc_count:-0}"
[ "${pvc_count:-0}" -gt 0 ] || warn "no PVCs in $NS"
[ "${svc_count:-0}" -gt 0 ] || warn "no Services in $NS"

section "Gateway"
if k -n "$NS" get deployment dsh-gateway -o name >/dev/null 2>&1; then
  pass "gateway deployment dsh-gateway exists"
  if k -n "$NS" get service dsh-gateway -o name >/dev/null 2>&1; then
    endpoints="$(k -n "$NS" get endpoints dsh-gateway \
      -o jsonpath='{range .subsets[*].addresses[*]}{.ip}{" "}{end}' 2>/dev/null || true)"
    if [ -n "$endpoints" ]; then
      pass "gateway endpoints: $endpoints"
    else
      warn "gateway Service has no ready endpoints"
    fi
  else
    warn "gateway Service dsh-gateway is missing"
  fi
else
  warn "no dsh-gateway deployment; this is a resource-layer-only install"
fi

section "MCP server"
if [ -n "$MCP_URL" ]; then
  if ! command -v curl >/dev/null 2>&1; then
    fail "curl is required for --mcp-url"
  else
    base="${MCP_URL%/}"
    base="${base%/mcp}"
    for endpoint in healthz readyz; do
      body="$(curl -fsS --max-time 5 "$base/$endpoint" 2>/dev/null | head -c 300 || true)"
      if [ -n "$body" ]; then
        pass "MCP /$endpoint: $body"
      else
        fail "MCP /$endpoint is unreachable at $base/$endpoint"
      fi
    done
  fi
else
  info "no --mcp-url supplied; skipping server health checks"
fi

printf '\n== Summary ==\nPASS=%d WARN=%d FAIL=%d\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
