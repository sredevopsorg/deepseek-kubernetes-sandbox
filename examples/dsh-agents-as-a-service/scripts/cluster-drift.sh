#!/usr/bin/env bash
# Read-only drift check: compare the live platform with the repo's expectations.
#
# The repo manifests are portable and cluster-specific values belong in
# manifests/local/values.env (applied with scripts/local-overlay.sh). This
# script flags the two failure modes that matter:
#
#   DRIFT - live state contradicts a repo requirement (security setting, image
#           pin, readiness probe, SSO boundary, untracked template/pool binding).
#           Exits non-zero.
#   WARN  - production hardening or local tuning is missing/different but the
#           platform still works (replicas, storage class/size, optional
#           policies, released PVs).
#   INFO  - a local override that is expected (gVisor, nodeSelector, affinity)
#           and should be recorded in manifests/local/values.env.
#
# It never changes the cluster. Run it from the repository checkout:
#   examples/dsh-agents-as-a-service/scripts/cluster-drift.sh
#
# Environment overrides: NAMESPACE (default dsh-demo), CONTEXT (kubectl context).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(cd ../.. && pwd)

set -a
# shellcheck source=/dev/null
. "$ROOT/versions.env"
set +a

NS="${NAMESPACE:-dsh-demo}"
EXPECTED_TAG="${DSH_VERSION}${RELEASE_REVISION:+-$RELEASE_REVISION}"
TEMPLATE=dsh-web

KUBECTL=(kubectl)
if [ -n "${CONTEXT:-}" ]; then
  KUBECTL+=(--context "$CONTEXT")
fi

DRIFTS=0
WARNS=0
pass() { printf 'PASS   %s\n' "$*"; }
info() { printf 'INFO   %s\n' "$*"; }
warn() { printf 'WARN   %s\n' "$*"; WARNS=$((WARNS + 1)); }
drift() { printf 'DRIFT  %s\n' "$*"; DRIFTS=$((DRIFTS + 1)); }

if ! "${KUBECTL[@]}" version >/dev/null 2>&1; then
  echo "FAIL   kubectl cannot reach a cluster; select a context first" >&2
  exit 1
fi
if ! "${KUBECTL[@]}" -n "$NS" get namespace "$NS" >/dev/null 2>&1; then
  echo "FAIL   namespace $NS not found" >&2
  exit 1
fi

echo "== Context"
printf 'context: %s\n' "$("${KUBECTL[@]}" config current-context)"
printf 'user:    %s\n' "$("${KUBECTL[@]}" auth whoami -o jsonpath='{.status.userInfo.username}' 2>/dev/null || echo unknown)"
printf 'namespace: %s\n' "$NS"
printf 'expected release: %s\n' "$EXPECTED_TAG"

echo
echo "== Control plane"
CONTROLLER_IMAGE=$("${KUBECTL[@]}" -n agent-sandbox-system get deployment agent-sandbox-controller \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
case "$CONTROLLER_IMAGE" in
  *":$AGENT_SANDBOX_VERSION") pass "agent-sandbox-controller image matches pin ($AGENT_SANDBOX_VERSION)" ;;
  "") warn "agent-sandbox-controller Deployment not found; is agent-sandbox installed?" ;;
  *) drift "agent-sandbox-controller is $CONTROLLER_IMAGE, repo pins $AGENT_SANDBOX_VERSION" ;;
esac

for crd in sandboxtemplates.extensions.agents.x-k8s.io sandboxwarmpools.extensions.agents.x-k8s.io \
  sandboxclaims.extensions.agents.x-k8s.io sandboxes.agents.x-k8s.io; do
  if "${KUBECTL[@]}" get crd "$crd" >/dev/null 2>&1; then
    pass "CRD $crd present"
  else
    drift "CRD $crd is missing"
  fi
done

