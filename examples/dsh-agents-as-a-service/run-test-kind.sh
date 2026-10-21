#!/usr/bin/env bash
# Smoke test for the dsh-agents-as-a-service example: runs the full README
# walkthrough non-interactively against the current kubectl context.
# Expects agent-sandbox (with extensions) already installed.
#
# NOTE: the only local port bound is the gateway port-forward on 18080.
# The DSH Web container port (3080) is in-cluster only and is never bound
# on the local host.
set -euo pipefail
cd "$(dirname "$0")"

NS=dsh-demo
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1" >&2; exit 1; }
cleanup() {
  kill "${PF:-}" 2>/dev/null || true
  rm -f "${JAR:-}"
  kubectl delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "=== 1. install prereqs (throwaway key) + template + warm pool"
kubectl apply -f 00-prereqs.yaml
# The provider key is never committed; generate a throwaway value.
kubectl -n "$NS" delete secret dsh-provider-keys --ignore-not-found >/dev/null
kubectl -n "$NS" create secret generic dsh-provider-keys \
  --from-literal=DEEPSEEK_API_KEY="sk-test-$(openssl rand -hex 16)"
kubectl apply -f 10-sandboxtemplate.yaml -f 20-sandboxwarmpool.yaml
for _ in $(seq 1 120); do
  READY=$(kubectl -n "$NS" get sandboxes -l agents.x-k8s.io/warm-pool-sandbox \
    -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | grep -c True || true)
  [ "$READY" -ge 2 ] && break
  sleep 5
done
[ "${READY:-0}" -ge 2 ] || fail "warm pool did not reach 2 Ready spares"
pass "warm pool: 2 spares Ready"

echo "=== 2. signup via claim (warm adoption)"
T0=$(date +%s)
kubectl apply -f 30-claim-alice.yaml
kubectl -n "$NS" wait sandboxclaim dsh-alice --for=condition=Ready --timeout=60s
ELAPSED=$(( $(date +%s) - T0 ))
SB=$(kubectl -n "$NS" get sandboxclaim dsh-alice -o jsonpath='{.status.sandbox.name}')
case "$SB" in dsh-pool-*) ;; *) fail "expected warm adoption, got sandbox '$SB'";; esac
pass "claim Ready in ${ELAPSED}s, adopted $SB"

echo "=== 3. write state on the PVC"
POD="$SB"
kubectl -n "$NS" exec "$POD" -- sh -c 'echo "remember me" > /home/node/.dsh/marker.txt'
pass "marker written in $POD"

echo "=== 4. suspend (pod deleted, PVC + Service retained)"
kubectl -n "$NS" patch sandbox "$SB" --type merge -p '{"spec":{"operatingMode":"Suspended"}}'
kubectl -n "$NS" wait --for=delete "pod/$POD" --timeout=120s
kubectl -n "$NS" get pvc "dsh-home-$SB" >/dev/null || fail "PVC was not retained"
kubectl -n "$NS" get svc "$SB" >/dev/null || fail "Service was not retained"
pass "suspended: pod gone, PVC and Service retained"

echo "=== 5. resume and verify state survived"
kubectl -n "$NS" patch sandbox "$SB" --type merge -p '{"spec":{"operatingMode":"Running"}}'
kubectl -n "$NS" wait sandbox "$SB" --for=condition=Ready --timeout=180s
POD="$SB"
MARKER=$(kubectl -n "$NS" exec "$POD" -- cat /home/node/.dsh/marker.txt)
[ "$MARKER" = "remember me" ] || fail "marker did not survive suspend/resume"
pass "resumed: state survived on the reattached PVC"

echo "=== 6. injection policy rejects env-carrying claims"
kubectl apply -f 40-claim-rejected.yaml
# Wait for the POSITIVE rejection signal, not merely "not bound yet".
REASON=""
for _ in $(seq 1 12); do
  REASON=$(kubectl -n "$NS" get sandboxclaim dsh-mallory \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].reason}' 2>/dev/null || true)
  [ "$REASON" = "EnvVarsInjectionRejected" ] && break
  sleep 5
done
[ "$REASON" = "EnvVarsInjectionRejected" ] \
  || fail "claim was not rejected (Ready reason='$REASON')"
BOUND=$(kubectl -n "$NS" get sandboxclaim dsh-mallory -o jsonpath='{.status.sandbox.name}' 2>/dev/null || true)
[ -z "$BOUND" ] || fail "env-carrying claim unexpectedly bound sandbox '$BOUND'"
kubectl delete -f 40-claim-rejected.yaml >/dev/null
pass "claim rejected with EnvVarsInjectionRejected and never bound"

