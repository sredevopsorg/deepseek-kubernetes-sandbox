#!/usr/bin/env bash
# Preflight for the gateway-api-sso exposure variant.
#
# Run this AFTER creating the oauth2-proxy/TLS Secrets, setting the gateway
# env and applying manifests/exposure/gateway-api-sso/, and BEFORE sending real
# traffic. It checks the configuration that is easy to get wrong and whose
# failure is either silent (login appears to work but the gateway still asks
# for its own token) or dangerous (a spoofable identity header, a placeholder
# host, a TLS Secret that does not exist).
#
# Checks:
#   1. kubectl reaches the cluster and the SSO objects exist;
#   2. the oauth2-proxy Secret has client-id, client-secret and a cookie-secret
#      that decodes to 16, 24 or 32 bytes; the TLS Secret exists;
#   3. deploy/dsh-gateway has AUTH_PROXY_HEADER=X-Forwarded-User and
#      COOKIE_SECURE=true (both are opt-in and both are required for SSO over
#      TLS);
#   4. the Gateway's TLS certificate reference resolves to an existing Secret;
#   5. no your-github-org / dsh-demo.example.com placeholder is left in the
#      applied oauth2-proxy args, Gateway listeners or HTTPRoute hostnames;
#   6. no Ingress in the namespace routes to dsh-gateway (matched on the
#      backend Service, not the object name). With SSO enabled the Service must
#      be reachable only through oauth2-proxy;
#   7. the gateway ServiceAccount may `patch` sandboxclaims. It is required
#      for the gateway's own control paths (and by the shipped Role, which
#      still grants it); without the verb those calls answer 503;
#   8. all three SSO NetworkPolicies shipped by the variant are applied, and
#      oauth2-proxy-ingress does not accept every namespace. Without them any
#      Pod in the cluster can reach dsh-gateway:8080 (directly, or through
#      oauth2-proxy) and spoof the identity header, so the exposure is
#      demo-only.
#
# Exit status: 0 when every check passes (warnings allowed), 1 on any failure.
# Every failure prints the fix. This script only reads cluster state.
#
# Usage:
#   examples/dsh-agents-as-a-service/scripts/preflight-sso.sh
#
# Environment overrides:
#   NAMESPACE        namespace (default: dsh-demo)
#   GATEWAY_DEPLOY   gateway Deployment name (default: dsh-gateway)
#   OAUTH_DEPLOY     oauth2-proxy Deployment name (default: oauth2-proxy)
#   OAUTH_SECRET     oauth2-proxy Secret name (default: oauth2-proxy)
#   TLS_SECRET       TLS Secret name the Gateway should reference (default:
#                    dsh-demo-tls)
#   GATEWAY_ROLE     Role/RoleBinding name that grants the gateway access
#                    (default: dsh-gateway; only used when impersonation is
#                    unavailable and the permission must be read statically)
set -euo pipefail
cd "$(dirname "$0")/.."

NS="${NAMESPACE:-dsh-demo}"
GATEWAY_DEPLOY="${GATEWAY_DEPLOY:-dsh-gateway}"
OAUTH_DEPLOY="${OAUTH_DEPLOY:-oauth2-proxy}"
OAUTH_SECRET="${OAUTH_SECRET:-oauth2-proxy}"
TLS_SECRET="${TLS_SECRET:-dsh-demo-tls}"
GATEWAY_ROLE="${GATEWAY_ROLE:-dsh-gateway}"