echo
echo "== SandboxTemplates"
TEMPLATE_LIST=$("${KUBECTL[@]}" -n "$NS" get sandboxtemplates -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)
while IFS= read -r name; do
  [ -n "$name" ] || continue
  if [ "$name" = "$TEMPLATE" ]; then
    pass "SandboxTemplate $TEMPLATE exists (repo blueprint)"
  else
    drift "SandboxTemplate $name is not in the repo (untracked hand-made template; fold it into $TEMPLATE via manifests/local/)"
  fi
done <<<"$TEMPLATE_LIST"

if printf '%s\n' "$TEMPLATE_LIST" | grep -qxF "$TEMPLATE"; then
  tpl() { "${KUBECTL[@]}" -n "$NS" get sandboxtemplate "$TEMPLATE" -o jsonpath="$1" 2>/dev/null || true; }

  AUTOMOUNT=$(tpl '{.spec.podTemplate.spec.automountServiceAccountToken}')
  if [ "$AUTOMOUNT" = "false" ]; then
    pass "$TEMPLATE keeps the ServiceAccount token out of sandbox pods"
  else
    drift "$TEMPLATE does not set spec.podTemplate.spec.automountServiceAccountToken: false (tenant pods would get the gateway/SA token)"
  fi

  TEMPLATE_IMAGE=$(tpl '{.spec.podTemplate.spec.containers[0].image}')
  if [ "$TEMPLATE_IMAGE" = "$GHCR_IMAGE:$EXPECTED_TAG" ]; then
    pass "$TEMPLATE image is the release identity ($EXPECTED_TAG)"
  else
    drift "$TEMPLATE image is '$TEMPLATE_IMAGE', expected '$GHCR_IMAGE:$EXPECTED_TAG'"
  fi

  for field in runtimeClassName 'nodeSelector' 'affinity'; do
    value=$(tpl "{.spec.podTemplate.spec.$field}")
    if [ -n "$value" ]; then
      info "$TEMPLATE has a local scheduling/runtime override: $field=$value (record it in manifests/local/values.env)"
    fi
  done

  STORAGE_CLASS=$(tpl '{.spec.volumeClaimTemplates[0].spec.storageClassName}')
  STORAGE_SIZE=$(tpl '{.spec.volumeClaimTemplates[0].spec.resources.requests.storage}')
  if [ -z "$STORAGE_CLASS" ]; then
    pass "$TEMPLATE uses the cluster default StorageClass"
  else
    RECLAIM=$("${KUBECTL[@]}" get storageclass "$STORAGE_CLASS" -o jsonpath='{.reclaimPolicy}' 2>/dev/null || true)
    case "$RECLAIM" in
      Delete)
        pass "$TEMPLATE pins storageClassName=$STORAGE_CLASS (Delete reclaim)"
        ;;
      Retain)
        warn "$TEMPLATE pins Retain StorageClass $STORAGE_CLASS; deleting a claim leaks its PV and Longhorn volume"
        ;;
      *)
        warn "$TEMPLATE pins storageClassName=$STORAGE_CLASS; could not read its reclaimPolicy"
        ;;
    esac
  fi
  if [ "$STORAGE_SIZE" = "5Gi" ]; then
    pass "$TEMPLATE requests 5Gi per workspace (repo value)"
  else
    warn "$TEMPLATE requests $STORAGE_SIZE per workspace, repo manifest says 5Gi"
  fi

  FAILED_POD_POLICY=$(tpl '{.spec.failedPodPolicy}')
  if [ -n "$FAILED_POD_POLICY" ]; then
    info "$TEMPLATE sets failedPodPolicy=$FAILED_POD_POLICY (local choice; repo leaves it commented out)"
  fi

  # shellcheck disable=SC2016 # $k/$v are kubectl jsonpath variables
  NODE_SELECTORS=$(tpl '{range $k, $v := .spec.podTemplate.spec.nodeSelector}{$k}={$v}{"\n"}{end}')
  while IFS= read -r selector; do
    [ -n "$selector" ] || continue
    matched=$("${KUBECTL[@]}" get nodes -l "$selector" -o name 2>/dev/null | grep -c . || true)
    if [ "$matched" -gt 0 ]; then
      pass "nodeSelector $selector matches $matched node(s)"
    else
      drift "$TEMPLATE nodeSelector '$selector' matches no node; new sandboxes will stay Pending"
    fi
  done <<<"$NODE_SELECTORS"

  if "${KUBECTL[@]}" -n "$NS" get httproute dsh-gateway >/dev/null 2>&1; then
    ROUTE_HOST=$("${KUBECTL[@]}" -n "$NS" get httproute dsh-gateway -o jsonpath='{.spec.hostnames[0]}' 2>/dev/null || true)
    TRUSTED=$(tpl '{.spec.podTemplate.spec.containers[0].env[?(@.name=="DSH_TRUSTED_HOSTS")].value}')
    if [ -n "$ROUTE_HOST" ] && [ "$TRUSTED" != "$ROUTE_HOST" ]; then
      drift "$TEMPLATE DSH_TRUSTED_HOSTS='$TRUSTED' does not match the exposed host '$ROUTE_HOST' (DSH will reject /api)"
    elif [ -n "$ROUTE_HOST" ]; then
      pass "DSH_TRUSTED_HOSTS matches the HTTPRoute host ($ROUTE_HOST)"
    fi
  fi
