"""Unit tests for the browser-usable DSH gateway.

Everything here runs without a cluster: the Kubernetes adapter, the upstream
httpx client and the WebSocket connector are injected fakes.  The HTTP tests
drive the real ASGI app through ``httpx.ASGITransport``; the WebSocket tests
use Starlette's ``TestClient`` (which runs the lifespan) against a fake
``ws_connect`` async context manager.
"""

import asyncio
import contextlib
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from kubernetes.client.exceptions import ApiException
from starlette.responses import Response
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway import (
    Session,
    Settings,
    UserExists,
    claim_name,
    claim_user,
    condition,
    create_app,
    derive_state,
    extract_dsh_cookies,
    filtered_query,
    gateway_managed,
    parse_dsh_launch_token,
    rewrite_location,
    same_origin,
    token_matches,
)

TOKEN = "s3cret-token"
POD_UID = "pod-uid-1"
POD_IP = "10.0.0.7"
FQDN = "dsh-alice-sbx.test-ns.svc.cluster.local"


def dsh_response(status=200, content=b"", headers=None):
    """A MockTransport response that is a real stream (``aiter_raw`` works)."""
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(content))


# --- fakes -----------------------------------------------------------------


class FakeK8s:
    def __init__(self):
        self.claims = {}
        self.sandboxes = {}
        self.pods = {}
        self.logs = {}
        self.created = []
        self.deleted = []
        self.patches = []
        self.token_patches = []
        self.conflict_on_create = False
        self.get_claim_error = None
        self.patch_error = None

    async def get_claim(self, user):
        if self.get_claim_error is not None:
            raise self.get_claim_error
        return self.claims.get(user)

    async def create_claim(self, user, token_hash, pool):
        if self.conflict_on_create:
            raise UserExists(user)
        self.created.append((user, token_hash, pool))
        self.claims.setdefault(user, {"metadata": {"name": claim_name(user)}})

    async def delete_claim(self, user):
        self.deleted.append(user)
        self.claims.pop(user, None)

    async def list_claims(self):
        return list(self.claims.values())

    async def get_sandbox(self, name):
        return self.sandboxes.get(name)

    async def patch_operating_mode(self, name, mode):
        if self.patch_error is not None:
            raise self.patch_error
        self.patches.append((name, mode))

    async def patch_token(self, user, token_hash):
        if self.patch_error is not None:
            raise self.patch_error
        self.token_patches.append((user, token_hash))
        claim = self.claims.get(user)
        if claim is not None:
            annotations = claim.setdefault("metadata", {}).setdefault("annotations", {})
            annotations["aaas.example.com/token-sha256"] = token_hash

    async def get_pod(self, name):
        return self.pods.get(name)

    async def read_pod_logs(self, name, container, tail_lines):
        return self.logs.get(name, "")


class FakeUpstream:
    """An httpx.MockTransport handler for both the handshake and the proxy."""

    def __init__(self):
        self.requests = []
        self.handshake_requests = []
        self.handshake_status = 303
        self.handshake_cookies = ["dsh-auth-a=one; Path=/; HttpOnly; SameSite=Strict"]
        self.proxy_handler = None
        self.proxy_requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/" and "token=" in str(request.url.query):
            self.handshake_requests.append(request)
            cookies = self.handshake_cookies
            index = min(len(self.handshake_requests) - 1, len(cookies) - 1)
            return dsh_response(
                self.handshake_status,
                headers=[
                    ("location", "/"),
                    ("set-cookie", cookies[index]),
                ],
            )
        self.proxy_requests.append(request)
        if self.proxy_handler is not None:
            return self.proxy_handler(request)
        return dsh_response(200, b"hello from DSH", {"content-type": "text/plain"})


class FakeWS:
    """Async context manager matching websockets' ClientConnection subset."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.close_code = None
        self.subprotocol = None
        self._stage = 0
        self._sent_event = asyncio.Event()

    async def send(self, data):
        self.sent.append(data)
        self._sent_event.set()

    async def close(self, code=1000):
        self.closed = True
        self.close_code = code

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._stage == 0:
            self._stage = 1
            return "welcome"
        if self._stage == 1:
            await self._sent_event.wait()
            self._stage = 2
            return "pong"
        raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeWSConnect:
    def __init__(self):
        self.calls = []
        self.instances = []
        self.error = None

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        ws = FakeWS()
        self.instances.append(ws)
        return ws


def add_user(k8s, user="alice", token=TOKEN, ready=True, pod_name=None, suffix=""):
    """Populate the fake API objects for a Ready (or Waking) user."""
    sandbox_name = f"dsh-{user}-sbx{suffix}"
    pod_name = pod_name or sandbox_name
    k8s.claims[user] = {
        "metadata": {
            "name": claim_name(user),
            "annotations": {
                "aaas.example.com/token-sha256": hashlib.sha256(token.encode()).hexdigest()
            },
        },
        "status": {"sandbox": {"name": sandbox_name}},
    }
    k8s.sandboxes[sandbox_name] = {
        "metadata": {"name": sandbox_name, "annotations": {}},
        "spec": {},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}] if ready else [],
            "serviceFQDN": FQDN,
            "podIPs": [POD_IP],
        },
    }
    k8s.pods[pod_name] = {"metadata": {"name": pod_name, "uid": POD_UID}}
    k8s.logs[pod_name] = f"booting\ndsh web: http://10.0.0.7:3080/?token={TOKEN}-launch\n"
    return sandbox_name, pod_name


# --- browser sessions ------------------------------------------------------
#
# A browser session is an opaque server-side id.  Tests must never forge a
# cookie (or a bearer token); they mint a real session through the gateway so
# the production lookup path is exercised.


def mint_session(env, user="alice") -> tuple[str, str]:
    """Mint a server-side session for ``user``; returns ``(cookie_name, sid)``."""
    response = Response()
    sid = env.gateway.set_session(response, user)
    return env.gateway.settings.cookie_name, sid


def session_dict(env, user="alice") -> dict:
    name, sid = mint_session(env, user)
    return {name: sid}


def session_cookie(env, user="alice") -> str:
    name, sid = mint_session(env, user)
    return f"{name}={sid}"


def response_session_user(env, response) -> str:
    """The user of the server-side session a response just set."""
    cookie = response.headers["set-cookie"]
    value = cookie.split(";", 1)[0].split("=", 1)[1]
    session = env.gateway.state.sessions.get(value)
    assert session is not None, "response did not set a server-side session"
    return session.user


@pytest.fixture
def env():
    settings = Settings(
        namespace="test-ns",
        pool="test-pool",
        idle_timeout=60,
        wake_timeout=1,
        sweep_interval=3600,
        upstream_retry_delay=0,
    )
    k8s = FakeK8s()
    upstream = FakeUpstream()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), timeout=5)
    ws_connect = FakeWSConnect()
    app = create_app(
        settings=settings,
        k8s=k8s,
        http_client=http_client,
        ws_connect=ws_connect,
    )
    return SimpleNamespace(
        app=app,
        gateway=app.state.gateway,
        settings=settings,
        k8s=k8s,
        upstream=upstream,
        ws_connect=ws_connect,
        http_client=http_client,
    )


def asgi_client(env, cookies=None):
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app),
        base_url="http://gateway.test",
    )
    if cookies:
        client.cookies.update(cookies)
    return client


def stale_handshake(env):
    env.gateway.state.handshake[("alice", POD_UID, "gateway.test")] = "dsh-auth-a=stale"


def raw_scope(method, path, headers=()):
    """An ASGI HTTP scope for driving the app without an httpx client.

    ``spec_version`` matches what uvicorn 0.54 advertises (2.3), which is what
    puts ``StreamingResponse`` on its disconnect-listening path.
    """
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("gateway.test", 80),
        "client": ("10.0.0.9", 4321),
        "headers": [(b"host", b"gateway.test"), *headers],
    }


async def asgi_call(env, scope, receive):
    """Drive the app with a scripted ``receive`` and return the sent messages.

    An exception escaping the app fails the test, which is exactly how a
    ``ClientDisconnect``-induced traceback used to surface.
    """
    sent = []

    async def send(message):
        sent.append(message)

    await env.app(scope, receive, send)
    return sent


# --- pure helpers ----------------------------------------------------------


def test_condition_true_when_status_matches():
    obj = {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    assert condition(obj, "Ready") is True


def test_condition_false_when_status_is_false():
    obj = {"status": {"conditions": [{"type": "Ready", "status": "False"}]}}
    assert condition(obj, "Ready") is False


def test_condition_false_when_type_absent():
    obj = {"status": {"conditions": [{"type": "Suspended", "status": "True"}]}}
    assert condition(obj, "Ready") is False


def test_condition_false_when_no_status():
    assert condition({}, "Ready") is False


def test_derive_state_provisioning_when_sandbox_missing():
    assert derive_state(None) == "Provisioning"


def test_derive_state_ready():
    sandbox = {"spec": {}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    assert derive_state(sandbox) == "Ready"


def test_derive_state_waking_when_not_yet_ready():
    sandbox = {"spec": {"operatingMode": "Running"}, "status": {"conditions": []}}
    assert derive_state(sandbox) == "Waking"


def test_derive_state_suspended():
    sandbox = {
        "spec": {"operatingMode": "Suspended"},
        "status": {"conditions": [{"type": "Suspended", "status": "True"}]},
    }
    assert derive_state(sandbox) == "Suspended"


def test_derive_state_suspending_while_mode_flipped_but_not_settled():
    sandbox = {"spec": {"operatingMode": "Suspended"}, "status": {"conditions": []}}
    assert derive_state(sandbox) == "Suspending"


def test_claim_name_and_user_roundtrip():
    assert claim_name("alice") == "dsh-alice"
    assert claim_user({"metadata": {"name": "dsh-alice"}}) == "alice"


def test_gateway_managed_requires_ownership_annotation():
    managed = {"metadata": {"annotations": {"aaas.example.com/managed-by": "dsh-gateway"}}}
    manual = {"metadata": {"name": "dsh-alice"}}
    other = {"metadata": {"annotations": {"aaas.example.com/managed-by": "someone"}}}
    assert gateway_managed(managed) is True
    assert gateway_managed(manual) is False
    assert gateway_managed(other) is False


def test_parse_dsh_launch_token_from_real_log_line():
    log = (
        "2026-10-21T10:00:00Z starting\n"
        "dsh web: http://10.244.1.7:3080/?token=abc-_123XYZ\n"
        "some later line\n"
    )
    assert parse_dsh_launch_token(log) == "abc-_123XYZ"


def test_parse_dsh_launch_token_uses_last_match():
    log = (
        "dsh web: http://10.0.0.1:3080/?token=old\n"
        "restart\n"
        "dsh web: http://10.0.0.2:3080/?token=new\n"
    )
    assert parse_dsh_launch_token(log) == "new"


@pytest.mark.parametrize(
    "log",
    ["", "no token here", "dsh web: http://10.0.0.1:3080/?token=", "dsh web: garbage"],
)
def test_parse_dsh_launch_token_returns_none_without_token(log):
    assert parse_dsh_launch_token(log) is None


def test_extract_dsh_cookies_keeps_only_dsh_auth_pairs():
    headers = [
        "dsh-auth-a=one; Path=/; HttpOnly; SameSite=Strict",
        "other=two; Path=/",
        "dsh-auth-b=three; Path=/",
    ]
    assert extract_dsh_cookies(headers) == "dsh-auth-a=one; dsh-auth-b=three"


def test_same_origin_origin_header():
    assert same_origin({"host": "gw.test", "origin": "http://gw.test"}) is True
    assert same_origin({"host": "gw.test", "origin": "http://evil.test"}) is False


def test_same_origin_referer_fallback_and_missing():
    assert same_origin({"host": "gw.test", "referer": "http://gw.test/u/a/"}) is True
    assert same_origin({"host": "gw.test"}) is False


def test_rewrite_location_prefixes_absolute_paths_only():
    assert rewrite_location("/", "alice") == "/u/alice/"
    assert rewrite_location("/assets/app.js", "alice") == "/u/alice/assets/app.js"
    assert rewrite_location("http://x/absolute", "alice") == "http://x/absolute"
    assert rewrite_location("//other/path", "alice") == "//other/path"
    assert rewrite_location("/u/alice/already", "alice") == "/u/alice/already"


def test_rewrite_location_drops_launch_token():
    assert rewrite_location("/?token=secret", "alice") == "/u/alice/"


def test_filtered_query_removes_token_only():
    assert filtered_query("a=1&token=x&b=2&token=y") == "a=1&b=2"


def test_filtered_query_preserves_vite_combined_module_url():
    """DSH's ``plugins/??<specifiers>`` query must reach the upstream verbatim."""
    raw = "?@deepseek-ai/dsh-client-modules/client.js&rev=a1bf7d6a56f9"
    assert filtered_query(raw) == raw
    assert filtered_query(raw + "&token=x") == raw