FAILED=0
pass() { printf 'PASS  %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*" >&2; }
fail() { printf 'FAIL  %s\n' "$*" >&2; FAILED=1; }

if ! command -v kubectl >/dev/null 2>&1; then
  echo "FAIL  kubectl is not installed; install it and select the target context" >&2
  exit 1
fi

if ! kubectl -n "$NS" get deployment "$GATEWAY_DEPLOY" >/dev/null 2>&1; then
  echo "FAIL  deploy/$GATEWAY_DEPLOY not found in namespace $NS." >&2
  echo "      Apply the base manifests first: kubectl apply -f manifests/" >&2
  echo "      Then apply the variant: kubectl apply -f manifests/exposure/gateway-api-sso/" >&2
  exit 1
fi

echo "== Secrets"
# check_secret_key <secret> <key> <hint>
check_secret_key() {
  local secret=$1 key=$2 hint=$3 value escaped
  # Kubernetes JSONPath treats "." as a path separator; escape it in keys such
  # as tls.crt.
  escaped=${key//./\\.}
  value=$(kubectl -n "$NS" get secret "$secret" -o jsonpath="{.data.${escaped}}" 2>/dev/null || true)
  if [ -n "$value" ]; then
    pass "Secret $secret has non-empty key '$key'"
  else
    fail "Secret $secret is missing a non-empty '$key' key. $hint"
  fi
}

check_secret_key "$OAUTH_SECRET" client-id \
  "Create it from manifests/exposure/gateway-api-sso/73-oauth2-proxy-secret.yaml.example."
check_secret_key "$OAUTH_SECRET" client-secret \
  "Create it from manifests/exposure/gateway-api-sso/73-oauth2-proxy-secret.yaml.example."
check_secret_key "$OAUTH_SECRET" cookie-secret \
  "Create it from manifests/exposure/gateway-api-sso/73-oauth2-proxy-secret.yaml.example."

# oauth2-proxy accepts the cookie secret either as raw bytes of length 16, 24
# or 32, or as URL-safe unpadded base64 that decodes to one of those lengths
# (pkg/encryption.SecretBytes). The documented generator,
# `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`, produces the
# second form: 43 base64 characters that decode to 32 bytes.
if COOKIE_B64=$(kubectl -n "$NS" get secret "$OAUTH_SECRET" \
  -o jsonpath='{.data.cookie-secret}' 2>/dev/null); then
  if [ -n "$COOKIE_B64" ]; then
    COOKIE_RAW=$(printf '%s' "$COOKIE_B64" | base64 -d 2>/dev/null || true)
    COOKIE_LEN=${#COOKIE_RAW}
    COOKIE_FORM=raw
    case "$COOKIE_LEN" in
      16 | 24 | 32) ;;
      *)
        # Mirror SecretBytes: only a strictly URL-safe base64 string is decoded;
        # anything else is used as raw bytes (and is then the wrong length).
        COOKIE_FORM=base64
        case "$COOKIE_RAW" in
          "" | *[!A-Za-z0-9_=-]*) COOKIE_LEN=0 ;;
          *)
            COOKIE_LEN=$(printf '%s' "$COOKIE_RAW" | tr -- '-_' '+/' | tr -d '=\n' \
              | base64 -d 2>/dev/null | wc -c || true)
            ;;
        esac
        ;;
    esac
    case "$COOKIE_LEN" in
      16 | 24 | 32) pass "oauth2-proxy cookie-secret is $COOKIE_FORM with a $COOKIE_LEN-byte key" ;;
      *)
        fail "oauth2-proxy cookie-secret is not a valid length ($COOKIE_LEN bytes). It must be 16, 24 or 32 raw bytes, or URL-safe unpadded base64 that decodes to one of those lengths. Generate it with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
        ;;
    esac
  fi
fi

check_secret_key "$TLS_SECRET" tls.crt \
  "Create the TLS Secret with: kubectl -n $NS create secret tls $TLS_SECRET --cert=tls.crt --key=tls.key"
check_secret_key "$TLS_SECRET" tls.key \
  "Create the TLS Secret with: kubectl -n $NS create secret tls $TLS_SECRET --cert=tls.crt --key=tls.key"

echo
echo "== Gateway SSO configuration (deploy/$GATEWAY_DEPLOY)"
GATEWAY_ENV=$(kubectl -n "$NS" get deployment "$GATEWAY_DEPLOY" \
  -o jsonpath='{range .spec.template.spec.containers[*].env[*]}{.name}={.value}{"\n"}{end}')

# env_value <NAME>: print the literal env value, or nothing when unset.
env_value() {
  printf '%s\n' "$GATEWAY_ENV" | awk -F= -v key="$1" '$1 == key { sub(/^[^=]*=/, ""); print }'
}
env_is_set() {
  printf '%s\n' "$GATEWAY_ENV" | awk -F= -v key="$1" '$1 == key { found = 1 } END { exit found ? 0 : 1 }'
}

if ! env_is_set AUTH_PROXY_HEADER; then
  fail "AUTH_PROXY_HEADER is not set on deploy/$GATEWAY_DEPLOY, so the gateway ignores oauth2-proxy's identity header and users are still asked for the gateway token. Fix: kubectl -n $NS set env deploy/$GATEWAY_DEPLOY AUTH_PROXY_HEADER=X-Forwarded-User"
elif [ "$(env_value AUTH_PROXY_HEADER)" != "X-Forwarded-User" ]; then
  fail "AUTH_PROXY_HEADER is '$(env_value AUTH_PROXY_HEADER)', expected 'X-Forwarded-User'. Fix: kubectl -n $NS set env deploy/$GATEWAY_DEPLOY AUTH_PROXY_HEADER=X-Forwarded-User"