fi

echo
echo "== Warm pool"
if "${KUBECTL[@]}" -n "$NS" get sandboxwarmpool dsh-pool >/dev/null 2>&1; then
  POOL_TEMPLATE=$("${KUBECTL[@]}" -n "$NS" get sandboxwarmpool dsh-pool -o jsonpath='{.spec.sandboxTemplateRef.name}')
  if [ "$POOL_TEMPLATE" = "$TEMPLATE" ]; then
    pass "warm pool dsh-pool references $TEMPLATE"
  else
    drift "warm pool dsh-pool references '$POOL_TEMPLATE', not '$TEMPLATE' (re-applying manifests/20-sandboxwarmpool.yaml would switch pools)"
  fi
  POOL_REPLICAS=$("${KUBECTL[@]}" -n "$NS" get sandboxwarmpool dsh-pool -o jsonpath='{.spec.replicas}')
  POOL_READY=$("${KUBECTL[@]}" -n "$NS" get sandboxwarmpool dsh-pool -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
  if [ "$POOL_REPLICAS" = "2" ]; then
    pass "warm pool has the repo's 2 replicas"
  else
    warn "warm pool replicas=$POOL_REPLICAS, repo default is 2 (local tuning; signup is still warm with >=1 spare)"
  fi
  if [ -n "$POOL_READY" ] && [ "$POOL_READY" -lt "$POOL_REPLICAS" ]; then
    warn "warm pool has $POOL_READY/$POOL_REPLICAS Ready spares (signup will be slow until it replenishes)"
  fi
else
  drift "SandboxWarmPool dsh-pool is missing"
fi

echo
echo "== PVCs"
RELEASED=$("${KUBECTL[@]}" get pv -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,NS:.spec.claimRef.namespace' --no-headers 2>/dev/null \
  | awk -v ns="$NS" '$2 == "Released" && $3 == ns { print $1 }' || true)
if [ -n "$RELEASED" ]; then
  warn "Released PVs from deleted claims in $NS (a Retain StorageClass leaks them):"
  while IFS= read -r pv; do
    [ -n "$pv" ] || continue
    printf '         %s\n' "$pv"
  done <<<"$RELEASED"
else
  pass "no Released PVs left behind in $NS"
fi

echo
echo "== Gateway"
if "${KUBECTL[@]}" -n "$NS" get deployment dsh-gateway >/dev/null 2>&1; then
  gw() { "${KUBECTL[@]}" -n "$NS" get deployment dsh-gateway -o jsonpath="$1" 2>/dev/null || true; }

  LIVE_GATEWAY_IMAGE=$(gw '{.spec.template.spec.containers[0].image}')
  EXPECTED_GATEWAY_IMAGE="$GATEWAY_IMAGE:$EXPECTED_TAG"
  if [ "$LIVE_GATEWAY_IMAGE" = "$EXPECTED_GATEWAY_IMAGE" ]; then
    pass "gateway image is the release identity ($EXPECTED_TAG)"
  else
    drift "gateway image is '$LIVE_GATEWAY_IMAGE', expected '$EXPECTED_GATEWAY_IMAGE'"
  fi

  REPLICAS=$(gw '{.spec.replicas}')
  if [ "$REPLICAS" = "1" ]; then
    pass "gateway replicas=1 (in-memory state contract)"
  else
    drift "gateway replicas=$REPLICAS; the in-memory session/idle state requires 1"
  fi

  READINESS_PATH=$(gw '{.spec.template.spec.containers[0].readinessProbe.httpGet.path}')
  if [ "$READINESS_PATH" = "/readyz" ]; then
    pass "readiness probe is API-aware (/readyz)"
  else
    drift "readiness probe is '$READINESS_PATH'; expected /readyz (an API outage otherwise leaves the pod Ready while users get 500s)"
  fi

  GATEWAY_ENV=$(gw '{range .spec.template.spec.containers[*].env[*]}{.name}={.value}{"\n"}{end}')
  has_env() { printf '%s\n' "$GATEWAY_ENV" | awk -F= -v k="$1" '$1 == k { found = 1 } END { exit found ? 0 : 1 }'; }
  env_value() { printf '%s\n' "$GATEWAY_ENV" | awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); print }'; }

  for required in NAMESPACE POOL DSH_CONTAINER COOKIE_SECURE; do
    if has_env "$required"; then
      pass "gateway env $required is set"
    else
      drift "gateway env $required is missing"
    fi
  done

  if has_env AUTH_PROXY_HEADER; then
    info "SSO is enabled (AUTH_PROXY_HEADER=$(env_value AUTH_PROXY_HEADER))"
    if [ "$(env_value COOKIE_SECURE | tr '[:upper:]' '[:lower:]')" = "true" ]; then
      pass "SSO uses a Secure session cookie"
    else
      drift "SSO is enabled with COOKIE_SECURE=$(env_value COOKIE_SECURE); the session cookie must be Secure"
    fi
    if has_env ALLOW_INSECURE_COOKIES; then
      warn "ALLOW_INSECURE_COOKIES is set; remove it behind TLS (ignored while COOKIE_SECURE=true)"
    fi
    HOPS=$(env_value TRUSTED_PROXY_HOPS)
    if [ -z "$HOPS" ] || [ "$HOPS" = "0" ]; then
      warn "TRUSTED_PROXY_HOPS is unset/0 behind the SSO proxy chain; all users share the auth rate-limit bucket"
    else
      pass "TRUSTED_PROXY_HOPS=$HOPS"
    fi
    SERVICE_TYPE=$("${KUBECTL[@]}" -n "$NS" get service dsh-gateway -o jsonpath='{.spec.type}' 2>/dev/null || true)
    if [ "$SERVICE_TYPE" = "ClusterIP" ]; then
      pass "gateway Service stays ClusterIP (only oauth2-proxy can reach it)"
    else
      drift "gateway Service type is '$SERVICE_TYPE'; SSO requires ClusterIP and a sole proxy path"
    fi
    for policy in dsh-gateway-ingress oauth2-proxy-ingress oauth2-proxy-egress; do
      if "${KUBECTL[@]}" -n "$NS" get networkpolicy "$policy" >/dev/null 2>&1; then
        pass "SSO NetworkPolicy $policy present"
      else
        drift "SSO NetworkPolicy $policy is missing"
      fi
    done
    NS_SELECTOR=$("${KUBECTL[@]}" -n "$NS" get networkpolicy oauth2-proxy-ingress \
      -o jsonpath='{.spec.ingress[0].from[0].namespaceSelector}' 2>/dev/null || true)
    case "$NS_SELECTOR" in
      '' | '{}' | 'map[]')
        drift "oauth2-proxy-ingress accepts every namespace; scope namespaceSelector to the Gateway implementation's namespace"
        ;;
      *)
        pass "oauth2-proxy-ingress is scoped to a namespace ($NS_SELECTOR)"
        ;;
    esac
  fi