echo "=== 7. gateway: browser signup, proxy, idle-suspend, wake-on-connect"
if command -v docker >/dev/null && command -v kind >/dev/null; then
  KIND_CLUSTER="${KIND_CLUSTER:-$(kubectl config current-context | sed 's/^kind-//')}"
  # Tag exactly what 50-gateway.yaml references so the smoke test exercises
  # the shipped manifest unchanged (imagePullPolicy: IfNotPresent uses the
  # image loaded into kind).
  GW_IMAGE=ghcr.io/sredevopsorg/deepseek-kubernetes-sandbox:gateway
  docker build -q -t "$GW_IMAGE" gateway/ >/dev/null
  kind load docker-image "$GW_IMAGE" --name "$KIND_CLUSTER" >/dev/null
  kubectl apply -f 50-gateway.yaml
  kubectl -n "$NS" rollout status deploy/dsh-gateway --timeout=120s
  # Local port-forward uses 18080 (NOT 3080, which stays in-cluster).
  kubectl -n "$NS" port-forward svc/dsh-gateway 18080:8080 >/dev/null 2>&1 &
  PF=$!
  GW=localhost:18080
  JAR=$(mktemp)

  for _ in $(seq 1 20); do
    HEALTH=$(curl -s -o /dev/null -w '%{http_code}' "http://$GW/healthz" || true)
    [ "$HEALTH" = "200" ] && break
    sleep 1
  done
  [ "${HEALTH:-}" = "200" ] || fail "gateway /healthz was not reachable on $GW"
  pass "gateway health: /healthz -> 200"

  # Signup exactly as the landing page does it: a form POST, answered with a
  # session cookie. The browser never handles the DSH launch token.
  SIGNUP_CODE=$(curl -s -o /tmp/dsh-gw-signup.out -w '%{http_code}' \
    -c "$JAR" -H "Origin: http://$GW" \
    -X POST "http://$GW/users" -d 'user=bob')
  case "$SIGNUP_CODE" in
    201|202|303) ;;
    *) fail "signup returned HTTP $SIGNUP_CODE: $(cat /tmp/dsh-gw-signup.out)" ;;
  esac
  grep -q 'dsh_gw_session' "$JAR" || fail "signup did not set the dsh_gw_session cookie"
  STATE=""
  for _ in $(seq 1 30); do
    STATE=$(curl -s -b "$JAR" "http://$GW/users/bob" \
      | sed -n 's/.*"state":"\([^"]*\)".*/\1/p')
    [ "$STATE" = "Ready" ] && break
    sleep 2
  done
  [ "$STATE" = "Ready" ] || fail "bob's claim was not Ready (state=$STATE)"
  pass "gateway signup: bob Ready with an HttpOnly session cookie"

  CT=$(curl -s -o /tmp/dsh-u-bob.html -w '%{content_type}' -b "$JAR" "http://$GW/u/bob/")
  case "$CT" in text/html*) ;; *) fail "/u/bob/ returned content-type '$CT', expected text/html";; esac
  grep -qi '<html' /tmp/dsh-u-bob.html || fail "/u/bob/ did not return the DSH Web HTML document"
  pass "gateway proxy: /u/bob/ returns DSH Web HTML through path routing"

  CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://$GW/u/bob/")
  [ "$CODE" = "401" ] || fail "tokenless /u/bob/ returned $CODE, expected 401"
  pass "gateway proxy: tokenless request rejected with 401"

  # Optional WebSocket check: the gateway must reject upgrades without its
  # session cookie and bridge authenticated upgrades to the sandbox.
  if python3 -c 'import websockets' >/dev/null 2>&1; then
    python3 - "$JAR" "$GW" <<'PY' || fail "WebSocket checks failed"
import sys

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

jar_path, gw = sys.argv[1], sys.argv[2]
cookie = None
with open(jar_path, encoding="utf-8") as jar:
    for raw in jar:
        line = raw.removeprefix("#HttpOnly_")
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.rstrip("\n").split("\t")
        if len(fields) >= 7 and fields[5] == "dsh_gw_session":
            cookie = f"{fields[5]}={fields[6]}"
            break
if not cookie:
    sys.exit("FAIL: no dsh_gw_session cookie in the jar")

uri = f"ws://{gw}/u/bob/api/remote.mux"


def connect_ws(headers):
    try:
        return connect(uri, additional_headers=headers, open_timeout=30)
    except TypeError:  # websockets < 14
        return connect(uri, extra_headers=headers, open_timeout=30)


def status_of(exc):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) or getattr(exc, "status_code", None)


# 1. Without the gateway cookie the gateway accepts the upgrade and then
#    closes with application code 4401 (WebSocket auth has no HTTP 401 path).
try:
    with connect(uri, open_timeout=10) as ws:
        try:
            ws.recv(timeout=10)
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            if code != 4401:
                sys.exit(f"FAIL: anonymous WebSocket closed with {code}, expected 4401")
            print("anonymous WebSocket rejected with close code 4401")
        else:
            sys.exit("FAIL: anonymous WebSocket stayed open")