def test_filtered_query_drops_an_encoded_token_parameter():
    assert filtered_query("a=1&to%6Ben=x&b=2") == "a=1&b=2"


def test_token_matches_is_timing_safe_hash_check():
    claim = {
        "metadata": {
            "annotations": {
                "aaas.example.com/token-sha256": hashlib.sha256(TOKEN.encode()).hexdigest()
            }
        }
    }
    assert token_matches(claim, TOKEN) is True
    assert token_matches(claim, "wrong") is False
    assert token_matches(claim, "") is False
    assert token_matches({"metadata": {}}, TOKEN) is False


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("NAMESPACE", "ns-x")
    monkeypatch.setenv("POOL", "pool-x")
    monkeypatch.setenv("IDLE_TIMEOUT", "7")
    monkeypatch.setenv("WAKE_TIMEOUT", "8")
    monkeypatch.setenv("DSH_PORT", "9999")
    monkeypatch.setenv("DSH_LOG_TAIL_LINES", "42")
    monkeypatch.setenv("COOKIE_NAME", "sid")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.setenv("COOKIE_MAX_AGE", "100")
    monkeypatch.setenv("MAX_BODY_MB", "2")
    monkeypatch.setenv("HTTP_TIMEOUT", "12.5")
    monkeypatch.setenv("AUTH_PROXY_HEADER", "X-Forwarded-User")
    monkeypatch.setenv("AUTH_RATE_LIMIT", "5")
    monkeypatch.setenv("AUTH_RATE_WINDOW", "30")
    monkeypatch.setenv("SIGNUP_INVITE_CODE", "invite-x")
    monkeypatch.setenv("UPSTREAM_HEADER_ALLOWLIST", "X-Custom, Accept")
    monkeypatch.setenv("MAX_IN_FLIGHT_PER_USER", "4")
    monkeypatch.setenv("WS_MAX_MESSAGE_BYTES", "1024")
    settings = Settings.from_env()
    assert settings.namespace == "ns-x"
    assert settings.pool == "pool-x"
    assert settings.idle_timeout == 7
    assert settings.wake_timeout == 8
    assert settings.dsh_port == 9999
    assert settings.dsh_log_tail_lines == 42
    assert settings.cookie_name == "sid"
    assert settings.cookie_secure is True
    assert settings.cookie_max_age == 100
    assert settings.max_body_bytes == 2 * 1024 * 1024
    assert settings.http_timeout == 12.5
    assert settings.auth_proxy_header == "X-Forwarded-User"
    assert settings.auth_rate_limit == 5
    assert settings.auth_rate_window == 30
    assert settings.signup_invite_code == "invite-x"
    assert settings.upstream_header_allowlist == frozenset({"x-custom", "accept"})
    assert settings.max_in_flight_per_user == 4
    assert settings.ws_max_message_bytes == 1024


def test_settings_auth_proxy_header_defaults_to_none(monkeypatch):
    monkeypatch.delenv("AUTH_PROXY_HEADER", raising=False)
    monkeypatch.setenv("ALLOW_INSECURE_COOKIES", "1")
    assert Settings.from_env().auth_proxy_header is None


def test_settings_from_env_rejects_insecure_cookies_by_default(monkeypatch):
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_COOKIES", raising=False)
    with pytest.raises(RuntimeError, match="insecure session cookie"):
        Settings.from_env()


def test_settings_from_env_allows_insecure_cookies_with_the_explicit_flag(monkeypatch):
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    monkeypatch.setenv("ALLOW_INSECURE_COOKIES", "1")
    monkeypatch.delenv("COOKIE_NAME", raising=False)
    settings = Settings.from_env()
    assert settings.cookie_secure is False
    assert settings.cookie_name == "dsh_gw_session"