else
  drift "Deployment dsh-gateway is missing"
fi

if "${KUBECTL[@]}" -n "$NS" get deployment oauth2-proxy >/dev/null 2>&1; then
  OAUTH_AUTOMOUNT=$("${KUBECTL[@]}" -n "$NS" get deployment oauth2-proxy \
    -o jsonpath='{.spec.template.spec.automountServiceAccountToken}' 2>/dev/null || true)
  if [ "$OAUTH_AUTOMOUNT" = "false" ]; then
    pass "oauth2-proxy does not mount a ServiceAccount token"
  else
    warn "oauth2-proxy mounts a ServiceAccount token it never uses (the repo manifest sets automountServiceAccountToken: false)"
  fi
fi

echo
echo "== NetworkPolicies"
if "${KUBECTL[@]}" -n "$NS" get networkpolicy dsh-sandbox >/dev/null 2>&1; then
  pass "sandbox NetworkPolicy is applied"
else
  warn "sandbox NetworkPolicy is not applied; tenants can reach each other and cluster-internal endpoints (required for production)"
fi

if "${KUBECTL[@]}" -n "$NS" get networkpolicy dsh-gateway-egress >/dev/null 2>&1; then
  pass "gateway egress NetworkPolicy is applied"
  API_HOST=$("${KUBECTL[@]}" -n default get endpoints kubernetes -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null || true)
  API_PORT=$("${KUBECTL[@]}" -n default get endpoints kubernetes -o jsonpath='{.subsets[0].ports[0].port}' 2>/dev/null || true)
  if [ -n "$API_HOST" ]; then
    if "${KUBECTL[@]}" -n "$NS" get networkpolicy dsh-gateway-egress -o json 2>/dev/null \
      | API_HOST="$API_HOST" API_PORT="$API_PORT" python3 -c '