else
  pass "AUTH_PROXY_HEADER=X-Forwarded-User"
fi

if ! env_is_set COOKIE_SECURE; then
  fail "COOKIE_SECURE is not set on deploy/$GATEWAY_DEPLOY; the session cookie must be Secure behind TLS. Fix: kubectl -n $NS set env deploy/$GATEWAY_DEPLOY COOKIE_SECURE=true"
elif [ "$(env_value COOKIE_SECURE | tr '[:upper:]' '[:lower:]')" != "true" ]; then
  fail "COOKIE_SECURE is '$(env_value COOKIE_SECURE)', expected 'true' behind TLS. Fix: kubectl -n $NS set env deploy/$GATEWAY_DEPLOY COOKIE_SECURE=true"
else
  pass "COOKIE_SECURE=true"
fi

if env_is_set ALLOW_INSECURE_COOKIES; then
  warn "ALLOW_INSECURE_COOKIES is still set on deploy/$GATEWAY_DEPLOY. It is ignored while COOKIE_SECURE=true but is a footgun: remove it with 'kubectl -n $NS set env deploy/$GATEWAY_DEPLOY ALLOW_INSECURE_COOKIES-'."
fi

echo
echo "== Gateway RBAC (sandboxclaims write access)"
# The gateway's control paths (account deletion, token rotation on a new
# signup, operatingMode changes) need write access to sandboxclaims. Without
# `patch` those calls answer 503. The SSO landing path itself never rotates the
# token-hash annotation - it is the claim's server-side identity marker.
GATEWAY_SA=$(kubectl -n "$NS" get deployment "$GATEWAY_DEPLOY" \
  -o jsonpath='{.spec.template.spec.serviceAccountName}' 2>/dev/null || true)
GATEWAY_SA=${GATEWAY_SA:-default}
CAN_I=$(kubectl auth can-i patch sandboxclaims.extensions.agents.x-k8s.io \
  -n "$NS" --as="system:serviceaccount:$NS:$GATEWAY_SA" 2>&1 || true)

# read_gateway_rules: one "apiGroups|resources|verbs" line per rule of the
# Role (or ClusterRole) the gateway RoleBinding points at. Used only when
# `kubectl auth can-i --as` is unavailable (a read-only kubeconfig cannot
# impersonate); it is a static check, not an authoritative one.
read_gateway_rules() {
  local rules_jsonpath kind name
  rules_jsonpath='{range .rules[*]}{range .apiGroups[*]}{@}{","}{end}{"|"}{range .resources[*]}{@}{","}{end}{"|"}{range .verbs[*]}{@}{","}{end}{"\n"}{end}'
  kind=$(kubectl -n "$NS" get rolebinding "$GATEWAY_ROLE" \
    -o jsonpath='{.roleRef.kind}' 2>/dev/null || true)
  name=$(kubectl -n "$NS" get rolebinding "$GATEWAY_ROLE" \
    -o jsonpath='{.roleRef.name}' 2>/dev/null || true)
  case "$kind" in
    Role) kubectl -n "$NS" get role "$name" -o jsonpath="$rules_jsonpath" 2>/dev/null || true ;;
    ClusterRole) kubectl get clusterrole "$name" -o jsonpath="$rules_jsonpath" 2>/dev/null || true ;;
  esac
}

if [ "$CAN_I" = "yes" ]; then
  pass "ServiceAccount $GATEWAY_SA can patch sandboxclaims"
elif [ "$CAN_I" = "no" ]; then
  fail "ServiceAccount $GATEWAY_SA cannot patch sandboxclaims: SSO sign-in answers 503 whenever the browser has no valid gateway session (after a gateway restart, cookie expiry or in a new browser). Fix: grant 'patch' on sandboxclaims to Role $GATEWAY_ROLE (the shipped Role in manifests/50-gateway.yaml has it; re-applying just the Role/RoleBinding is enough). RBAC is evaluated per request; no gateway restart is needed."
