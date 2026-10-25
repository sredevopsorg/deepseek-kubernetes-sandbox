"""Browser-usable gateway for DeepSeek Harness Web (DSH) sandboxes.

This is the control + data plane in front of the agent-sandbox CRDs.  It is a
single teaching-friendly module with three jobs:

* **Users** -- create a ``SandboxClaim`` over a warm pool, mint a bearer token
  (only its SHA-256 is stored, as a claim annotation), report derived state,
  and delete the claim (which cascades the sandbox and its PVC).
* **Browser sessions** -- exchange a user token for an HttpOnly session cookie
  so the DSH UI can be opened in a browser at ``/u/<user>/``.  Browser requests
  authenticate with that cookie; automation can keep using
  ``Authorization: Bearer <token>``.
* **Reverse proxy** -- HTTP (including SSE) and WebSocket proxying to the
  sandbox's DSH Web server on ``:3080``.  DSH itself is cookie-gated: the
  gateway scrapes the launch token from the pod logs, performs the ``?token=``
  handshake server-side, caches the resulting ``dsh-auth-*`` cookie, and
  injects it upstream.  The browser never sees the launch or bearer token.

A background sweeper suspends sandboxes idle for ``IDLE_TIMEOUT`` seconds.
Suspending deletes the pod only; the PVC (and the user's state) survives.

Deliberately NOT production code: single replica, in-memory state and token
cache, no TLS termination, and launch-token discovery by log scraping.

Run locally with::

    uvicorn gateway:create_app --factory --host 0.0.0.0 --port 8080

The factory signature (``Settings``, ``Kubernetes``, an httpx client and the
websockets connector are all injectable) is what makes the test suite
cluster-free.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import (
    parse_qs,
    parse_qsl,
    quote,
    unquote_plus,
    urlencode,
    urlsplit,
    urlunsplit,
)

import anyio
import httpx
import websockets
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

# --- Kubernetes / DSH constants -------------------------------------------

GROUP, VERSION = "extensions.agents.x-k8s.io", "v1beta1"
CORE_GROUP = "agents.x-k8s.io"
TOKEN_ANNOTATION = "aaas.example.com/token-sha256"
# Ownership marker: the gateway shares its namespace with claims applied by
# hand (the resource-layer walkthrough in the example README).  Every claim is
# named "dsh-<user>", so without an explicit marker the sweeper cannot tell its
# own users from a claim someone created manually.
MANAGED_ANNOTATION = "aaas.example.com/managed-by"
MANAGED_BY = "dsh-gateway"
POD_NAME_ANNOTATION = "agents.x-k8s.io/pod-name"
USER_LABEL = "sandbox.users.io/dsh-user"

# RFC 1123 label: lowercase alphanumerics and "-", must start/end alphanumeric.
# The claim name is "dsh-<user>", so cap the user part to keep the claim a
# valid Kubernetes object name.
DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
MAX_USER_LEN = 63 - len("dsh-")

# Headers a proxy must not forward in either direction (RFC 7230 section 6.1).
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Headers the gateway owns: never forwarded from the browser, overwritten with
# the gateway's own view of the connection instead.
OWNED_REQUEST_HEADERS = HOP_BY_HOP | {
    "authorization",
    "cookie",
    "host",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
    "x-real-ip",
}

_DSH_WEB_LINE = re.compile(r"dsh web:\s+(?P<url>https?://\S+)")


class UserExists(Exception):
    """The claim already exists (a 409 from the API server)."""


class BodyTooLarge(Exception):
    """The request body exceeded ``MAX_BODY_MB``."""


class HandshakeError(Exception):
    """DSH's launch token or auth cookie could not be obtained."""


class SandboxUnavailable(Exception):
    """The sandbox is not (yet) ready to serve traffic."""


class UpstreamError(Exception):
    """The upstream DSH connection failed."""


# --- Pure helpers (unit-testable without an app) --------------------------


def valid_user(user: str) -> bool:
    """A user is usable as a claim-name suffix (dns-1123 label, length-capped)."""
    return (
        isinstance(user, str)
        and len(user) <= MAX_USER_LEN
        and DNS1123_LABEL.fullmatch(user) is not None
    )


def claim_name(user: str) -> str:
    return f"dsh-{user}"


def claim_user(claim: dict) -> str:
    return claim["metadata"]["name"].removeprefix("dsh-")


def gateway_managed(claim: dict) -> bool:
    """True only for claims this gateway created.

    The sweeper must not suspend claims it does not own: a claim applied from
    a resource-layer manifest (e.g. ``30-claim-alice.yaml``) has the same
    ``dsh-<user>`` name but no ownership marker, and suspending it deletes a
    pod the operator is using.
    """
    annotations = (claim.get("metadata") or {}).get("annotations") or {}
    return annotations.get(MANAGED_ANNOTATION) == MANAGED_BY


def condition(obj: dict, ctype: str) -> bool:
    for c in (obj.get("status") or {}).get("conditions", []):
        if c.get("type") == ctype:
            return c.get("status") == "True"
    return False


def derive_state(sandbox: dict | None) -> str:
    """The state machine a platform shows users (mode x conditions)."""
    if sandbox is None:
        return "Provisioning"
    mode = sandbox.get("spec", {}).get("operatingMode", "Running")
    if mode == "Suspended":
        return "Suspended" if condition(sandbox, "Suspended") else "Suspending"
    return "Ready" if condition(sandbox, "Ready") else "Waking"


def token_matches(claim: dict, token: str | None) -> bool:
    """Timing-safe check of a user token against the stored SHA-256."""
    want = (claim.get("metadata", {}).get("annotations") or {}).get(
        TOKEN_ANNOTATION, ""
    )
    if not token or not want:
        return False
    got = hashlib.sha256(token.encode()).hexdigest()
    return secrets.compare_digest(got, want)