import ipaddress, json, os, sys

host = ipaddress.ip_address(os.environ["API_HOST"])
port = int(os.environ["API_PORT"])
policy = json.load(sys.stdin)
allowed = False
for rule in (policy.get("spec") or {}).get("egress") or []:
    ports = rule.get("ports") or []
    port_ok = not ports or any(str(p.get("port")) == str(port) for p in ports)
    for peer in rule.get("to") or []:
        block = (peer.get("ipBlock") or {}).get("cidr")
        if not block or not port_ok:
            continue
        try:
            if host in ipaddress.ip_network(block, strict=False):
                allowed = True
        except ValueError:
            continue
sys.exit(0 if allowed else 1)
'; then
      pass "gateway egress allows the Kubernetes API endpoint $API_HOST:$API_PORT"
    else
      drift "gateway egress does not allow the Kubernetes API endpoint $API_HOST:$API_PORT (kube-router evaluates post-DNAT; the ClusterIP rule alone is not enough)"
    fi
  else
    warn "could not read the kubernetes Service endpoint to check the gateway egress policy"
  fi
else
  warn "gateway egress NetworkPolicy is not applied (required for production)"
fi

echo
if [ "$DRIFTS" -gt 0 ]; then
  printf 'cluster-drift: %d drift item(s), %d warning(s) - review the DRIFT lines above.\n' "$DRIFTS" "$WARNS" >&2
  exit 1
fi
printf 'cluster-drift: ok (%d warning(s) only).\n' "$WARNS"