except InvalidStatus as exc:
    sys.exit(f"FAIL: anonymous WebSocket handshake returned HTTP {status_of(exc)}")
except Exception as exc:  # noqa: BLE001 - report any handshake failure
    sys.exit(f"FAIL: anonymous WebSocket check raised {exc!r}")

# 2. With the browser session cookie the upgrade is accepted and bridged.
#    DSH may not serve a WebSocket on '/', so an upstream close (1011) is
#    reported but tolerated; a 4401 would mean the gateway lost the cookie.
try:
    with connect_ws({"Cookie": cookie, "Origin": f"http://{gw}"}) as ws:
        try:
            ws.recv(timeout=10)
            print("authenticated WebSocket accepted and streaming")
        except TimeoutError:
            print("authenticated WebSocket accepted (no immediate close)")
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            if code == 4401:
                sys.exit(
                    "FAIL: authenticated WebSocket was treated as unauthenticated (4401)"
                )
            print(f"authenticated WebSocket accepted; upstream closed with {code}")
except InvalidStatus as exc:
    sys.exit(f"FAIL: authenticated WebSocket handshake returned HTTP {status_of(exc)}")
except Exception as exc:  # noqa: BLE001 - report any handshake failure
    sys.exit(f"FAIL: authenticated WebSocket check raised {exc!r}")
PY
    pass "gateway WebSocket: anonymous rejected (4401), authenticated upgrade bridged"
  else
    echo "WARNING: python websockets not installed; skipping the WebSocket check" >&2
  fi

  echo "waiting for idle sweeper (~75s)..."
  for _ in $(seq 1 30); do
    STATE=$(curl -s -b "$JAR" "http://$GW/users/bob" \
      | sed -n 's/.*"state":"\([^"]*\)".*/\1/p')
    [ "$STATE" = "Suspended" ] && break
    sleep 5
  done
  [ "$STATE" = "Suspended" ] || fail "bob was not idle-suspended (state=$STATE)"
  pass "idle sweeper suspended bob"

  # Regression: alice's claim was applied by hand in step 2, so it has no
  # aaas.example.com/managed-by annotation. The sweeper must leave it alone -
  # even though the same idle window just suspended bob. If it does not, README
  # step 3 (logs/port-forward against alice's pod) breaks.
  ALICE_MODE=$(kubectl -n "$NS" get sandbox "$SB" -o jsonpath='{.spec.operatingMode}')
  [ "$ALICE_MODE" = "Running" ] \
    || fail "sweeper suspended a claim it does not own (alice: $ALICE_MODE)"
  pass "idle sweeper left the hand-applied alice claim Running"

  T0=$(date +%s)
  curl -sf -m 150 -b "$JAR" "http://$GW/u/bob/" >/dev/null \
    || fail "wake-on-connect request failed"
  pass "wake-on-connect: request held and served after $(( $(date +%s) - T0 ))s"

  curl -sf -X DELETE -b "$JAR" -H "Origin: http://$GW" \
    "http://$GW/users/bob" >/dev/null \
    || fail "gateway user deletion failed"
  kill "$PF" 2>/dev/null || true
  rm -f "$JAR"
  pass "gateway delete: bob removed"
elif [ "${SKIP_GATEWAY:-}" = "1" ]; then
  echo "WARNING: gateway checks SKIPPED (SKIP_GATEWAY=1) — signup, auth," >&2
  echo "WARNING: proxy, WebSocket, idle-suspend and wake-on-connect were NOT validated" >&2
  GATEWAY_SKIPPED=1
else
  fail "docker + kind are required for the gateway checks (set SKIP_GATEWAY=1 to run only the resource-layer checks)"
fi

echo "=== 8. cascade delete"
kubectl -n "$NS" delete sandboxclaim dsh-alice
for _ in $(seq 1 24); do
  kubectl -n "$NS" get sandbox "$SB" >/dev/null 2>&1 || break
  sleep 5
done
kubectl -n "$NS" get sandbox "$SB" >/dev/null 2>&1 && fail "sandbox not garbage-collected"
for _ in $(seq 1 24); do
  kubectl -n "$NS" get pvc "dsh-home-$SB" >/dev/null 2>&1 || break
  sleep 5
done
kubectl -n "$NS" get pvc "dsh-home-$SB" >/dev/null 2>&1 && fail "PVC not garbage-collected"
pass "claim delete cascaded: sandbox + PVC gone"

echo
if [ -n "${GATEWAY_SKIPPED:-}" ]; then
  echo "Resource-layer checks passed; gateway checks were SKIPPED."
else
  echo "All checks passed."
fi
