"""Unit tests for the browser-usable DSH gateway.

Everything here runs without a cluster: the Kubernetes adapter, the upstream
httpx client and the WebSocket connector are injected fakes.  The HTTP tests
drive the real ASGI app through ``httpx.ASGITransport``; the WebSocket tests
use Starlette's ``TestClient`` (which runs the lifespan) against a fake
``ws_connect`` async context manager.
"""

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from kubernetes.client.exceptions import ApiException
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway import (
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
FQDN = "dsh-alice-sbx.test-ns.svc.cluster.local"


def dsh_response(status=200, content=b"", headers=None):
    """A MockTransport response that is a real stream (``aiter_raw`` works)."""
    return httpx.Response(
        status, headers=headers, stream=httpx.ByteStream(content)
    )


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
        self.handshake_cookies = [
            "dsh-auth-a=one; Path=/; HttpOnly; SameSite=Strict"
        ]
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
        return dsh_response(
            200, b"hello from DSH", {"content-type": "text/plain"}
        )


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
                "aaas.example.com/token-sha256": hashlib.sha256(
                    token.encode()
                ).hexdigest()
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
        },
    }
    k8s.pods[pod_name] = {"metadata": {"name": pod_name, "uid": POD_UID}}
    k8s.logs[pod_name] = (
        "booting\n"
        f"dsh web: http://10.0.0.7:3080/?token={TOKEN}-launch\n"
    )
    return sandbox_name, pod_name


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
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), timeout=5
    )
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
    env.gateway.state.handshake[("alice", POD_UID, "gateway.test")] = (
        "dsh-auth-a=stale"
    )


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