def parse_dsh_launch_token(logs: str) -> str | None:
    """Return the token from the last ``dsh web: http://...?token=...`` line.

    DSH prints the launch URL once at startup.  We scan the tail of the pod log
    and keep the last match so a restarted process (which prints a new token)
    wins over the stale one.
    """
    token = None
    for line in (logs or "").splitlines():
        match = _DSH_WEB_LINE.search(line)
        if not match:
            continue
        values = parse_qs(urlsplit(match.group("url")).query).get("token")
        if values and values[0]:
            token = values[0]
    return token


def extract_dsh_cookies(set_cookie_headers: list[str]) -> str:
    """Join the ``dsh-auth-*`` pairs from Set-Cookie headers into a Cookie value."""
    pairs = []
    for raw in set_cookie_headers:
        first = raw.split(";", 1)[0]
        name, sep, value = first.partition("=")
        name = name.strip()
        if sep and name.startswith("dsh-auth-"):
            pairs.append(f"{name}={value.strip()}")
    return "; ".join(pairs)


def same_origin(headers) -> bool:
    """True when Origin (or Referer) names the same authority as Host."""
    host = (headers.get("host") or "").strip().lower()
    origin = headers.get("origin")
    if origin:
        return urlsplit(origin).netloc.lower() == host
    referer = headers.get("referer")
    if referer:
        return urlsplit(referer).netloc.lower() == host
    return False


def rewrite_location(location: str, user: str) -> str:
    """Prefix absolute-path redirects so the browser stays under ``/u/<user>/``."""
    if not location.startswith("/") or location.startswith("//"):
        return location
    parts = urlsplit(location)
    prefix = f"/u/{user}"
    if parts.path == prefix or parts.path.startswith(f"{prefix}/"):
        return location
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "token"
    ]
    return urlunsplit(("", "", prefix + parts.path, urlencode(query), parts.fragment))