else
  RULES=$(read_gateway_rules)
  if printf '%s\n' "$RULES" | awk -F'|' \
    '$1 ~ /(^|,)extensions\.agents\.x-k8s\.io(,|$)/ && $2 ~ /(^|,)sandboxclaims(,|$)/ && $3 ~ /(^|,)patch(,|$)/ { found = 1 } END { exit found ? 0 : 1 }'; then
    pass "the gateway Role grants patch on sandboxclaims (static check; impersonation unavailable)"
  elif [ -n "$RULES" ]; then
    fail "Role $GATEWAY_ROLE does not grant 'patch' on sandboxclaims: SSO sign-in answers 503 whenever the browser has no valid gateway session. Fix: grant that verb to the Role (the shipped Role in manifests/50-gateway.yaml has it; re-applying just the Role/RoleBinding is enough). RBAC is evaluated per request; no gateway restart is needed."
  else
    warn "could not verify that ServiceAccount $GATEWAY_SA may patch sandboxclaims ($CAN_I). Check manually that Role $GATEWAY_ROLE grants 'patch' on extensions.agents.x-k8s.io/sandboxclaims."
  fi
fi

echo
echo "== SSO objects and placeholders"
if kubectl -n "$NS" get deployment "$OAUTH_DEPLOY" >/dev/null 2>&1; then
  pass "deploy/$OAUTH_DEPLOY exists"
  OAUTH_ARGS=$(kubectl -n "$NS" get deployment "$OAUTH_DEPLOY" \
    -o jsonpath='{range .spec.template.spec.containers[*].args[*]}{@}{"\n"}{end}')
else
  OAUTH_ARGS=""
  fail "deploy/$OAUTH_DEPLOY not found. Apply the variant: kubectl apply -f manifests/exposure/gateway-api-sso/"
fi

# GitHub org restriction is the only authorization in the shipped variant.
# --email-domain=* is acceptable only because --github-org narrows membership;
# a missing or empty org value would accept any GitHub account.
if [ -n "$OAUTH_ARGS" ]; then
  if printf '%s\n' "$OAUTH_ARGS" | grep -q '^--provider=github$'; then
    pass "oauth2-proxy uses --provider=github"
  else
    fail "oauth2-proxy is not configured with --provider=github. Fix: add --provider=github to deploy/$OAUTH_DEPLOY."
  fi
  if printf '%s\n' "$OAUTH_ARGS" | grep -Eq '^--github-org=.+$'; then
    pass "oauth2-proxy restricts membership with --github-org"
  else
    fail "oauth2-proxy has no non-empty --github-org. Fix: set --github-org=<your-org> in deploy/$OAUTH_DEPLOY; otherwise --email-domain=* accepts any GitHub account."
  fi
fi

if kubectl -n "$NS" get httproute dsh-gateway >/dev/null 2>&1; then
  pass "HTTPRoute dsh-gateway exists"
  ROUTE_STATE=$(kubectl -n "$NS" get httproute dsh-gateway \
    -o jsonpath='{.spec.hostnames[*]}' 2>/dev/null || true)
else
  ROUTE_STATE=""
  fail "HTTPRoute dsh-gateway not found. Apply the variant: kubectl apply -f manifests/exposure/gateway-api-sso/"
fi

if kubectl -n "$NS" get gateway dsh-gateway >/dev/null 2>&1; then
  pass "Gateway dsh-gateway exists"
  GATEWAY_STATE=$(kubectl -n "$NS" get gateway dsh-gateway \
    -o jsonpath='{range .spec.listeners[*]}{.hostname}{"\n"}{end}' 2>/dev/null || true)
  CERT_REFS=$(kubectl -n "$NS" get gateway dsh-gateway \
    -o jsonpath='{range .spec.listeners[*].tls.certificateRefs[*]}{.namespace}{"/"}{.name}{"\n"}{end}' 2>/dev/null || true)
else
  GATEWAY_STATE=""
  CERT_REFS=""
  fail "Gateway dsh-gateway not found. Apply the variant: kubectl apply -f manifests/exposure/gateway-api-sso/"
fi

# Placeholders are the default values in the committed manifests. They reach
# the cluster if the variant is applied before editing them.
PLACEHOLDER_RE='your-github-org|dsh-demo\.example\.com'
PLACEHOLDER_STATE=$(printf '%s\n%s\n%s\n' "$OAUTH_ARGS" "$ROUTE_STATE" "$GATEWAY_STATE")
if printf '%s\n' "$PLACEHOLDER_STATE" | grep -En "$PLACEHOLDER_RE" >/dev/null 2>&1; then
  fail "the applied SSO objects still contain a placeholder (your-github-org or dsh-demo.example.com):"
  printf '%s\n' "$PLACEHOLDER_STATE" | grep -En "$PLACEHOLDER_RE" | sed 's/^/      /' >&2
  echo "      Replace --github-org, --redirect-url, the Gateway listener host and the HTTPRoute hostname with the real public FQDN and organization, then re-apply and restart the proxy." >&2
else
  pass "no your-github-org / dsh-demo.example.com placeholder in the applied SSO objects"