def test_token_matches_is_timing_safe_hash_check():
    claim = {
        "metadata": {
            "annotations": {
                "aaas.example.com/token-sha256": hashlib.sha256(
                    TOKEN.encode()
                ).hexdigest()
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
    assert (
        body["metadata"]["annotations"]["aaas.example.com/managed-by"]
        == "dsh-gateway"
    )
    assert body["spec"]["warmPoolRef"]["name"] == "test-pool"
    assert body["spec"]["additionalPodMetadata"]["labels"][
        "sandbox.users.io/dsh-user"
    ] == "alice"


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


@pytest.mark.asyncio
async def test_landing_page_shows_session_when_cookie_present(env):
    async with asgi_client(
        env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}
    ) as client:
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
@pytest.mark.parametrize(
    "bad_user", ["Alice", "-alice", "alice-", "", "a_b", 123, None, "a" * 60]
)
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
    # The JSON API also gets a browser session cookie (HttpOnly, SameSite=Lax).
    cookie = response.headers["set-cookie"]
    assert "dsh_gw_session=alice:" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


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
    cookie = response.headers["set-cookie"]
    assert re_cookie_user(cookie) == "bob"


def re_cookie_user(set_cookie: str) -> str:
    value = set_cookie.split(";", 1)[0].split("=", 1)[1]
    return value.split(":", 1)[0]


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
    assert re_cookie_user(response.headers["set-cookie"]) == "alice"


@pytest.mark.asyncio
async def test_login_json_and_failures(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        ok = await client.post("/login", json={"user": "alice", "token": TOKEN})
        bad = await client.post("/login", json={"user": "alice", "token": "nope"})
        missing = await client.post("/login", json={"user": "ghost", "token": TOKEN})
    assert ok.status_code == 200
    assert bad.status_code == 401
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_logout_clears_cookie(env):
    async with asgi_client(
        env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}
    ) as client:
        response = await client.post("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "dsh_gw_session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


# --- auth ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_requires_valid_bearer_or_cookie(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        forbidden = await client.get(
            "/users/alice", headers={"Authorization": "Bearer wrong"}
        )
        bearer = await client.get(
            "/users/alice", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        cookie = await client.get(
            "/users/alice",
            headers={"Cookie": f"dsh_gw_session=alice:{TOKEN}"},
        )
        wrong_user_cookie = await client.get(
            "/users/alice",
            headers={"Cookie": f"dsh_gw_session=bob:{TOKEN}"},
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
        response = await client.delete(
            "/users/alice", headers={"Authorization": "Bearer wrong"}
        )
    assert response.status_code == 401
    assert env.k8s.deleted == []


@pytest.mark.asyncio
async def test_delete_with_cookie_requires_same_origin(env):
    add_user(env.k8s)
    env.gateway.state.handshake[("alice", POD_UID, "gateway.test")] = "dsh-auth-a=x"
    cookies = {"dsh_gw_session": f"alice:{TOKEN}"}
    async with asgi_client(env, cookies=cookies) as client:
        missing = await client.delete("/users/alice")
        cross = await client.delete(
            "/users/alice", headers={"Origin": "http://evil.test"}
        )
        good = await client.delete(
            "/users/alice", headers={"Origin": "http://gateway.test"}
        )
    assert missing.status_code == 403
    assert cross.status_code == 403
    assert good.status_code == 200
    assert env.k8s.deleted == ["alice"]
    assert env.gateway.state.handshake == {}


@pytest.mark.asyncio
async def test_delete_with_bearer_does_not_need_origin(env):
    add_user(env.k8s)
    async with asgi_client(env) as client:
        response = await client.delete(
            "/users/alice", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_delete_missing_claim_is_404(env):
    add_user(env.k8s)
    env.k8s.claims.clear()
    async with asgi_client(env) as client:
        response = await client.delete(
            "/users/alice", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unexpected_api_error_propagates(env):
    add_user(env.k8s)
    env.k8s.get_claim_error = ApiException(status=500)
    async with asgi_client(env) as client:
        with pytest.raises(ApiException):
            await client.get("/users/alice")


# --- DSH handshake ---------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_handshakes_and_uses_browser_host(env):
    add_user(env.k8s)
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
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
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
        first = await client.get("/u/alice/api/one")
        second = await client.get("/u/alice/api/two")
    assert first.status_code == second.status_code == 200
    assert len(env.upstream.handshake_requests) == 1
    assert len(env.upstream.proxy_requests) == 2


@pytest.mark.asyncio
async def test_missing_launch_token_returns_503_with_retry_after(env):
    _sandbox_name, pod_name = add_user(env.k8s)
    env.k8s.logs[pod_name] = "no token printed yet\n"
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"
    assert "launch token" in response.json()["error"]


@pytest.mark.asyncio
async def test_handshake_4xx_is_503(env):
    add_user(env.k8s)
    env.upstream.handshake_status = 401
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_missing_cookie_in_handshake_is_503(env):
    add_user(env.k8s)
    env.upstream.handshake_cookies = ["other=1; Path=/"]
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_pod_annotation_overrides_sandbox_name(env):
    sandbox_name, pod_name = add_user(env.k8s)
    env.k8s.sandboxes[sandbox_name]["metadata"]["annotations"][
        "agents.x-k8s.io/pod-name"
    ] = "custom-pod"
    env.k8s.pods["custom-pod"] = {"metadata": {"uid": "other-uid"}}
    env.k8s.logs["custom-pod"] = env.k8s.logs[pod_name]
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
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
        "Cookie": "dsh_gw_session=alice:x; browser=1",
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
async def test_proxy_rewrites_absolute_path_location(env):
    add_user(env.k8s)
    env.upstream.proxy_handler = lambda request: dsh_response(
        303,
        headers=[
            ("location", "/?token=leak&next=1"),
            ("set-cookie", "upstream=leak; Path=/"),
        ],
    )
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
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
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
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
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 401
    assert len(env.upstream.proxy_requests) == 1


@pytest.mark.asyncio
async def test_proxy_upstream_connection_error_is_502(env):
    add_user(env.k8s)

    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    env.upstream.proxy_handler = handler
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
        response = await client.get("/u/alice/api/state")
    assert response.status_code == 502
    assert env.gateway.state.in_flight["alice"] == 0


@pytest.mark.asyncio
async def test_proxy_body_limit_returns_413(env):
    add_user(env.k8s)
    env.gateway.settings = Settings(
        **{**env.settings.__dict__, "max_body_bytes": 4}
    )
    async with asgi_client(env) as client:
        response = await client.post(
            "/u/alice/api/echo",
            content=b"too large",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 413
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
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
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
    async with asgi_client(env, cookies={"dsh_gw_session": f"alice:{TOKEN}"}) as client:
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
    cookies = {"dsh_gw_session": f"alice:{TOKEN}"}
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
                "cookie": f"dsh_gw_session=alice:{TOKEN}",
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
    assert url.startswith(f"ws://{FQDN}:3080/api/remote.mux")
    assert "token=secret" not in url
    assert "x=1" in url
    headers = kwargs["additional_headers"]
    assert "authorization" not in headers
    assert headers["cookie"] == "dsh-auth-a=one"
    assert headers["host"] == "testserver"
    assert env.ws_connect.instances[0].sent == ["hello"]


def test_ws_cookie_same_origin_bridges_frames(env):
    add_user(env.k8s)
    with TestClient(env.app) as client:
        with client.websocket_connect(
            "/u/alice/api/remote.mux",
            headers={
                "cookie": f"dsh_gw_session=alice:{TOKEN}",
                "origin": "http://testserver",
            },
        ) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "welcome"
            assert ws.receive_text() == "pong"

    headers = env.ws_connect.calls[0][1]["additional_headers"]
    assert headers["cookie"] == "dsh-auth-a=one"
    assert headers["origin"] == "http://testserver"


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
    env.gateway.state.last_activity["alice"] = (
        time.time() - env.settings.idle_timeout - 10
    )
    await env.gateway.sweep_once()
    assert env.k8s.patches == []


@pytest.mark.asyncio
async def test_sweeper_suspends_idle_ready_sandbox(env):
    env.k8s.claims["alice"] = ready_claim()
    env.k8s.sandboxes["dsh-alice-sbx"] = ready_sandbox()
    env.gateway.state.last_activity["alice"] = (
        time.time() - env.settings.idle_timeout - 10
    )
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