def filtered_query(query_string: str) -> str:
    """Drop ``?token=`` before a request is sent upstream.

    The remaining query is forwarded byte-for-byte.  DSH's combined-module
    URLs look like ``plugins/??@deepseek-ai/dsh-client-modules/client.js`` —
    the raw query itself begins with ``?`` and DSH routes it verbatim.
    Re-encoding through ``parse_qsl``/``urlencode`` turns that into ``%3F…``,
    the upstream answers 404, and the client boot dies with "HTML did not
    preload @deepseek-ai/dsh-client-modules/client.js".
    """
    if not query_string:
        return ""
    kept = []
    for part in query_string.split("&"):
        if unquote_plus(part.split("=", 1)[0]) == "token":
            continue
        kept.append(part)
    return "&".join(kept)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# --- Settings --------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, read from the environment by default.

    Defaults are the demo values from the example manifests.  Everything is
    injected into ``create_app`` so tests never need the environment or a
    cluster.
    """

    namespace: str = "dsh-demo"
    pool: str = "dsh-pool"
    idle_timeout: int = 60
    wake_timeout: int = 120
    dsh_port: int = 3080
    dsh_container: str = "dsh-web"
    dsh_log_tail_lines: int = 2000
    cookie_name: str = "dsh_gw_session"
    cookie_secure: bool = False
    cookie_max_age: int = 7 * 24 * 60 * 60
    max_body_bytes: int = 32 * 1024 * 1024
    http_timeout: float = 300.0
    sweep_interval: float = 15.0
    # Connection retries while a Ready pod is still binding its port.
    upstream_attempts: int = 3
    upstream_retry_delay: float = 1.0
    # Optional identity header (e.g. "X-Forwarded-User") set by an authenticating
    # reverse proxy such as oauth2-proxy. When configured, requests carrying a
    # valid user in this header are treated as authenticated (SSO); unset (None)
    # disables SSO entirely and the header is never trusted.
    auth_proxy_header: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            namespace=os.environ.get("NAMESPACE", "dsh-demo"),
            pool=os.environ.get("POOL", "dsh-pool"),
            idle_timeout=_env_int("IDLE_TIMEOUT", 60),
            wake_timeout=_env_int("WAKE_TIMEOUT", 120),
            dsh_port=_env_int("DSH_PORT", 3080),
            dsh_container=os.environ.get("DSH_CONTAINER", "dsh-web"),
            dsh_log_tail_lines=_env_int("DSH_LOG_TAIL_LINES", 2000),
            cookie_name=os.environ.get("COOKIE_NAME", "dsh_gw_session"),
            cookie_secure=_env_bool("COOKIE_SECURE", False),
            cookie_max_age=_env_int("COOKIE_MAX_AGE", 7 * 24 * 60 * 60),
            max_body_bytes=_env_int("MAX_BODY_MB", 32) * 1024 * 1024,
            http_timeout=_env_float("HTTP_TIMEOUT", 300.0),
            sweep_interval=_env_float("SWEEP_INTERVAL", 15.0),
            auth_proxy_header=os.environ.get("AUTH_PROXY_HEADER") or None,
        )


# --- Kubernetes adapter ----------------------------------------------------
#
# The official Kubernetes client is synchronous.  Every call is pushed onto a
# worker thread with anyio.to_thread so it never blocks the event loop.  The
# client and its credentials are loaded lazily, on the first real call: that
# keeps ``GET /healthz`` (and importing this module) cluster-free.


class Kubernetes:
    """Thin async wrapper over the sync CustomObjects/CoreV1 API clients."""

    def __init__(self, namespace: str = "dsh-demo"):
        self.namespace = namespace
        self._crd: k8s_client.CustomObjectsApi | None = None
        self._core: k8s_client.CoreV1Api | None = None
        # anyio.to_thread runs _call() on a worker pool, so the lazy init can be
        # entered concurrently. Serialise it: without the lock one thread can
        # observe _crd set while _core is still None and return a None client.
        self._clients_lock = threading.Lock()

    def _clients(self):
        if self._crd is None or self._core is None:
            with self._clients_lock:
                if self._crd is None or self._core is None:
                    try:
                        k8s_config.load_incluster_config()
                    except k8s_config.ConfigException:
                        k8s_config.load_kube_config()
                    self._crd = k8s_client.CustomObjectsApi()
                    self._core = k8s_client.CoreV1Api()
        return self._crd, self._core

    async def _call(self, which: str, method: str, *args, **kwargs):
        def op():
            crd, core = self._clients()
            target = crd if which == "crd" else core
            return getattr(target, method)(*args, **kwargs)

        return await anyio.to_thread.run_sync(op)

    # -- claims ------------------------------------------------------------

    async def get_claim(self, user: str) -> dict | None:
        try:
            return await self._call(
                "crd",
                "get_namespaced_custom_object",
                GROUP,
                VERSION,
                self.namespace,
                "sandboxclaims",
                claim_name(user),
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    async def create_claim(self, user: str, token_hash: str, pool: str) -> None:
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "SandboxClaim",
            "metadata": {
                "name": claim_name(user),
                "annotations": {
                    TOKEN_ANNOTATION: token_hash,
                    MANAGED_ANNOTATION: MANAGED_BY,
                },
            },
            "spec": {
                "warmPoolRef": {"name": pool},
                "additionalPodMetadata": {"labels": {USER_LABEL: user}},
            },
        }
        try:
            await self._call(
                "crd",
                "create_namespaced_custom_object",
                GROUP,
                VERSION,
                self.namespace,
                "sandboxclaims",
                body,
            )
        except ApiException as exc:
            if exc.status == 409:
                raise UserExists(user) from exc
            raise

    async def delete_claim(self, user: str) -> None:
        try:
            await self._call(
                "crd",
                "delete_namespaced_custom_object",
                GROUP,
                VERSION,
                self.namespace,
                "sandboxclaims",
                claim_name(user),
            )
        except ApiException as exc:
            if exc.status != 404:  # already gone => idempotent success
                raise

    async def list_claims(self) -> list[dict]:
        result = await self._call(
            "crd",
            "list_namespaced_custom_object",
            GROUP,
            VERSION,
            self.namespace,
            "sandboxclaims",
        )
        return result.get("items", [])

    # -- sandboxes ---------------------------------------------------------

    async def get_sandbox(self, name: str) -> dict | None:
        try:
            return await self._call(
                "crd",
                "get_namespaced_custom_object",
                CORE_GROUP,
                VERSION,
                self.namespace,
                "sandboxes",
                name,
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    async def patch_operating_mode(self, name: str, mode: str) -> None:
        await self._call(
            "crd",
            "patch_namespaced_custom_object",
            CORE_GROUP,
            VERSION,
            self.namespace,
            "sandboxes",
            name,
            {"spec": {"operatingMode": mode}},
        )

    async def patch_token(self, user: str, token_hash: str) -> None:
        await self._call(
            "crd",
            "patch_namespaced_custom_object",
            GROUP,
            VERSION,
            self.namespace,
            "sandboxclaims",
            claim_name(user),
            {"metadata": {"annotations": {TOKEN_ANNOTATION: token_hash}}},
        )

    # -- pods and logs -----------------------------------------------------

    async def get_pod(self, name: str) -> dict | None:
        try:
            pod = await self._call(
                "core", "read_namespaced_pod", name, self.namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        # CoreV1Api returns a V1Pod model while every other call in this module
        # (CRD calls, fakes in tests) yields a dict. Normalise at the boundary
        # so callers can keep using ``pod.get(...)``.
        return k8s_client.ApiClient().sanitize_for_serialization(pod)

    async def read_pod_logs(
        self, name: str, container: str, tail_lines: int
    ) -> str:
        result = await self._call(
            "core",
            "read_namespaced_pod_log",
            name,
            self.namespace,
            container=container,
            tail_lines=tail_lines,
        )
        return result or ""


# --- Gateway ---------------------------------------------------------------


@dataclass
class GatewayState:
    """In-memory, per-process state (single replica by design)."""

    last_activity: dict[str, float] = field(default_factory=dict)
    in_flight: dict[str, int] = field(default_factory=dict)
    maintenance: dict[str, bool] = field(default_factory=dict)
    # (user, pod UID, browser Host) -> "dsh-auth-a=...; dsh-auth-b=..."
    handshake: dict[tuple[str, str, str], str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Gateway:
    """Route handlers plus the state machine they share."""

    def __init__(
        self,
        settings: Settings,
        k8s,
        http_client: httpx.AsyncClient,
        ws_connect,
    ):
        self.settings = settings
        self.k8s = k8s
        self.http = http_client
        self.ws_connect = ws_connect
        self.state = GatewayState()

    # -- credentials -------------------------------------------------------

    def credentials(self, headers, cookies) -> tuple[str | None, str | None]:
        """Return ``(token, cookie_user)``; bearer wins over the session cookie."""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip(), None
        raw = cookies.get(self.settings.cookie_name)
        if raw:
            user, sep, token = raw.partition(":")
            if sep and user and token:
                return token, user
        return None, None

    def remote_user(self, headers) -> str | None:
        """The user asserted by a trusted reverse proxy, when SSO is enabled.

        Honored only when ``auth_proxy_header`` is configured, and only when the
        value is a valid dns-1123 label — a spoofed or malformed value is never
        trusted.
        """
        name = self.settings.auth_proxy_header
        if not name:
            return None
        raw = (headers.get(name) or "").strip()
        return raw if valid_user(raw) else None

    def _authorized(
        self,
        claim: dict,
        token: str | None,
        cookie_user: str | None,
        user: str,
        remote: str | None = None,
    ) -> bool:
        if remote == user:
            return True  # identity asserted by the authenticating proxy
        if cookie_user is not None and cookie_user != user:
            return False  # a session for another user never authorizes this one
        return token_matches(claim, token)

    async def _sso_session(
        self, remote: str, headers, cookies, response: Response
    ) -> dict | None:
        """Provision ``remote`` idempotently and mint/refresh a session token.

        The authenticating proxy has already verified ``remote``, so it is
        trusted. A brand-new user is provisioned (claim created, token minted);
        an existing user whose session cookie is missing or no longer matches
        gets a rotated token so a fresh cookie can be issued. A still-valid
        session is reused untouched. Provisioning is non-blocking: the sandbox
        is awaited by the proxy path (``_resolve_sandbox``), so the landing page
        can report "Provisioning" instead of stalling for ``WAKE_TIMEOUT``.
        """
        claim = await self.k8s.get_claim(remote)
        token, cookie_user = self.credentials(headers, cookies)
        if claim is not None and cookie_user == remote and token_matches(claim, token):
            return claim
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        is_new = claim is None
        if is_new:
            try:
                await self.k8s.create_claim(remote, token_hash, self.settings.pool)
            except UserExists:
                # Lost a concurrent signup race: rotate the existing token.
                is_new = False
                await self.k8s.patch_token(remote, token_hash)
            else:
                await self._touch(remote)
        else:
            await self.k8s.patch_token(remote, token_hash)
            await self._touch(remote)
        self.set_session(response, remote, token)
        return await self.k8s.get_claim(remote) if is_new else claim

    @staticmethod
    def _mutation_from_cookie(headers, method: str, cookie_user: str | None) -> bool:
        return (
            cookie_user is not None
            and method.upper() not in SAFE_METHODS
            and not same_origin(headers)
        )

    def set_session(self, response: Response, user: str, token: str) -> None:
        response.set_cookie(
            self.settings.cookie_name,
            f"{user}:{token}",
            max_age=self.settings.cookie_max_age,
            path="/",
            httponly=True,
            samesite="lax",
            secure=self.settings.cookie_secure,
        )

    # -- activity / maintenance -------------------------------------------

    async def _touch(self, user: str) -> None:
        async with self.state.lock:
            self.state.last_activity[user] = time.time()

    async def _acquire(self, user: str) -> bool:
        """Reserve an in-flight slot, waiting out a concurrent suspend."""
        deadline = time.monotonic() + self.settings.wake_timeout
        while True:
            async with self.state.lock:
                if not self.state.maintenance.get(user):
                    self.state.in_flight[user] = (
                        self.state.in_flight.get(user, 0) + 1
                    )
                    self.state.last_activity[user] = time.time()
                    return True
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(0.1)

    async def _release(self, user: str) -> None:
        async with self.state.lock:
            self.state.in_flight[user] = max(
                self.state.in_flight.get(user, 1) - 1, 0
            )
            self.state.last_activity[user] = time.time()

    # -- sandbox / handshake helpers --------------------------------------

    async def _sandbox_of(self, claim: dict) -> dict | None:
        name = (claim.get("status") or {}).get("sandbox", {}).get("name")
        if not name:
            return None
        return await self.k8s.get_sandbox(name)

    async def wait_ready(self, user: str, timeout: int) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            claim = await self.k8s.get_claim(user)
            sandbox = await self._sandbox_of(claim) if claim else None
            if sandbox is not None and derive_state(sandbox) == "Ready":
                return sandbox
            await asyncio.sleep(1)
        return None

    async def _resolve_sandbox(self, user: str, claim: dict) -> tuple[dict, str]:
        sandbox = await self._sandbox_of(claim)
        if sandbox is None:
            raise SandboxUnavailable("sandbox not provisioned yet")
        if derive_state(sandbox) != "Ready":
            # Wake-on-connect: safe in ANY state, including mid-suspension.
            self._clear_handshake(user)
            try:
                await self.k8s.patch_operating_mode(
                    sandbox["metadata"]["name"], "Running"
                )
            except Exception as exc:
                raise SandboxUnavailable("could not wake the sandbox") from exc
            sandbox = await self.wait_ready(user, self.settings.wake_timeout)
            if sandbox is None:
                raise SandboxUnavailable("DSH is waking up, retry shortly")
            await self._touch(user)
        fqdn = (sandbox.get("status") or {}).get("serviceFQDN")
        if not fqdn:
            raise SandboxUnavailable("sandbox has no service FQDN yet")
        return sandbox, fqdn

    def _pod_name(self, sandbox: dict) -> str:
        annotations = sandbox.get("metadata", {}).get("annotations") or {}
        return (
            annotations.get(POD_NAME_ANNOTATION)
            or sandbox["metadata"]["name"]
        )

    @staticmethod
    def _pod_ip(sandbox: dict) -> str | None:
        """The pod IP is the authority DSH accepts in its Host fence."""
        for entry in (sandbox.get("status") or {}).get("podIPs") or []:
            ip = entry.get("ip") if isinstance(entry, dict) else entry
            if ip:
                return ip
        return None

    def _clear_handshake(self, user: str) -> None:
        for key in [k for k in self.state.handshake if k[0] == user]:
            self.state.handshake.pop(key, None)

    async def dsh_cookie(
        self,
        user: str,
        sandbox: dict,
        fqdn: str,
        host: str,
        force: bool = False,
    ) -> tuple[str, tuple[str, str, str], bool]:
        """Return ``(cookie, cache_key, was_cached)``, handshaking on a miss."""
        pod_name = self._pod_name(sandbox)
        try:
            pod = await self.k8s.get_pod(pod_name)
        except ApiException as exc:
            raise HandshakeError(f"could not read pod {pod_name}: {exc}") from exc
        if pod is None:
            raise HandshakeError(f"pod {pod_name} not found")
        uid = (pod.get("metadata") or {}).get("uid") or pod_name
        key = (user, uid, host)
        if not force:
            cached = self.state.handshake.get(key)
            if cached:
                return cached, key, True
        try:
            logs = await self.k8s.read_pod_logs(
                pod_name,
                self.settings.dsh_container,
                self.settings.dsh_log_tail_lines,
            )
        except ApiException as exc:
            raise HandshakeError(
                f"could not read logs for {pod_name}: {exc}"
            ) from exc
        launch_token = parse_dsh_launch_token(logs)
        if not launch_token:
            raise HandshakeError("DSH launch token not found in pod logs")
        url = (
            f"http://{fqdn}:{self.settings.dsh_port}/"
            f"?token={quote(launch_token, safe='')}"
        )
        headers = {
            "Host": host,
            "X-Forwarded-Host": host,
            "X-Forwarded-Proto": "http",
        }
        try:
            response = await self.http.get(
                url,
                headers=headers,
                follow_redirects=False,
                timeout=self.settings.http_timeout,
            )
        except httpx.HTTPError as exc:
            raise HandshakeError(f"DSH handshake failed: {exc}") from exc
        if response.status_code >= 400:
            raise HandshakeError(
                f"DSH handshake returned {response.status_code}"
            )
        cookie = extract_dsh_cookies(response.headers.get_list("set-cookie"))
        if not cookie:
            raise HandshakeError("DSH handshake set no dsh-auth cookie")
        self.state.handshake[key] = cookie
        return cookie, key, False

    # -- landing page / health --------------------------------------------

    async def redirect_to_workspace(self, request: Request) -> Response:
        """``/u/<user>`` -> ``/u/<user>/``; DSH's relative URLs need the slash."""
        user = request.path_params["user"]
        if not valid_user(user):
            return self._error(404, "unknown user")
        target = f"/u/{user}/"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(target, status_code=308)

    async def healthz(self, request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @staticmethod
    def _sso_page(user: str, state: str) -> str:
        """Minimal SSO landing: connect to the sandbox, or offer to recreate it.

        No signup/token forms — the authenticating proxy has already identified
        the user. A "Provisioning" sandbox refreshes itself until a pod exists;
        any other state auto-connects to ``/u/<user>/`` and offers the single
        "Recreate sandbox" action.
        """
        safe_user = html.escape(user)
        path = quote(user)
        if state == "Provisioning":
            status = "Provisioning"
            body = (
                '<meta http-equiv="refresh" content="3">'
                "<p>Provisioning your sandbox… this can take a minute.</p>"
            )
        else:
            status = html.escape(state)
            confirm = (
                'onsubmit="return confirm(\'Delete this sandbox and start fresh? '
                "Your workspace data will be wiped.')\""
            )
            body = (
                f'<meta http-equiv="refresh" content="1;url=/u/{path}/">'
                "<p>Connecting to your sandbox…</p>"
                f'<form method="post" action="/recreate" {confirm}>'
                '<button type="submit">Recreate sandbox</button></form>'
            )
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>DSH sandbox</title><style>"
            "body{font-family:system-ui,sans-serif;max-width:42rem;margin:3rem auto;"
            "padding:0 1rem;line-height:1.5}"
            "form{margin:1rem 0}button{padding:.4rem 1rem}"
            "</style></head><body>"
            "<h1>DeepSeek Harness sandbox</h1>"
            f"<p>Signed in as <strong>{safe_user}</strong>.</p>"
            f"<p>Sandbox status: <strong>{status}</strong>.</p>"
            f"{body}"
            "</body></html>"
        )

    async def landing(self, request: Request) -> Response:
        remote = self.remote_user(request.headers)
        if remote:
            # SSO: auto-provision + connect. A temporary response captures the
            # session cookie minted by _sso_session; the real page is rendered
            # once the sandbox state is known.
            response = HTMLResponse()
            claim = await self._sso_session(
                remote, request.headers, request.cookies, response
            )
            sandbox = await self._sandbox_of(claim) if claim else None
            page = HTMLResponse(self._sso_page(remote, derive_state(sandbox)))
            if "set-cookie" in response.headers:
                page.headers["set-cookie"] = response.headers["set-cookie"]
            return page
        token, user = self.credentials(request.headers, request.cookies)
        session = ""
        if user and token:
            safe_user = html.escape(user)
            session = (
                f'<p class="session">Signed in as <strong>{safe_user}</strong>. '
                f'<a href="/u/{quote(user)}/">Open workspace</a></p>'
                '<form method="post" action="/logout">'
                '<button type="submit">Sign out</button></form>'
            )
        body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DSH sandbox gateway</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 42rem;
         margin: 3rem auto; padding: 0 1rem; line-height: 1.5; }}
  form {{ display: flex; gap: .5rem; margin: .5rem 0 1.5rem; }}
  input {{ flex: 1; padding: .4rem; }}
  button {{ padding: .4rem 1rem; }}
  code {{ background: #eee; padding: .1rem .3rem; }}
</style>
</head>
<body>
<h1>DeepSeek Harness sandboxes</h1>
{session}
<section>
<h2>Create a sandbox</h2>
<form method="post" action="/users">
  <input name="user" placeholder="username (dns-1123 label)" required>
  <button type="submit">Create</button>
</form>
</section>
<section>
<h2>Sign in with a token</h2>
<form method="post" action="/login">
  <input name="user" placeholder="username" required>
  <input name="token" placeholder="bearer token" required>
  <button type="submit">Sign in</button>
</form>
</section>
<p>Automation can use <code>Authorization: Bearer &lt;token&gt;</code>
against the JSON API. The browser session is an HttpOnly cookie.</p>
</body>
</html>"""
        return HTMLResponse(body)

    # -- JSON helpers ------------------------------------------------------

    @staticmethod
    def _error(status_code: int, message: str, headers=None) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status_code, headers=headers)

    @staticmethod
    def _wants_form(request: Request) -> bool:
        ctype = request.headers.get("content-type", "").lower()
        return ctype.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        )

    async def _read_payload(self, request: Request) -> dict:
        if request.headers.get("content-type", "").lower().startswith(
            "application/json"
        ):
            data = await self._read_body(request)
            try:
                payload = json.loads(data)
            except ValueError:
                payload = None
            return payload if isinstance(payload, dict) else {}
        form = await request.form()
        return {key: form.get(key) for key in form}

    async def _read_body(self, request: Request) -> bytes:
        limit = self.settings.max_body_bytes
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > limit:
                    raise BodyTooLarge
            except ValueError:
                pass
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise BodyTooLarge
            chunks.append(chunk)
        return b"".join(chunks)

    # -- users -------------------------------------------------------------

    async def signup(self, request: Request) -> Response:
        wants_form = self._wants_form(request)
        try:
            payload = await self._read_payload(request)
        except BodyTooLarge:
            return self._error(413, "request body too large")
        user = payload.get("user", "")
        if not valid_user(user):
            return self._error(
                400, "body must be {'user': '<dns-1123 label>'}"
            )
        if await self.k8s.get_claim(user) is not None:
            return self._error(409, "user exists")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        try:
            await self.k8s.create_claim(user, token_hash, self.settings.pool)
        except UserExists:
            return self._error(409, "user exists")
        await self._touch(user)
        sandbox = await self.wait_ready(user, self.settings.wake_timeout)
        state = derive_state(sandbox)
        sandbox_name = (sandbox or {}).get("metadata", {}).get("name")
        if wants_form:
            response = RedirectResponse(f"/u/{user}/", status_code=303)
            self.set_session(response, user, token)
            return response
        response = JSONResponse(
            {"user": user, "token": token, "state": state, "sandbox": sandbox_name},
            status_code=201 if state == "Ready" else 202,
        )
        self.set_session(response, user, token)
        return response

    async def login(self, request: Request) -> Response:
        wants_form = self._wants_form(request)
        try:
            payload = await self._read_payload(request)
        except BodyTooLarge:
            return self._error(413, "request body too large")
        user = payload.get("user", "")
        token = payload.get("token", "")
        if not isinstance(user, str) or not isinstance(token, str):
            return self._error(400, "user and token are required")
        claim = await self.k8s.get_claim(user)
        if claim is None:
            return self._error(404, "not found")
        if not token_matches(claim, token):
            return self._error(401, "unauthorized")
        await self._touch(user)
        if wants_form:
            response = RedirectResponse(f"/u/{user}/", status_code=303)
        else:
            response = JSONResponse({"user": user, "state": "ok"})
        self.set_session(response, user, token)
        return response

    async def logout(self, request: Request) -> Response:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(self.settings.cookie_name, path="/")
        return response

    async def get_user(self, request: Request) -> Response:
        user = request.path_params["user"]
        if not valid_user(user):
            return self._error(404, "not found")
        claim = await self.k8s.get_claim(user)
        if claim is None:
            return self._error(404, "not found")
        token, cookie_user = self.credentials(request.headers, request.cookies)
        if not self._authorized(
            claim, token, cookie_user, user, self.remote_user(request.headers)
        ):
            return self._error(401, "unauthorized")
        sandbox = await self._sandbox_of(claim)
        return JSONResponse(
            {
                "user": user,
                "state": derive_state(sandbox),
                "sandbox": (sandbox or {}).get("metadata", {}).get("name"),
            }
        )

    async def delete_user(self, request: Request) -> Response:
        user = request.path_params["user"]
        if not valid_user(user):
            return self._error(404, "not found")
        claim = await self.k8s.get_claim(user)
        if claim is None:
            return self._error(404, "not found")
        token, cookie_user = self.credentials(request.headers, request.cookies)
        if not self._authorized(
            claim, token, cookie_user, user, self.remote_user(request.headers)
        ):
            return self._error(401, "unauthorized")
        if self._mutation_from_cookie(request.headers, "DELETE", cookie_user):
            return self._error(403, "cross-origin request rejected")
        await self.k8s.delete_claim(user)
        self._clear_handshake(user)
        async with self.state.lock:
            self.state.last_activity.pop(user, None)
            self.state.in_flight.pop(user, None)
        return JSONResponse(
            {"user": user, "note": "claim deleted; sandbox and PVC cascade"}
        )

    async def recreate(self, request: Request) -> Response:
        """Delete and re-provision the SSO user's sandbox for a clean start.

        SSO-only: the identity comes from the trusted proxy header. Deleting the
        claim cascades to the sandbox pod and its PVC, wiping user state; we poll
        until the claim is fully gone (finalizers) before recreating to avoid a
        name-collision 409, then reconnect.
        """
        remote = self.remote_user(request.headers)
        if not remote:
            return self._error(401, "unauthorized")
        _token, cookie_user = self.credentials(request.headers, request.cookies)
        if cookie_user is not None and cookie_user != remote:
            return self._error(401, "unauthorized")
        if self._mutation_from_cookie(request.headers, "POST", cookie_user):
            return self._error(403, "cross-origin request rejected")
        await self.k8s.delete_claim(remote)
        self._clear_handshake(remote)
        async with self.state.lock:
            self.state.last_activity.pop(remote, None)
            self.state.in_flight.pop(remote, None)
        # Wait out any finalizer so the claim name is reusable.
        for _ in range(30):
            if await self.k8s.get_claim(remote) is None:
                break
            await asyncio.sleep(1)
        response = RedirectResponse(f"/u/{remote}/", status_code=303)
        await self._sso_session(
            remote, request.headers, request.cookies, response
        )
        return response

    # -- HTTP proxy --------------------------------------------------------

    async def proxy_http(self, request: Request) -> Response:
        user = request.path_params["user"]
        if not valid_user(user):
            return self._error(404, "unknown user")
        remote = self.remote_user(request.headers)
        claim = await self.k8s.get_claim(user)
        if claim is None:
            if remote == user:
                # SSO first hit straight at a deep link: let landing bootstrap.
                return RedirectResponse("/", status_code=303)
            return self._error(404, "unknown user")
        token, cookie_user = self.credentials(request.headers, request.cookies)
        if remote == user and not (
            cookie_user == user and token_matches(claim, token)
        ):
            # SSO user without a valid session cookie: bootstrap, then continue.
            return RedirectResponse("/", status_code=303)
        if not self._authorized(claim, token, cookie_user, user, remote):
            return self._error(401, "unauthorized")
        if self._mutation_from_cookie(request.headers, request.method, cookie_user):
            return self._error(403, "cross-origin request rejected")

        if not await self._acquire(user):
            return self._error(
                503, "gateway busy, retry shortly", {"Retry-After": "5"}
            )
        streaming = False
        try:
            sandbox, fqdn = await self._resolve_sandbox(user, claim)
            host = request.headers.get("host", "")
            cookie, _, was_cached = await self.dsh_cookie(
                user, sandbox, fqdn, host
            )
            body = await self._read_body(request)

            upstream = await self._send_upstream(
                request, user, fqdn, cookie, body
            )
            if upstream.status_code == 401 and was_cached:
                # The cached DSH cookie expired (pod restart, logout, ...):
                # handshake once more and replay the (already buffered) request.
                await upstream.aclose()
                cookie, _, _ = await self.dsh_cookie(
                    user, sandbox, fqdn, host, force=True
                )
                upstream = await self._send_upstream(
                    request, user, fqdn, cookie, body
                )

            response = StreamingResponse(
                self._relay(user, upstream),
                status_code=upstream.status_code,
                headers=self._response_headers(upstream, user),
            )
            streaming = True
            return response
        except BodyTooLarge:
            return self._error(413, "request body too large")
        except (HandshakeError, SandboxUnavailable) as exc:
            return self._error(
                503,
                f"{exc}; retry shortly",
                {"Retry-After": "10"},
            )
        except UpstreamError:
            return self._error(502, "upstream unavailable")
        finally:
            if not streaming:
                await self._release(user)

    async def _send_upstream(
        self,
        request: Request,
        user: str,
        fqdn: str,
        cookie: str,
        body: bytes,
    ) -> httpx.Response:
        path = request.path_params.get("path", "")
        url = f"http://{fqdn}:{self.settings.dsh_port}/{path}"
        query = filtered_query(request.url.query)
        if query:
            url = f"{url}?{query}"

        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in OWNED_REQUEST_HEADERS
            and key.lower() != "content-length"
        }
        host = request.headers.get("host", "")
        headers["host"] = host
        headers["cookie"] = cookie  # only the cached DSH auth cookie
        headers["x-forwarded-host"] = host
        headers["x-forwarded-proto"] = request.url.scheme
        if request.client:
            headers["x-forwarded-for"] = request.client.host
        else:
            headers["x-forwarded-for"] = ""
        port = host.rsplit(":", 1)[1] if ":" in host else ""
        headers["x-forwarded-port"] = port or (
            "443" if request.url.scheme == "https" else "80"
        )

        last_error: Exception | None = None
        for attempt in range(self.settings.upstream_attempts):
            try:
                upstream_request = self.http.build_request(
                    request.method,
                    url,
                    headers=headers,
                    content=body,
                    timeout=self._stream_timeout(),
                )
                return await self.http.send(upstream_request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Retry only failures that cannot have reached the upstream;
                # replaying a timed-out POST could duplicate a mutation.
                last_error = exc
                if attempt + 1 < self.settings.upstream_attempts:
                    await asyncio.sleep(self.settings.upstream_retry_delay)
            except httpx.HTTPError as exc:
                raise UpstreamError("upstream request failed") from exc
        raise UpstreamError("upstream unavailable") from last_error

    def _stream_timeout(self) -> httpx.Timeout:
        """Proxied streams (SSE, long responses) must not hit a read timeout.

        An idle event stream would otherwise raise ``httpx.ReadTimeout`` every
        ``HTTP_TIMEOUT`` seconds and cut the browser's connection.  The relay
        is cancelled when the browser disconnects and ``_relay`` closes the
        upstream response, so an unbounded read cannot outlive the client.
        """
        return httpx.Timeout(
            connect=self.settings.http_timeout,
            read=None,
            write=self.settings.http_timeout,
            pool=self.settings.http_timeout,
        )

    @staticmethod
    def _response_headers(upstream: httpx.Response, user: str):
        headers = []
        for key, value in upstream.headers.multi_items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP or lowered == "set-cookie":
                continue  # never leak upstream cookies to the browser
            if lowered == "location":
                value = rewrite_location(value, user)
            headers.append((key, value))
        return Headers(raw=[(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers])

    async def _relay(self, user: str, upstream: httpx.Response):
        try:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            except httpx.HTTPError as exc:
                # The upstream stream ended abnormally (reset, network change,
                # deadline). The browser reconnects; one traceback per idle SSE
                # stream is noise, so end the response cleanly instead.
                print(f"stream for {user} ended early: {exc!r}", flush=True)
                return
        finally:
            with contextlib.suppress(Exception):
                await upstream.aclose()
            await self._release(user)

    # -- WebSocket proxy ---------------------------------------------------

    async def _ws_reject(self, websocket: WebSocket, code: int) -> None:
        # A close code can only be delivered after the upgrade is accepted;
        # before accept Starlette would turn this into an HTTP 403.
        with contextlib.suppress(Exception):
            await websocket.accept()
        with contextlib.suppress(Exception):
            await websocket.close(code=code)

    async def proxy_ws(self, websocket: WebSocket) -> None:
        user = websocket.path_params["user"]
        if not valid_user(user):
            return await self._ws_reject(websocket, 4401)
        claim = await self.k8s.get_claim(user)
        if claim is None:
            return await self._ws_reject(websocket, 4401)
        token, cookie_user = self.credentials(websocket.headers, websocket.cookies)
        if not self._authorized(
            claim, token, cookie_user, user, self.remote_user(websocket.headers)
        ):
            return await self._ws_reject(websocket, 4401)
        if cookie_user is not None and not same_origin(websocket.headers):
            return await self._ws_reject(websocket, 4401)

        if not await self._acquire(user):
            return await self._ws_reject(websocket, 1011)
        upstream_cm = None
        try:
            try:
                sandbox, fqdn = await self._resolve_sandbox(user, claim)
                host = websocket.headers.get("host", "")
                path = websocket.path_params.get("path", "")
                # DSH's Host fence accepts its own bind address and binds the
                # auth cookie to the request authority. websockets derives the
                # Host header from the URI and cannot be overridden, so dial the
                # pod IP (the bind address) and handshake under that same
                # authority - a cookie minted for the browser authority would be
                # rejected (401). Forward no Origin either: CSWSH is already
                # rejected by same_origin() above.
                upstream_host = self._pod_ip(sandbox) or fqdn
                if ":" in upstream_host and not upstream_host.startswith("["):
                    upstream_host = f"[{upstream_host}]"  # IPv6 literal
                authority = f"{upstream_host}:{self.settings.dsh_port}"
                cookie, _, _ = await self.dsh_cookie(
                    user, sandbox, fqdn, authority
                )
                url = f"ws://{authority}/{path}"
                query = filtered_query(websocket.url.query)
                if query:
                    url = f"{url}?{query}"
                headers = {
                    key: value
                    for key, value in websocket.headers.items()
                    if key.lower() not in OWNED_REQUEST_HEADERS
                    and key.lower() != "origin"
                    and not key.lower().startswith("sec-websocket-")
                }
                headers["cookie"] = cookie
                headers["x-forwarded-host"] = host
                headers["x-forwarded-proto"] = websocket.url.scheme
                if websocket.client:
                    headers["x-forwarded-for"] = websocket.client.host
                subprotocols = [
                    part.strip()
                    for part in websocket.headers.get(
                        "sec-websocket-protocol", ""
                    ).split(",")
                    if part.strip()
                ]
                upstream_cm = self.ws_connect(
                    url,
                    additional_headers=headers,
                    subprotocols=subprotocols or None,
                    open_timeout=self.settings.wake_timeout,
                    max_size=None,
                    proxy=None,
                )
                upstream = await upstream_cm.__aenter__()
            except Exception:
                return await self._ws_reject(websocket, 1011)

            try:
                await websocket.accept(
                    subprotocol=getattr(upstream, "subprotocol", None)
                )
            except RuntimeError:
                pass
            await self._pump(websocket, upstream)
            with contextlib.suppress(Exception):
                await websocket.close(
                    code=self._upstream_close_code(upstream)
                )
        finally:
            if upstream_cm is not None:
                with contextlib.suppress(Exception):
                    await upstream_cm.__aexit__(None, None, None)
            await self._release(user)

    @staticmethod
    def _upstream_close_code(upstream) -> int:
        code = getattr(upstream, "close_code", None)
        if isinstance(code, int) and 1000 <= code <= 4999 and code not in (1005, 1006):
            return code
        return 1000

    async def _pump(self, websocket: WebSocket, upstream) -> None:
        async def client_to_upstream():
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
            finally:
                with contextlib.suppress(Exception):
                    await upstream.close()

        async def upstream_to_client():
            async for message in upstream:
                if isinstance(message, str):
                    await websocket.send_text(message)
                else:
                    await websocket.send_bytes(message)

        tasks = [
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
        ]
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)

    # -- idle sweeper ------------------------------------------------------

    async def sweep_once(self) -> None:
        claims = await self.k8s.list_claims()
        for claim in claims:
            # Only suspend claims this gateway created.  Everything else in
            # the namespace (resource-layer claims from the README walkthrough,
            # other controllers' objects) is not ours to manage.
            if not gateway_managed(claim):
                continue
            user = claim_user(claim)
            try:
                sandbox = await self._sandbox_of(claim)
            except ApiException:
                continue  # transient/stale claim; retry next cycle
            if sandbox is None or derive_state(sandbox) != "Ready":
                continue
            async with self.state.lock:
                if self.state.maintenance.get(user):
                    continue
                if self.state.in_flight.get(user, 0) > 0:
                    continue  # requests in flight => the user is active
                idle = time.time() - self.state.last_activity.setdefault(
                    user, time.time()
                )
                if idle < self.settings.idle_timeout:
                    continue
                self.state.maintenance[user] = True
            try:
                print(f"suspending {user} (idle {int(idle)}s)", flush=True)
                await self.k8s.patch_operating_mode(
                    sandbox["metadata"]["name"], "Suspended"
                )
                self._clear_handshake(user)
            finally:
                async with self.state.lock:
                    self.state.maintenance[user] = False

    async def sweeper_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.sweep_interval)
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    f"sweeper: transient error, retrying next cycle: {exc}",
                    flush=True,
                )


# --- Application factory ---------------------------------------------------


def _lifespan(gateway: Gateway, close_http_client: httpx.AsyncClient | None = None):
    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(gateway.sweeper_loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if close_http_client is not None:
                with contextlib.suppress(Exception):
                    await close_http_client.aclose()

    return lifespan


def create_app(
    settings: Settings | None = None,
    k8s=None,
    http_client: httpx.AsyncClient | None = None,
    ws_connect=None,
) -> Starlette:
    """Build the ASGI app.  All collaborators are injectable for tests."""
    settings = settings or Settings.from_env()
    k8s = k8s if k8s is not None else Kubernetes(settings.namespace)
    owns_http_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(
            follow_redirects=False, timeout=settings.http_timeout
        )
    gateway = Gateway(
        settings, k8s, http_client, ws_connect or websockets.connect
    )
    routes = [
        Route("/healthz", gateway.healthz),
        Route("/", gateway.landing),
        Route("/users", gateway.signup, methods=["POST"]),
        Route("/login", gateway.login, methods=["POST"]),
        Route("/logout", gateway.logout, methods=["POST"]),
        Route("/recreate", gateway.recreate, methods=["POST"]),
        Route("/users/{user}", gateway.get_user),
        Route("/users/{user}", gateway.delete_user, methods=["DELETE"]),
        Route("/u/{user}", gateway.redirect_to_workspace),
        Route(
            "/u/{user}/{path:path}",
            gateway.proxy_http,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        ),
        WebSocketRoute("/u/{user}/{path:path}", gateway.proxy_ws),
    ]
    app = Starlette(
        routes=routes,
        lifespan=_lifespan(gateway, http_client if owns_http_client else None),
    )
    app.state.gateway = gateway
    return app