def test_settings_default_cookie_name_is_host_prefixed_when_secure(monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.delenv("COOKIE_NAME", raising=False)
    assert Settings.from_env().cookie_name == "__Host-dsh_gw_session"


def test_settings_explicit_cookie_name_wins(monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.setenv("COOKIE_NAME", "sid")
    assert Settings.from_env().cookie_name == "sid"


def test_settings_upstream_header_allowlist_unset_means_denylist_mode(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_COOKIES", "1")
    monkeypatch.delenv("UPSTREAM_HEADER_ALLOWLIST", raising=False)
    assert Settings.from_env().upstream_header_allowlist is None
    monkeypatch.setenv("UPSTREAM_HEADER_ALLOWLIST", "")
    assert Settings.from_env().upstream_header_allowlist == frozenset()


# --- Kubernetes adapter ----------------------------------------------------


@pytest.mark.asyncio
async def test_kubernetes_create_claim_body(monkeypatch):
    from gateway import Kubernetes

    calls = []

    async def fake_call(which, method, *args):
        calls.append((which, method, args))

    k8s = Kubernetes("test-ns")
    monkeypatch.setattr(k8s, "_call", fake_call)
    await k8s.create_claim("alice", "hash", "test-pool")

    which, method, args = calls[0]
    assert which == "crd"
    assert method == "create_namespaced_custom_object"
    body = args[4]
    assert body["metadata"]["name"] == "dsh-alice"
    assert body["metadata"]["annotations"]["aaas.example.com/token-sha256"] == "hash"
    assert body["metadata"]["annotations"]["aaas.example.com/managed-by"] == "dsh-gateway"
    assert body["spec"]["warmPoolRef"]["name"] == "test-pool"
    assert body["spec"]["additionalPodMetadata"]["labels"]["sandbox.users.io/dsh-user"] == "alice"


@pytest.mark.asyncio
async def test_kubernetes_maps_404_to_none_and_propagates_other_errors(monkeypatch):
    from gateway import Kubernetes

    k8s = Kubernetes("test-ns")
    calls = {"n": 0}

    async def fake_call(which, method, *args):
        calls["n"] += 1
        raise ApiException(status=404 if calls["n"] == 1 else 500)

    monkeypatch.setattr(k8s, "_call", fake_call)
    assert await k8s.get_claim("alice") is None
    with pytest.raises(ApiException):
        await k8s.get_claim("alice")


@pytest.mark.asyncio
async def test_kubernetes_read_pod_logs_forwards_container_and_tail(monkeypatch):
    from gateway import Kubernetes

    calls = []

    class Core:
        def read_namespaced_pod_log(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "log line"

    k8s = Kubernetes("test-ns")
    monkeypatch.setattr(k8s, "_clients", lambda: (None, Core()))
    assert await k8s.read_pod_logs("pod-1", "dsh-web", 200) == "log line"
    assert calls == [(("pod-1", "test-ns"), {"container": "dsh-web", "tail_lines": 200})]


@pytest.mark.asyncio
async def test_kubernetes_get_pod_normalises_the_v1pod_model(monkeypatch):
    """CoreV1Api returns a V1Pod model; callers index it like a dict."""
    from kubernetes.client import V1ObjectMeta, V1Pod

    from gateway import Kubernetes

    async def fake_call(which, method, *args):
        return V1Pod(metadata=V1ObjectMeta(name="dsh-alice", uid="pod-uid-9"))

    k8s = Kubernetes("test-ns")
    monkeypatch.setattr(k8s, "_call", fake_call)

    pod = await k8s.get_pod("dsh-alice")

    assert isinstance(pod, dict)
    assert pod["metadata"]["uid"] == "pod-uid-9"


def test_kubernetes_clients_initialise_once_under_concurrency(monkeypatch):
    """_call runs on a worker pool: the lazy init must not hand out a None client."""
    import gateway as gw
    from gateway import Kubernetes

    created = []

    class Crd:
        def __init__(self):
            created.append("crd")

    class SlowCore:
        def __init__(self):
            time.sleep(0.05)  # widen the window between _crd and _core
            created.append("core")

    monkeypatch.setattr(gw.k8s_client, "CustomObjectsApi", Crd)
    monkeypatch.setattr(gw.k8s_client, "CoreV1Api", SlowCore)
    monkeypatch.setattr(gw.k8s_config, "load_incluster_config", lambda: None)

    k8s = Kubernetes("test-ns")
    results = []

    def worker():
        results.append(k8s._clients())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(core is not None for _, core in results)
    assert created.count("core") == 1


# --- health / landing / redirect ------------------------------------------


@pytest.mark.asyncio
async def test_healthz_needs_no_auth_and_no_cluster(env):
    async with asgi_client(env) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_healthz_does_not_touch_the_cluster_at_startup():
    # No k8s injected: create_app builds the lazy Kubernetes adapter, which
    # must not load credentials until a claim/pod call actually happens.
    app = create_app(
        settings=Settings(namespace="test-ns"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: dsh_response())),
        ws_connect=lambda *a, **k: None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    ) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_landing_page_has_signup_and_login_forms(env):
    async with asgi_client(env) as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'action="/users"' in response.text
    assert 'action="/login"' in response.text
    # No invite code configured => no invite input on the landing page.
    assert 'name="invite"' not in response.text


@pytest.mark.asyncio
async def test_landing_page_shows_invite_input_only_when_configured(env):
    configure(env, signup_invite_code="let-me-in")
    async with asgi_client(env) as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert 'name="invite"' in response.text


@pytest.mark.asyncio
async def test_landing_page_shows_session_when_cookie_present(env):
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/")
    assert "Signed in as" in response.text
    assert "/u/alice/" in response.text


@pytest.mark.asyncio
async def test_workspace_redirect_adds_trailing_slash(env):
    async with asgi_client(env) as client:
        response = await client.get("/u/alice?x=1")
    assert response.status_code == 308
    assert response.headers["location"] == "/u/alice/?x=1"


# --- signup / login / logout ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_user", ["Alice", "-alice", "alice-", "", "a_b", 123, None, "a" * 60])
async def test_signup_rejects_invalid_dns1123_label(env, bad_user):
    async with asgi_client(env) as client:
        response = await client.post("/users", json={"user": bad_user})
    assert response.status_code == 400
    assert env.k8s.created == []


@pytest.mark.asyncio
async def test_signup_conflict_when_claim_exists(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.post("/users", json={"user": "alice"})
    assert response.status_code == 409
    assert env.k8s.created == []


@pytest.mark.asyncio
async def test_signup_conflict_on_concurrent_race(env):
    env.k8s.conflict_on_create = True
    async with asgi_client(env) as client:
        response = await client.post("/users", json={"user": "alice"})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_signup_json_stores_only_hash_and_returns_token(env, monkeypatch):
    sandbox = {
        "metadata": {"name": "dsh-alice-sbx"},
        "spec": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }

    async def ready(user, timeout):
        return sandbox

    monkeypatch.setattr(env.gateway, "wait_ready", ready)
    async with asgi_client(env) as client:
        response = await client.post("/users", json={"user": "alice"})

    assert response.status_code == 201
    body = response.json()
    assert body["user"] == "alice"
    assert body["state"] == "Ready"
    assert body["sandbox"] == "dsh-alice-sbx"
    assert body["token"]
    user, token_hash, pool = env.k8s.created[0]
    assert user == "alice"
    assert pool == "test-pool"
    assert token_hash == hashlib.sha256(body["token"].encode()).hexdigest()
    assert token_hash != body["token"]
    # The JSON API also gets a browser session cookie (HttpOnly, SameSite=Lax)
    # carrying only an opaque server-side session id, never the user/token.
    cookie = response.headers["set-cookie"]
    assert "dsh_gw_session=" in cookie
    assert f"dsh_gw_session={TOKEN}" not in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert response_session_user(env, response) == "alice"


@pytest.mark.asyncio
async def test_signup_returns_202_when_sandbox_not_ready(env, monkeypatch):
    async def not_ready(user, timeout):
        return None

    monkeypatch.setattr(env.gateway, "wait_ready", not_ready)
    async with asgi_client(env) as client:
        response = await client.post("/users", json={"user": "alice"})
    assert response.status_code == 202
    assert response.json()["state"] == "Provisioning"


@pytest.mark.asyncio
async def test_signup_form_redirects_and_sets_session_cookie(env, monkeypatch):
    async def not_ready(user, timeout):
        return None

    monkeypatch.setattr(env.gateway, "wait_ready", not_ready)
    async with asgi_client(env) as client:
        response = await client.post(
            "/users",
            data={"user": "bob"},
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/u/bob/"
    assert response_session_user(env, response) == "bob"


@pytest.mark.asyncio
async def test_signup_body_limit_returns_413(env):
    env.settings = Settings(**{**env.settings.__dict__, "max_body_bytes": 8})
    env.gateway.settings = env.settings
    async with asgi_client(env) as client:
        response = await client.post(
            "/users",
            content=json.dumps({"user": "alice"}),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_login_form_sets_cookie_and_redirects(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.post(
            "/login",
            data={"user": "alice", "token": TOKEN},
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/u/alice/"
    assert response_session_user(env, response) == "alice"


@pytest.mark.asyncio
async def test_secure_session_cookie_uses_the_host_prefix_and_flags(env):
    configure(env, cookie_secure=True, cookie_name="__Host-dsh_gw_session")
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.post(
            "/login",
            data={"user": "alice", "token": TOKEN},
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-dsh_gw_session=")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/" in cookie
    assert "SameSite=lax" in cookie
    assert "Domain=" not in cookie
    # The value is an opaque session id, not <user>:<token>.
    assert "alice" not in cookie.split(";", 1)[0]
    assert TOKEN not in cookie


@pytest.mark.asyncio
async def test_login_json_and_failures(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        ok = await client.post("/login", json={"user": "alice", "token": TOKEN})
        bad = await client.post("/login", json={"user": "alice", "token": "nope"})
        missing = await client.post("/login", json={"user": "ghost", "token": TOKEN})
    assert ok.status_code == 200
    assert bad.status_code == 401
    # An unknown user is a 401 too: login must not reveal which users exist.
    assert missing.status_code == 401


@pytest.mark.asyncio
async def test_logout_clears_cookie_and_revokes_the_session(env):
    add_user(env.k8s)
    name, sid = mint_session(env)
    assert env.gateway.session_for(sid) is not None
    async with asgi_client(env, cookies={name: sid}) as client:
        response = await client.post("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "dsh_gw_session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    # The old sid is dead server-side, not just deleted from the browser.
    assert env.gateway.session_for(sid) is None
    async with asgi_client(env, cookies={name: sid}) as client:
        replay = await client.get("/users/alice")
    assert replay.status_code == 401


@pytest.mark.asyncio
async def test_logout_deletion_cookie_is_secure_for_the_host_prefix(env):
    """A __Host- cookie can only be cleared by a Secure deletion Set-Cookie."""
    configure(env, cookie_secure=True, cookie_name="__Host-dsh_gw_session")
    add_user(env.k8s)
    name, sid = mint_session(env)
    assert name == "__Host-dsh_gw_session"
    async with asgi_client(env, cookies={name: sid}) as client:
        response = await client.post("/logout")
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-dsh_gw_session=")
    assert "Max-Age=0" in cookie
    assert "Path=/" in cookie
    assert "Secure" in cookie
    assert "Domain=" not in cookie


# --- signup invite / auth abuse controls -----------------------------------


def configure(env, **overrides):
    """Return a copy of the fixture settings with overrides applied."""
    env.settings = Settings(**{**env.settings.__dict__, **overrides})
    env.gateway.settings = env.settings
    return env


@pytest.mark.asyncio
async def test_signup_requires_invite_when_configured(env, monkeypatch):
    configure(env, signup_invite_code="invite-code")

    async def not_ready(user, timeout):
        return None

    monkeypatch.setattr(env.gateway, "wait_ready", not_ready)
    async with asgi_client(env) as client:
        missing = await client.post("/users", json={"user": "alice"})
        wrong = await client.post("/users", json={"user": "alice", "invite": "nope"})
        good = await client.post("/users", json={"user": "alice", "invite": "invite-code"})
    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert good.status_code == 202
    assert [created[0] for created in env.k8s.created] == ["alice"]


@pytest.mark.asyncio
async def test_invite_is_checked_before_the_user_lookup(env):
    configure(env, signup_invite_code="invite-code")
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.post("/users", json={"user": "alice"})
    # 403, not 409: an unauthenticated caller cannot probe for existing users.
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_invite_accepts_the_header_and_the_form_field(env, monkeypatch):
    configure(env, signup_invite_code="invite-code")

    async def not_ready(user, timeout):
        return None

    monkeypatch.setattr(env.gateway, "wait_ready", not_ready)
    async with asgi_client(env) as client:
        via_header = await client.post(
            "/users",
            json={"user": "alice"},
            headers={"X-Signup-Invite": "invite-code"},
        )
        via_form = await client.post(
            "/users",
            data={"user": "bob", "invite": "invite-code"},
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert via_header.status_code == 202
    assert via_form.status_code == 303


@pytest.mark.asyncio
async def test_form_auth_rejects_cross_origin_requests(env):
    add_user(env.k8s)
    form = {"content-type": "application/x-www-form-urlencoded"}
    async with asgi_client(env) as client:
        signup = await client.post(
            "/users", data={"user": "bob"}, headers={**form, "Origin": "http://evil.test"}
        )
        login = await client.post(
            "/login",
            data={"user": "alice", "token": TOKEN},
            headers={**form, "Origin": "http://evil.test"},
        )
        referer = await client.post(
            "/login",
            data={"user": "alice", "token": TOKEN},
            headers={**form, "Referer": "http://evil.test/login"},
        )
        same_origin = await client.post(
            "/login",
            data={"user": "alice", "token": TOKEN},
            headers={**form, "Origin": "http://gateway.test"},
        )
    assert signup.status_code == 403
    assert login.status_code == 403
    assert referer.status_code == 403
    assert same_origin.status_code == 303


@pytest.mark.asyncio
async def test_auth_rate_limit_returns_429_with_retry_after(env):
    configure(env, auth_rate_limit=2, auth_rate_window=60)
    add_user(env.k8s)
    async with asgi_client(env) as client:
        first = await client.post("/login", json={"user": "alice", "token": "nope"})
        second = await client.post("/login", json={"user": "alice", "token": "nope"})
        third = await client.post("/login", json={"user": "alice", "token": "nope"})
    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 429
    assert int(third.headers["retry-after"]) >= 1


@pytest.mark.asyncio
async def test_auth_rate_limit_is_per_client_via_rightmost_forwarded_for(env):
    configure(env, auth_rate_limit=1, auth_rate_window=60)
    add_user(env.k8s)
    async with asgi_client(env) as client:
        first = await client.post(
            "/login",
            json={"user": "alice", "token": "nope"},
            headers={"X-Forwarded-For": "1.1.1.1, 10.0.0.1"},
        )
        # A spoofed leftmost entry cannot escape the same rightmost-client bucket.
        same_client = await client.post(
            "/login",
            json={"user": "alice", "token": "nope"},
            headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"},
        )
        other_client = await client.post(
            "/login",
            json={"user": "alice", "token": "nope"},
            headers={"X-Forwarded-For": "10.0.0.2"},
        )
    assert first.status_code == 401
    assert same_client.status_code == 429
    assert other_client.status_code == 401


@pytest.mark.asyncio
async def test_auth_rate_limit_zero_disables_it(env):
    configure(env, auth_rate_limit=0)
    add_user(env.k8s)
    async with asgi_client(env) as client:
        responses = [
            await client.post("/login", json={"user": "alice", "token": "nope"}) for _ in range(40)
        ]
    assert {response.status_code for response in responses} == {401}


@pytest.mark.asyncio
async def test_signup_is_rate_limited_before_the_body_is_read(env):
    """429 wins over parsing: an abusive client cannot even submit a body."""
    configure(env, auth_rate_limit=1, auth_rate_window=60)
    async with asgi_client(env) as client:
        # Spend the single allowed auth attempt on a login, then hit signup.
        await client.post("/login", json={"user": "alice", "token": "nope"})
        limited = await client.post("/users", content=b"not json at all")
    assert limited.status_code == 429
    assert env.k8s.created == []


# --- auth ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_requires_valid_bearer_or_cookie(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        forbidden = await client.get("/users/alice", headers={"Authorization": "Bearer wrong"})
        bearer = await client.get("/users/alice", headers={"Authorization": f"Bearer {TOKEN}"})
        cookie = await client.get(
            "/users/alice",
            headers={"Cookie": session_cookie(env)},
        )
        wrong_user_cookie = await client.get(
            "/users/alice",
            headers={"Cookie": session_cookie(env, "bob")},
        )
    assert forbidden.status_code == 401
    assert bearer.status_code == 200
    assert bearer.json()["state"] == "Ready"
    assert cookie.status_code == 200
    assert wrong_user_cookie.status_code == 401


@pytest.mark.asyncio
async def test_get_user_not_found(env):
    async with asgi_client(env) as client:
        response = await client.get("/users/alice")
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/users/Alice", "/u/Alice/", "/u/Alice"])
async def test_invalid_user_in_path_is_404(env, path):
    async with asgi_client(env) as client:
        response = await client.get(path)
    assert response.status_code == 404
    assert env.k8s.claims == {}


@pytest.mark.asyncio
async def test_delete_user_requires_auth(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.delete("/users/alice", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert env.k8s.deleted == []


@pytest.mark.asyncio
async def test_delete_with_cookie_requires_same_origin(env):
    add_user(env.k8s)
    env.gateway.state.handshake[("alice", POD_UID, "gateway.test")] = "dsh-auth-a=x"
    cookies = session_dict(env)
    async with asgi_client(env, cookies=cookies) as client:
        missing = await client.delete("/users/alice")
        cross = await client.delete("/users/alice", headers={"Origin": "http://evil.test"})
        good = await client.delete("/users/alice", headers={"Origin": "http://gateway.test"})
    assert missing.status_code == 403
    assert cross.status_code == 403
    assert good.status_code == 200
    assert env.k8s.deleted == ["alice"]
    assert env.gateway.state.handshake == {}


@pytest.mark.asyncio
async def test_delete_with_bearer_does_not_need_origin(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.delete("/users/alice", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_delete_missing_claim_is_404(env):
    add_user(env.k8s)
    env.k8s.claims.clear()
    async with asgi_client(env) as client:
        response = await client.delete("/users/alice", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unexpected_api_error_propagates(env):
    add_user(env.k8s)
    env.k8s.get_claim_error = ApiException(status=500)
    async with asgi_client(env) as client:
        with pytest.raises(ApiException):
            await client.get("/users/alice")


@pytest.mark.asyncio
async def test_bearer_token_never_authorizes_another_user(env):
    add_user(env.k8s, "alice", token=TOKEN)
    add_user(env.k8s, "bob", token="bob-token")
    async with asgi_client(env) as client:
        alice_on_bob = await client.get("/users/bob", headers={"Authorization": f"Bearer {TOKEN}"})
        bob_on_alice = await client.get(
            "/users/alice", headers={"Authorization": "Bearer bob-token"}
        )
        alice_on_alice = await client.get(
            "/users/alice", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert alice_on_bob.status_code == 401
    assert bob_on_alice.status_code == 401
    assert alice_on_alice.status_code == 200


@pytest.mark.asyncio
async def test_session_cookie_never_authorizes_another_user(env):
    add_user(env.k8s, "alice", token=TOKEN)
    add_user(env.k8s, "bob", token="bob-token")
    bob_cookie = session_cookie(env, "bob")
    async with asgi_client(env) as client:
        user = await client.get("/users/alice", headers={"Cookie": bob_cookie})
        proxy = await client.get("/u/alice/api/state", headers={"Cookie": bob_cookie})
    assert user.status_code == 401
    assert proxy.status_code == 401


@pytest.mark.asyncio
async def test_opaque_session_cookie_is_not_a_bearer_token(env):
    add_user(env.k8s)
    _name, sid = mint_session(env)
    async with asgi_client(env) as client:
        response = await client.get("/users/alice", headers={"Authorization": f"Bearer {sid}"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_session_is_revoked_and_treated_as_anonymous(env):
    add_user(env.k8s)
    name, sid = mint_session(env)
    env.gateway.state.sessions[sid] = Session(user="alice", expires_at=time.time() - 1)
    async with asgi_client(env, cookies={name: sid}) as client:
        user = await client.get("/users/alice")
        landing = await client.get("/")
    assert user.status_code == 401
    assert "Signed in as" not in landing.text
    assert sid not in env.gateway.state.sessions


@pytest.mark.asyncio
async def test_delete_user_revokes_all_its_sessions(env):
    add_user(env.k8s)
    cookie = session_dict(env)
    other = session_dict(env)  # a second browser session for the same user
    async with asgi_client(env, cookies=cookie) as client:
        response = await client.delete("/users/alice", headers={"Origin": "http://gateway.test"})
    assert response.status_code == 200
    assert env.gateway.state.sessions == {}
    async with asgi_client(env, cookies=other) as client:
        landing = await client.get("/")
    assert "Signed in as" not in landing.text


# --- DSH handshake ---------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_handshakes_and_uses_browser_host(env):
    add_user(env.k8s)
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 200
    assert response.text == "hello from DSH"
    handshake = env.upstream.handshake_requests[0]
    assert handshake.url.host == FQDN
    assert handshake.url.port == 3080
    assert handshake.url.params["token"] == TOKEN + "-launch"
    assert handshake.headers["host"] == "gateway.test"
    proxy = env.upstream.proxy_requests[0]
    assert proxy.headers["cookie"] == "dsh-auth-a=one"


@pytest.mark.asyncio
async def test_dsh_cookie_is_cached_per_pod_uid_and_host(env):
    add_user(env.k8s)
    async with asgi_client(env, cookies=session_dict(env)) as client:
        first = await client.get("/u/alice/api/one")
        second = await client.get("/u/alice/api/two")
    assert first.status_code == second.status_code == 200
    assert len(env.upstream.handshake_requests) == 1
    assert len(env.upstream.proxy_requests) == 2


@pytest.mark.asyncio
async def test_missing_launch_token_returns_503_with_retry_after(env):
    _sandbox_name, pod_name = add_user(env.k8s)
    env.k8s.logs[pod_name] = "no token printed yet\n"
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"
    assert "launch token" in response.json()["error"]


@pytest.mark.asyncio
async def test_handshake_4xx_is_503(env):
    add_user(env.k8s)
    env.upstream.handshake_status = 401
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_missing_cookie_in_handshake_is_503(env):
    add_user(env.k8s)
    env.upstream.handshake_cookies = ["other=1; Path=/"]
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_handshake_error_never_leaks_the_launch_token_or_url(env):
    _sandbox_name, pod_name = add_user(env.k8s)
    secret = "super-secret-launch-token"
    env.k8s.logs[pod_name] = f"dsh web: http://10.0.0.7:3080/?token={secret}\n"
    env.upstream.handshake_status = 500
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 503
    body = response.text
    assert secret not in body
    assert FQDN not in body
    assert "10.0.0.7" not in body
    assert "dsh-auth" not in body


@pytest.mark.asyncio
async def test_pod_annotation_overrides_sandbox_name(env):
    sandbox_name, pod_name = add_user(env.k8s)
    env.k8s.sandboxes[sandbox_name]["metadata"]["annotations"]["agents.x-k8s.io/pod-name"] = (
        "custom-pod"
    )
    env.k8s.pods["custom-pod"] = {"metadata": {"uid": "other-uid"}}
    env.k8s.logs["custom-pod"] = env.k8s.logs[pod_name]
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 200
    assert ("alice", "other-uid", "gateway.test") in env.gateway.state.handshake


# --- HTTP proxy ------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_strips_gateway_auth_cookie_and_hop_headers(env):
    add_user(env.k8s)

    def handler(request):
        return dsh_response(
            200,
            b"ok",
            {
                "content-type": "text/plain",
                "connection": "keep-alive",
                "set-cookie": "upstream=leak; Path=/",
            },
        )

    env.upstream.proxy_handler = handler
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Cookie": session_cookie(env) + "; browser=1",
        "X-Forwarded-For": "1.2.3.4",
        "X-Forwarded-Proto": "https",
        "Host": "gateway.test",
        "Origin": "http://gateway.test",
    }
    async with asgi_client(env) as client:
        response = await client.get("/u/alice/api/foo?token=secret&x=1", headers=headers)

    assert response.status_code == 200
    proxy = env.upstream.proxy_requests[0]
    assert proxy.url.path == "/api/foo"
    assert proxy.url.query.decode() == "x=1"
    assert "authorization" not in proxy.headers
    assert proxy.headers["cookie"] == "dsh-auth-a=one"
    assert "browser=1" not in proxy.headers["cookie"]
    assert proxy.headers["host"] == "gateway.test"
    assert proxy.headers["origin"] == "http://gateway.test"
    assert proxy.headers["x-forwarded-for"] == "127.0.0.1"
    assert proxy.headers["x-forwarded-proto"] == "http"
    # Upstream Set-Cookie and hop-by-hop headers never reach the browser.
    assert "set-cookie" not in response.headers
    assert "connection" not in response.headers


@pytest.mark.asyncio
async def test_proxy_strips_client_supplied_forwarding_and_sso_headers(env):
    add_user(env.k8s)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Forwarded": "for=evil.test;proto=https",
        "X-Forwarded-User": "bob",
        "X-Forwarded-Host": "evil.test",
        "X-Auth-Request-User": "bob",
        "X-Auth-Request-Email": "bob@evil.test",
        "X-Real-IP": "6.6.6.6",
        "Origin": "http://gateway.test",
    }
    async with asgi_client(env) as client:
        response = await client.get("/u/alice/api/state", headers=headers)
    assert response.status_code == 200
    proxy = env.upstream.proxy_requests[0]
    for name in (
        "forwarded",
        "x-forwarded-user",
        "x-auth-request-user",
        "x-auth-request-email",
        "x-real-ip",
    ):
        assert name not in proxy.headers
    # The gateway's own hop metadata comes from the real connection instead.
    assert proxy.headers["x-forwarded-for"] == "127.0.0.1"
    assert proxy.headers["x-forwarded-host"] == "gateway.test"


@pytest.mark.asyncio
async def test_proxy_upstream_header_allowlist_drops_unlisted_headers(env):
    configure(env, upstream_header_allowlist=frozenset({"x-custom-ok"}))
    add_user(env.k8s)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Custom-Ok": "keep",
        "X-Custom-Drop": "drop",
        "X-Forwarded-User": "bob",
        "Origin": "http://gateway.test",
    }
    async with asgi_client(env) as client:
        response = await client.get("/u/alice/api/state", headers=headers)
    assert response.status_code == 200
    proxy = env.upstream.proxy_requests[0]
    assert proxy.headers["x-custom-ok"] == "keep"
    assert "x-custom-drop" not in proxy.headers
    # Allowlisting never re-enables gateway-owned or spoofable headers.
    assert "authorization" not in proxy.headers
    assert "x-forwarded-user" not in proxy.headers
    assert "cookie" not in proxy.headers or proxy.headers["cookie"] == "dsh-auth-a=one"


@pytest.mark.asyncio
async def test_proxy_rejects_requests_over_the_per_user_in_flight_cap(env):
    configure(env, max_in_flight_per_user=1)
    add_user(env.k8s)
    env.gateway.state.in_flight["alice"] = 1
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 503
    assert "retry-after" in response.headers
    assert env.gateway.state.in_flight["alice"] == 1
    assert env.upstream.proxy_requests == []


@pytest.mark.asyncio
async def test_proxy_accepts_requests_below_the_per_user_in_flight_cap(env):
    configure(env, max_in_flight_per_user=2)
    add_user(env.k8s)
    env.gateway.state.in_flight["alice"] = 1
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 200
    assert env.gateway.state.in_flight["alice"] == 1  # the slot was released again


@pytest.mark.asyncio
async def test_proxy_rewrites_absolute_path_location(env):
    add_user(env.k8s)
    env.upstream.proxy_handler = lambda request: dsh_response(
        303,
        headers=[
            ("location", "/?token=leak&next=1"),
            ("set-cookie", "upstream=leak; Path=/"),
        ],
    )
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/redirect", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/u/alice/?next=1"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_proxy_refreshes_cookie_once_on_upstream_401(env):
    add_user(env.k8s)
    env.upstream.handshake_cookies = ["dsh-auth-a=new; Path=/"]
    stale_handshake(env)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return dsh_response(401, b"expired")
        return dsh_response(200, b"fresh")

    env.upstream.proxy_handler = handler
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 200
    assert response.text == "fresh"
    assert len(calls) == 2
    assert len(env.upstream.handshake_requests) == 1  # only the forced refresh
    assert calls[1].headers["cookie"] == "dsh-auth-a=new"


@pytest.mark.asyncio
async def test_proxy_does_not_refresh_401_without_cached_cookie(env):
    add_user(env.k8s)
    env.upstream.proxy_handler = lambda request: dsh_response(401, b"no")
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 401
    assert len(env.upstream.proxy_requests) == 1


@pytest.mark.asyncio
async def test_proxy_upstream_connection_error_is_502(env):
    add_user(env.k8s)

    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    env.upstream.proxy_handler = handler
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 502
    assert env.gateway.state.in_flight["alice"] == 0


@pytest.mark.asyncio
async def test_proxy_body_limit_returns_413(env):
    add_user(env.k8s)
    env.gateway.settings = Settings(**{**env.settings.__dict__, "max_body_bytes": 4})
    async with asgi_client(env) as client:
        response = await client.post(
            "/u/alice/api/echo",
            content=b"too large",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 413
    assert env.gateway.state.in_flight["alice"] == 0


@pytest.mark.asyncio
async def test_proxy_client_disconnect_mid_body_returns_499(env):
    """An aborted upload is answered 499 quietly and never reaches DSH."""
    add_user(env.k8s)
    messages = [
        {"type": "http.request", "body": b"partial", "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0)

    sent = await asgi_call(
        env,
        raw_scope(
            "POST",
            "/u/alice/api/echo",
            headers=[
                (b"origin", b"http://gateway.test"),
                (b"cookie", session_cookie(env).encode()),
                (b"content-type", b"application/json"),
            ],
        ),
        receive,
    )
    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert [start["status"] for start in starts] == [499]
    assert env.upstream.proxy_requests == []
    assert env.gateway.state.in_flight["alice"] == 0


@pytest.mark.asyncio
async def test_signup_client_disconnect_mid_body_returns_499(env):
    """Body-reading control routes get the same treatment, not just the proxy."""
    messages = [
        {"type": "http.request", "body": b'{"user": "ali', "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0)

    sent = await asgi_call(
        env,
        raw_scope("POST", "/users", headers=[(b"content-type", b"application/json")]),
        receive,
    )
    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert [start["status"] for start in starts] == [499]
    assert env.k8s.claims == {}


@pytest.mark.asyncio
async def test_proxy_client_disconnect_mid_stream_frees_slot(env):
    """Closing an open SSE-style stream frees the slot without a traceback."""
    add_user(env.k8s)
    first_chunk = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"chunk-one"
            first_chunk.set()
            await asyncio.Event().wait()  # the upstream stream stays open

    env.upstream.proxy_handler = lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=SlowStream()
    )
    messages = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        if messages:
            return messages.pop(0)
        await first_chunk.wait()
        return {"type": "http.disconnect"}

    sent = await asgi_call(
        env,
        raw_scope(
            "GET",
            "/u/alice/api/stream",
            headers=[(b"cookie", session_cookie(env).encode())],
        ),
        receive,
    )
    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert [start["status"] for start in starts] == [200]
    assert env.gateway.state.in_flight["alice"] == 0


@pytest.mark.asyncio
async def test_release_frees_slot_even_when_cancelled(env):
    """A cancelled request must still return its in-flight slot.

    Starlette cancels the streaming task through an anyio cancel scope when the
    client disconnects, and the cancellation can land inside ``_relay``'s
    ``finally``.  Waiting for ``state.lock`` there would strand the slot, which
    keeps the sandbox looking busy forever.
    """
    env.gateway.state.in_flight["alice"] = 1
    await env.gateway.state.lock.acquire()
    try:
        task = asyncio.create_task(env.gateway._release("alice"))
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        env.gateway.state.lock.release()
    assert env.gateway.state.in_flight["alice"] == 0


@pytest.mark.asyncio
async def test_proxy_client_disconnect_survives_cancelled_upstream_close(env):
    """A cancellation that lands inside ``upstream.aclose()`` must not skip the release.

    CancelledError is a BaseException, so ``contextlib.suppress(Exception)``
    around the close would let it escape before ``_release`` ran.  A real
    network close awaits, which is where the cancel scope delivers it.
    """
    add_user(env.k8s)
    first_chunk = asyncio.Event()

    class ClosingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"chunk-one"
            first_chunk.set()
            await asyncio.Event().wait()

        async def aclose(self):
            await asyncio.sleep(0)  # a checkpoint for the cancel scope

    env.upstream.proxy_handler = lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=ClosingStream()
    )
    messages = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        if messages:
            return messages.pop(0)
        await first_chunk.wait()
        return {"type": "http.disconnect"}

    sent = await asgi_call(
        env,
        raw_scope(
            "GET",
            "/u/alice/api/stream",
            headers=[(b"cookie", session_cookie(env).encode())],
        ),
        receive,
    )
    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert [start["status"] for start in starts] == [200]
    assert env.gateway.state.in_flight["alice"] == 0


@pytest.mark.asyncio
async def test_proxy_wakes_suspended_sandbox(env, monkeypatch):
    sandbox_name, _ = add_user(env.k8s, ready=False)
    env.k8s.sandboxes[sandbox_name]["spec"]["operatingMode"] = "Suspended"
    env.upstream.proxy_handler = lambda request: dsh_response(200, b"awake")

    async def ready(user, timeout):
        env.k8s.sandboxes[sandbox_name]["spec"]["operatingMode"] = "Running"
        env.k8s.sandboxes[sandbox_name]["status"] = {
            "conditions": [{"type": "Ready", "status": "True"}],
            "serviceFQDN": FQDN,
        }
        return env.k8s.sandboxes[sandbox_name]

    monkeypatch.setattr(env.gateway, "wait_ready", ready)
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 200
    assert ("dsh-alice-sbx", "Running") in env.k8s.patches


@pytest.mark.asyncio
async def test_proxy_wake_timeout_returns_503(env, monkeypatch):
    sandbox_name, _ = add_user(env.k8s, ready=False)
    env.k8s.sandboxes[sandbox_name]["spec"]["operatingMode"] = "Suspended"

    async def never(user, timeout):
        return None

    monkeypatch.setattr(env.gateway, "wait_ready", never)
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"


@pytest.mark.asyncio
async def test_proxy_unknown_user_is_404(env):
    async with asgi_client(env) as client:
        response = await client.get("/u/ghost/api/state")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_proxy_requires_auth(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_proxy_cookie_mutation_requires_same_origin(env):
    add_user(env.k8s)
    cookies = session_dict(env)
    async with asgi_client(env, cookies=cookies) as client:
        cross = await client.post(
            "/u/alice/api/mutate", headers={"Origin": "http://evil.test"}, content=b"{}"
        )
        good = await client.post(
            "/u/alice/api/mutate",
            headers={"Origin": "http://gateway.test"},
            content=b"{}",
        )
    assert cross.status_code == 403
    assert good.status_code == 200


def test_stream_timeout_leaves_read_unbounded(env):
    timeout = env.gateway._stream_timeout()
    assert timeout.read is None
    assert timeout.connect == env.settings.http_timeout


@pytest.mark.asyncio
async def test_proxied_requests_use_the_stream_timeout(env, monkeypatch):
    from starlette.requests import Request

    captured = {}
    real_build = env.gateway.http.build_request

    def spy_build(method, url, **kwargs):
        captured.update(kwargs)
        return real_build(method, url, **kwargs)

    monkeypatch.setattr(env.gateway.http, "build_request", spy_build)
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": "/u/alice/events",
        "raw_path": b"/u/alice/events",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"gateway.test")],
        "client": ("127.0.0.1", 4321),
        "server": ("gateway.test", 80),
        "path_params": {"user": "alice", "path": "events"},
    }
    response = await env.gateway._send_upstream(Request(scope), "alice", FQDN, "dsh-auth-a=1", b"")
    await response.aclose()

    assert captured["timeout"].read is None


@pytest.mark.asyncio
async def test_relay_ends_cleanly_when_the_upstream_stream_breaks(env):
    class Broken:
        closed = False

        async def aiter_raw(self):
            yield b"chunk"
            raise httpx.ReadTimeout("upstream went quiet")

        async def aclose(self):
            self.closed = True

    upstream = Broken()
    chunks = [chunk async for chunk in env.gateway._relay("alice", upstream)]

    assert chunks == [b"chunk"]
    assert upstream.closed is True
    assert env.gateway.state.in_flight["alice"] == 0


# --- WebSocket proxy -------------------------------------------------------


def test_ws_unauthenticated_closes_4401(env):
    with TestClient(env.app) as client:
        with client.websocket_connect("/u/alice/api/remote.mux") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
    assert exc.value.code == 4401


def test_ws_cookie_cross_origin_closes_4401(env):
    add_user(env.k8s)
    with TestClient(env.app) as client:
        with client.websocket_connect(
            "/u/alice/api/remote.mux",
            headers={
                "cookie": session_cookie(env),
                "origin": "http://evil.test",
            },
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
    assert exc.value.code == 4401


def test_ws_bridges_frames_and_injects_cookie(env):
    add_user(env.k8s)
    with TestClient(env.app) as client:
        with client.websocket_connect(
            "/u/alice/api/remote.mux?token=secret&x=1",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "welcome"
            assert ws.receive_text() == "pong"

    url, kwargs = env.ws_connect.calls[0]
    # DSH's Host fence: dial the pod IP (its bind address) and never forward
    # the browser's Host/Origin, which would make the upgrade 403.
    assert url.startswith(f"ws://{POD_IP}:3080/api/remote.mux")
    assert "token=secret" not in url
    assert "x=1" in url
    headers = kwargs["additional_headers"]
    assert "authorization" not in headers
    assert headers["cookie"] == "dsh-auth-a=one"
    assert "host" not in headers
    assert "origin" not in headers
    assert env.ws_connect.instances[0].sent == ["hello"]


def test_ws_cookie_same_origin_bridges_frames(env):
    add_user(env.k8s)
    with TestClient(env.app) as client:
        with client.websocket_connect(
            "/u/alice/api/remote.mux",
            headers={
                "cookie": session_cookie(env),
                "origin": "http://testserver",
            },
        ) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "welcome"
            assert ws.receive_text() == "pong"

    headers = env.ws_connect.calls[0][1]["additional_headers"]
    assert headers["cookie"] == "dsh-auth-a=one"
    assert "origin" not in headers


def test_ws_handshake_failure_closes_1011(env):
    _, pod_name = add_user(env.k8s)
    env.k8s.logs[pod_name] = "token not ready\n"
    with TestClient(env.app) as client:
        with client.websocket_connect(
            "/u/alice/api/remote.mux",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
    assert exc.value.code == 1011


def test_ws_upstream_connect_failure_closes_1011(env):
    add_user(env.k8s)
    env.ws_connect.error = RuntimeError("cannot connect")
    with TestClient(env.app) as client:
        with client.websocket_connect(
            "/u/alice/api/remote.mux",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
    assert exc.value.code == 1011


def test_ws_strips_client_supplied_forwarding_and_sso_headers(env):
    add_user(env.k8s)
    with TestClient(env.app) as client:
        with client.websocket_connect(
            "/u/alice/api/remote.mux",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Forwarded": "for=evil.test",
                "X-Forwarded-User": "bob",
                "X-Auth-Request-User": "bob",
            },
        ) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "welcome"
            assert ws.receive_text() == "pong"
    headers = env.ws_connect.calls[0][1]["additional_headers"]
    assert "forwarded" not in headers
    assert "x-forwarded-user" not in headers
    assert "x-auth-request-user" not in headers
    assert headers["cookie"] == "dsh-auth-a=one"


def test_ws_per_user_in_flight_cap_closes_1011(env):
    configure(env, max_in_flight_per_user=1)
    add_user(env.k8s)
    env.gateway.state.in_flight["alice"] = 1
    with TestClient(env.app) as client:
        with client.websocket_connect(
            "/u/alice/api/remote.mux",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
    assert exc.value.code == 1011
    assert env.ws_connect.calls == []


def test_ws_passes_the_default_max_message_size(env):
    add_user(env.k8s)
    with TestClient(env.app) as client:
        with client.websocket_connect(
            "/u/alice/api/remote.mux",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "welcome"
            assert ws.receive_text() == "pong"
    assert env.ws_connect.calls[0][1]["max_size"] == 16 * 1024 * 1024


def test_ws_max_message_size_is_configurable(env):
    configure(env, ws_max_message_bytes=2048)
    add_user(env.k8s)
    with TestClient(env.app) as client:
        with client.websocket_connect(
            "/u/alice/api/remote.mux",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "welcome"
            assert ws.receive_text() == "pong"
    assert env.ws_connect.calls[0][1]["max_size"] == 2048


# --- idle sweeper ----------------------------------------------------------


def ready_claim(user="alice"):
    return {
        "metadata": {
            "name": claim_name(user),
            "annotations": {"aaas.example.com/managed-by": "dsh-gateway"},
        },
        "status": {"sandbox": {"name": f"dsh-{user}-sbx"}},
    }


def ready_sandbox(user="alice"):
    return {
        "metadata": {"name": f"dsh-{user}-sbx"},
        "spec": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


@pytest.mark.asyncio
async def test_sweeper_skips_claim_it_does_not_own(env):
    """A hand-applied claim (e.g. README 30-claim-alice.yaml) must not be swept.

    The gateway shares the namespace with the resource-layer walkthrough; those
    claims are named dsh-<user> too, but carry no ownership annotation.  The
    sweeper used to suspend them 60s after first sighting, deleting the pod that
    README step 3 port-forwards to.
    """
    env.k8s.claims["alice"] = {
        "metadata": {"name": claim_name("alice")},
        "status": {"sandbox": {"name": "dsh-alice-sbx"}},
    }
    env.k8s.sandboxes["dsh-alice-sbx"] = ready_sandbox()

    # The sweeper never starts an idle clock for a claim it does not own...
    await env.gateway.sweep_once()
    assert "alice" not in env.gateway.state.last_activity

    # ...and does not suspend it even when it looks long-idle.
    env.gateway.state.last_activity["alice"] = time.time() - env.settings.idle_timeout - 10
    await env.gateway.sweep_once()
    assert env.k8s.patches == []


@pytest.mark.asyncio
async def test_sweeper_suspends_idle_ready_sandbox(env):
    env.k8s.claims["alice"] = ready_claim()
    env.k8s.sandboxes["dsh-alice-sbx"] = ready_sandbox()
    env.gateway.state.last_activity["alice"] = time.time() - env.settings.idle_timeout - 10
    env.gateway.state.handshake[("alice", POD_UID, "gateway.test")] = "dsh-auth-a=x"

    await env.gateway.sweep_once()

    assert env.k8s.patches == [("dsh-alice-sbx", "Suspended")]
    assert env.gateway.state.handshake == {}
    assert env.gateway.state.maintenance["alice"] is False


@pytest.mark.asyncio
async def test_sweeper_skips_in_flight_request(env):
    env.k8s.claims["alice"] = ready_claim()
    env.k8s.sandboxes["dsh-alice-sbx"] = ready_sandbox()
    env.gateway.state.last_activity["alice"] = time.time() - 1000
    env.gateway.state.in_flight["alice"] = 1

    await env.gateway.sweep_once()

    assert env.k8s.patches == []


@pytest.mark.asyncio
async def test_sweeper_skips_recently_active_user(env):
    env.k8s.claims["alice"] = ready_claim()
    env.k8s.sandboxes["dsh-alice-sbx"] = ready_sandbox()
    env.gateway.state.last_activity["alice"] = time.time()

    await env.gateway.sweep_once()

    assert env.k8s.patches == []


@pytest.mark.asyncio
async def test_sweeper_skips_sandbox_not_ready(env):
    env.k8s.claims["alice"] = ready_claim()
    env.k8s.sandboxes["dsh-alice-sbx"] = {
        "metadata": {"name": "dsh-alice-sbx"},
        "spec": {},
        "status": {"conditions": []},
    }
    env.gateway.state.last_activity["alice"] = time.time() - 1000

    await env.gateway.sweep_once()

    assert env.k8s.patches == []


@pytest.mark.asyncio
async def test_sweeper_survives_transient_api_error(env):
    env.k8s.claims["alice"] = ready_claim()
    env.k8s.sandboxes["dsh-alice-sbx"] = ready_sandbox()
    env.gateway.state.last_activity["alice"] = time.time() - 1000
    env.k8s.patch_error = ApiException(status=500)

    with pytest.raises(ApiException):
        await env.gateway.sweep_once()

    # The per-user maintenance flag must be released even on failure.
    assert env.gateway.state.maintenance["alice"] is False


@pytest.mark.asyncio
async def test_sweeper_prunes_orphan_and_expired_state(env):
    now = time.time()
    # No claim for "ghost": every per-user entry is orphaned.
    env.gateway.state.last_activity["ghost"] = now
    env.gateway.state.in_flight["ghost"] = 0
    env.gateway.state.maintenance["ghost"] = False
    env.gateway.state.handshake[("ghost", POD_UID, "gateway.test")] = "dsh-auth-a=x"
    _ghost_name, ghost_sid = mint_session(env, "ghost")
    env.gateway.state.auth_rate["1.2.3.4"] = (now - 3600, 5)
    # A user with requests still in flight is preserved even without a claim.
    env.gateway.state.in_flight["busy"] = 2
    # A live claim keeps its user's current session...
    env.k8s.claims["alice"] = ready_claim()
    env.k8s.sandboxes["dsh-alice-sbx"] = ready_sandbox()
    env.gateway.state.last_activity["alice"] = now
    _alice_name, alice_sid = mint_session(env, "alice")
    # ...but an expired session is pruned even for a live user.
    _old_name, expired_sid = mint_session(env, "alice")
    env.gateway.state.sessions[expired_sid] = Session(user="alice", expires_at=now - 1)

    await env.gateway.sweep_once()

    assert "ghost" not in env.gateway.state.last_activity
    assert "ghost" not in env.gateway.state.in_flight
    assert "ghost" not in env.gateway.state.maintenance
    assert ("ghost", POD_UID, "gateway.test") not in env.gateway.state.handshake
    assert ghost_sid not in env.gateway.state.sessions
    assert env.gateway.state.auth_rate == {}
    assert env.gateway.state.in_flight["busy"] == 2
    assert alice_sid in env.gateway.state.sessions
    assert expired_sid not in env.gateway.state.sessions


# --- SSO (reverse-proxy identity) ------------------------------------------


def enable_sso(env, header="X-Forwarded-User"):
    """Flip an env into SSO mode: the gateway trusts the named identity header."""
    return configure(env, auth_proxy_header=header)


def test_remote_user_none_when_sso_disabled(env):
    assert env.gateway.remote_user({"X-Forwarded-User": "alice"}) is None


def test_remote_user_returns_valid_and_rejects_invalid(env):
    enable_sso(env)
    g = env.gateway
    assert g.remote_user({"X-Forwarded-User": "alice"}) == "alice"
    assert g.remote_user({"X-Forwarded-User": "  bob "}) == "bob"
    assert g.remote_user({"X-Forwarded-User": "UPPER"}) is None
    assert g.remote_user({"X-Forwarded-User": "bad name"}) is None
    assert g.remote_user({"X-Forwarded-User": ""}) is None
    assert g.remote_user({}) is None


@pytest.mark.asyncio
async def test_sso_landing_new_user_shows_provisioning(env):
    enable_sso(env)
    async with asgi_client(env) as client:
        response = await client.get("/", headers={"X-Forwarded-User": "alice"})
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Signed in as <strong>alice</strong>" in response.text
    assert "Provisioning" in response.text
    assert "Recreate sandbox" not in response.text
    assert response_session_user(env, response) == "alice"
    assert [c[0] for c in env.k8s.created] == ["alice"]
    assert env.k8s.token_patches == []


@pytest.mark.asyncio
async def test_sso_landing_ready_connects_and_offers_recreate_only(env):
    enable_sso(env)
    add_user(env.k8s)
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.get("/", headers={"X-Forwarded-User": "alice"})
    assert response.status_code == 200
    assert "Signed in as <strong>alice</strong>" in response.text
    assert "Recreate sandbox" in response.text
    # No signup/token forms for SSO users.
    assert 'action="/users"' not in response.text
    assert 'action="/login"' not in response.text
    # Auto-connects to the workspace.
    assert "url=/u/alice/" in response.text
    assert env.k8s.created == []
    assert env.k8s.token_patches == []


@pytest.mark.asyncio
async def test_sso_landing_rotates_token_when_session_missing(env):
    enable_sso(env)
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.get("/", headers={"X-Forwarded-User": "alice"})
    assert response.status_code == 200
    assert "Recreate sandbox" in response.text
    assert response_session_user(env, response) == "alice"
    assert [p[0] for p in env.k8s.token_patches] == ["alice"]
    assert env.k8s.created == []


@pytest.mark.asyncio
async def test_sso_landing_reuses_a_valid_session_for_the_remote_user(env):
    enable_sso(env)
    add_user(env.k8s)
    name, sid = mint_session(env)
    async with asgi_client(env, cookies={name: sid}) as client:
        response = await client.get("/", headers={"X-Forwarded-User": "alice"})
    assert response.status_code == 200
    assert "Recreate sandbox" in response.text
    # Untouched: no cookie is re-issued and no token is rotated.
    assert "set-cookie" not in response.headers
    assert [p[0] for p in env.k8s.token_patches] == []
    assert env.gateway.state.sessions[sid].user == "alice"


@pytest.mark.asyncio
async def test_sso_landing_revokes_a_foreign_session(env):
    enable_sso(env)
    add_user(env.k8s)
    bob_name, bob_sid = mint_session(env, "bob")
    async with asgi_client(env, cookies={bob_name: bob_sid}) as client:
        response = await client.get("/", headers={"X-Forwarded-User": "alice"})
    assert response.status_code == 200
    # A session for another identity is never reused and is revoked server-side.
    assert bob_sid not in env.gateway.state.sessions
    assert response_session_user(env, response) == "alice"


@pytest.mark.asyncio
async def test_recreate_revokes_the_old_session_and_mints_a_new_one(env):
    enable_sso(env)
    add_user(env.k8s)
    name, sid = mint_session(env)
    async with asgi_client(env, cookies={name: sid}) as client:
        response = await client.post(
            "/recreate",
            headers={"X-Forwarded-User": "alice", "Origin": "http://gateway.test"},
        )
    assert response.status_code == 303
    assert sid not in env.gateway.state.sessions
    new_sid = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    assert env.gateway.state.sessions[new_sid].user == "alice"


@pytest.mark.asyncio
async def test_recreate_deletes_and_reprovisions(env):
    enable_sso(env)
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.post("/recreate", headers={"X-Forwarded-User": "alice"})
    assert response.status_code == 303
    assert response.headers["location"] == "/u/alice/"
    assert env.k8s.deleted == ["alice"]
    assert [c[0] for c in env.k8s.created] == ["alice"]
    assert response_session_user(env, response) == "alice"


@pytest.mark.asyncio
async def test_recreate_requires_sso(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.post("/recreate", headers={"X-Forwarded-User": "alice"})
    assert response.status_code == 401
    assert env.k8s.deleted == []


@pytest.mark.asyncio
async def test_recreate_rejects_cross_origin(env):
    enable_sso(env)
    add_user(env.k8s)
    async with asgi_client(env, cookies=session_dict(env)) as client:
        response = await client.post(
            "/recreate",
            headers={"X-Forwarded-User": "alice", "Origin": "http://evil.test"},
        )
    assert response.status_code == 403
    assert env.k8s.deleted == []


@pytest.mark.asyncio
async def test_sso_get_user_authorizes_via_header(env):
    enable_sso(env)
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.get("/users/alice", headers={"X-Forwarded-User": "alice"})
    assert response.status_code == 200
    assert response.json()["user"] == "alice"


@pytest.mark.asyncio
async def test_sso_proxy_redirects_to_bootstrap_without_session(env):
    enable_sso(env)
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.get("/u/alice/", headers={"X-Forwarded-User": "alice"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.asyncio
async def test_sso_proxy_redirects_to_bootstrap_when_claim_missing(env):
    enable_sso(env)
    async with asgi_client(env) as client:
        response = await client.get("/u/alice/", headers={"X-Forwarded-User": "alice"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.asyncio
async def test_spoofed_header_ignored_when_sso_disabled(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.get("/u/alice/", headers={"X-Forwarded-User": "alice"})
    assert response.status_code == 401