fi

# The Gateway must reference an existing TLS Secret; the default name is
# dsh-demo-tls, but 70-gateway.yaml can be edited to another name.
if [ -n "$CERT_REFS" ]; then
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    ref_ns=${ref%%/*}
    ref_name=${ref#*/}
    ref_ns=${ref_ns:-$NS}
    if kubectl -n "$ref_ns" get secret "$ref_name" >/dev/null 2>&1; then
      pass "Gateway TLS certificate reference ${ref_ns}/${ref_name} exists"
    else
      fail "Gateway references TLS Secret ${ref_ns}/${ref_name} but it does not exist. Create it (see manifests/exposure/gateway-api-sso/74-tls-secret.yaml.example)."
    fi
  done <<<"$CERT_REFS"
else
  warn "the Gateway has no TLS certificateRefs; SSO over plain HTTP would expose the session cookie. Terminate TLS at the Gateway listener."
fi

# A second path to the gateway defeats SSO: anyone who can reach
# dsh-gateway:8080 can spoof X-Forwarded-User. Match on the routing backend,
# not on the Ingress name: an Ingress called anything else still routes.
if kubectl -n "$NS" get ingress >/dev/null 2>&1; then
  INGRESS_STATE=$(kubectl -n "$NS" get ingress -o json 2>/dev/null || true)
  if INGRESS_HITS=$(printf '%s' "$INGRESS_STATE" | python3 -c '
import json, sys
try:
    objects = json.load(sys.stdin).get("items", [])
except ValueError:
    objects = []
for obj in objects:
    spec = obj.get("spec") or {}
    backends = []
    default = spec.get("defaultBackend") or {}
    if default:
        backends.append(default)
    for rule in spec.get("rules") or []:
        for path in ((rule.get("http") or {}).get("paths") or []):
            backends.append((path.get("backend") or {}))
    for backend in backends:
        service = (backend.get("service") or {}).get("name")
        if service == "dsh-gateway":
            print(obj.get("metadata", {}).get("name", "?"))
            break
'); then
    if [ -n "$INGRESS_HITS" ]; then
      fail "an Ingress in namespace $NS routes to Service dsh-gateway ($INGRESS_HITS). With SSO enabled the gateway must be reachable only through oauth2-proxy: delete the Ingress variant (kubectl delete -f manifests/exposure/ingress/)."
    fi
  fi
fi

# The SSO variant's policies are part of the variant: without them the
# gateway Service is reachable by any Pod, which can spoof the identity header.
# All three shipped policies are checked - the egress one keeps a compromised
# proxy from reaching anything but DNS, public HTTPS and the gateway.
echo
echo "== SSO NetworkPolicies (required with this variant)"
for POLICY in dsh-gateway-ingress oauth2-proxy-ingress oauth2-proxy-egress; do
  if kubectl -n "$NS" get networkpolicy "$POLICY" >/dev/null 2>&1; then
    pass "NetworkPolicy $POLICY exists"
  else
    fail "NetworkPolicy $POLICY is missing: any Pod in the cluster can reach dsh-gateway:8080 and spoof X-Forwarded-User, so the SSO identity is not trustworthy. Fix: kubectl apply -f manifests/exposure/gateway-api-sso/75-networkpolicies.yaml"
  fi
done

# An oauth2-proxy ingress rule that accepts traffic from every namespace makes
# any pod a usable identity-header injector.
if NS_JSON=$(kubectl -n "$NS" get networkpolicy oauth2-proxy-ingress -o json 2>/dev/null); then
  if printf '%s' "$NS_JSON" | python3 -c '
import json, sys
policy = json.load(sys.stdin)
for rule in ((policy.get("spec") or {}).get("ingress") or []):
    for peer in rule.get("from") or []:
        if "namespaceSelector" in peer and not (peer.get("namespaceSelector") or {}):
            sys.exit(1)
'; then
    pass "oauth2-proxy-ingress restricts source namespaces"
  else
    fail "oauth2-proxy-ingress accepts traffic from every namespace (empty namespaceSelector): any pod in the cluster can reach oauth2-proxy and inject X-Forwarded-User. Fix: scope the namespaceSelector to the Gateway implementation's namespace in manifests/exposure/gateway-api-sso/75-networkpolicies.yaml, then re-apply it."
  fi
fi

echo
if [ "$FAILED" -ne 0 ]; then
  echo "preflight-sso: FAILED - fix the items above before exposing the gateway." >&2
  exit 1
fi
echo "preflight-sso: ok - the SSO variant looks correctly configured."
