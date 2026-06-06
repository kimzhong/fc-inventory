"""Async FusionCompute VRM REST client.

1:1 port of `fc_client.py` (326L) from v1.0.0, but using
`httpx.AsyncClient` instead of `requests.Session`. The 6x3x3 auto-detect
login matrix, the `"10000022"` version-rejection body check, the
paginated `offset`/`limit` loop, and the per-VM disk/NIC fallback
chain are all preserved verbatim from the original.

The lifespan handler in `app.main` creates a single shared
`AsyncClient` (TLS verify disabled — FC ships with self-signed certs)
and `attach()`s it to every `FCClient` instance so all the background
collection jobs reuse the same connection pool.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Priority-ordered list of API versions to try (mirrors fc_client.py:51).
API_VERSIONS: list[str] = ["v8.0", "v6.5", "v6.3", "v6.1", "v1.0", "v9.0"]

# Numeric body code that means "wrong Accept-header API version".
# Locale-safe: it's a code, not a translated string. Mirrors
# `if "10000022" in body:` at fc_client.py:130.
_VERSION_REJECTION_CODE = "10000022"


def _sha256(text: str) -> str:
    """Hash a string with SHA-256 (FC's default password-encryption)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_protocol(host: str) -> str:
    """Strip `https://` / `http://` and trailing slashes from a host string.

    Mirrors the `host.replace(...)` block at `fc_client.py:22`.
    """
    h = host.strip()
    for prefix in ("https://", "http://"):
        if h.startswith(prefix):
            h = h[len(prefix):]
            break
    return h.strip("/")


class FCClient:
    """Async FusionCompute VRM REST client."""

    def __init__(self, host: str, username: str, password: str, port: int = 7443) -> None:
        host = _strip_protocol(host)
        self.host: str = host
        self.port: int = port
        self.username: str = username
        self.password: str = password
        self.base_url: str = f"https://{host}:{port}"
        self.token: str | None = None
        self.version: str | None = None  # negotiated API version (set on successful login)
        self._client: httpx.AsyncClient | None = None  # attached by lifespan

    # ── Lifecycle ─────────────────────────────────────────

    def attach(self, client: httpx.AsyncClient) -> None:
        """Inject the shared `httpx.AsyncClient` from the FastAPI lifespan.

        The shared client has `verify=False` (FC self-signed certs)
        and pooled connection limits. The token, once obtained, is
        stored on the `X-Auth-Token` header for subsequent requests.
        """
        self._client = client

    def _headers(self, version: str | None = None) -> dict[str, str]:
        h = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": f"application/json;version={version or 'v1.0'};charset=UTF-8",
        }
        if self.token:
            h["X-Auth-Token"] = self.token
        return h

    # ── Login: 6 x 3 x 3 matrix ────────────────────────────

    async def login(self) -> dict[str, Any]:
        """Authenticate to FusionCompute by trying the 6x3x3 matrix.

        Mirrors `FCClient.login` (fc_client.py:40-154). The control
        flow is preserved exactly:
          - outer loop: ports (dedup, in the order [self.port, 7443, 8443])
          - middle loop: API versions in `API_VERSIONS` order
          - inner loop: 3 auth methods
          - on `"10000022"` body -> break inner (next version)
          - on `ConnectError` / `ReadTimeout` -> break both inner+mid (next port)
          - on HTTP 200 -> capture token, set base_url, return
          - on any other failure -> continue to next auth method
        """
        if self._client is None:
            raise RuntimeError("FCClient.attach() must be called before login()")

        auth_methods = self._build_auth_methods()
        ports_to_try = list(dict.fromkeys([self.port, 7443, 8443]))  # fc_client.py:86

        last_error: str | None = None
        for port in ports_to_try:
            url = f"https://{self.host}:{port}/service/session"

            for ver in API_VERSIONS:
                for attempt in auth_methods:
                    label = f"[v={ver}] {attempt['label']} -> {url}"
                    logger.info("login.attempt", attempt=label)

                    headers = self._headers(ver)
                    headers.update(attempt["headers"])

                    try:
                        if attempt["method"] == "POST":
                            resp = await self._client.post(
                                url, headers=headers, json=attempt["json"], timeout=10.0,
                            )
                        else:  # PUT
                            resp = await self._client.put(
                                url, headers=headers, json=attempt["json"], timeout=10.0,
                            )
                    except httpx.ConnectError:
                        logger.warning("login.connect_refused", port=port)
                        last_error = f"Connection refused on port {port}"
                        break  # next port
                    except httpx.ReadTimeout:
                        logger.warning("login.timeout", port=port)
                        last_error = f"Timeout on port {port}"
                        break  # next port
                    except Exception as exc:
                        logger.warning("login.error", err=str(exc))
                        last_error = str(exc)
                        continue

                    body = resp.text[:300]
                    logger.debug("login.response", status=resp.status_code, body_preview=body)

                    if resp.status_code == 200:
                        self.port = port
                        self.version = ver
                        self.base_url = f"https://{self.host}:{port}/service"
                        logger.info(
                            "login.ok",
                            version=ver,
                            port=port,
                            method=attempt["label"],
                            base_url=self.base_url,
                        )
                        return self._extract_token(resp, label)

                    body_for_log = resp.text[:200]
                    logger.warning(
                        "login.failed",
                        status=resp.status_code,
                        method=attempt["label"],
                        body_preview=body_for_log,
                    )
                    last_error = f"HTTP {resp.status_code}: {body_for_log}"

                    # Version-rejection -> skip to next version.
                    if _VERSION_REJECTION_CODE in body:
                        logger.info("login.version_rejected", version=ver)
                        break  # break auth loop; try next version

        raise ConnectionError(
            f"All login methods failed. Last error: {last_error}\n"
            "Please verify: 1) Username/Password is correct  "
            "2) FusionCompute VRM is reachable from this machine"
        )

    def _build_auth_methods(self) -> list[dict[str, Any]]:
        """Build the 3 auth flavours from fc_client.py:54-83."""
        u = self.username
        p = self.password
        return [
            {
                "label": "POST + headers + plain",
                "method": "POST",
                "headers": {
                    "X-Auth-User": u,
                    "X-Auth-Key": p,
                    "X-Auth-UserType": "0",
                    "X-ENCRYPT-ALGORITHM": "1",
                },
                "json": None,
            },
            {
                "label": "POST + headers + SHA256",
                "method": "POST",
                "headers": {
                    "X-Auth-User": u,
                    "X-Auth-Key": _sha256(p),
                    "X-Auth-UserType": "0",
                    "X-ENCRYPT-ALGORITHM": "0",
                },
                "json": None,
            },
            {
                "label": "PUT + JSON body",
                "method": "PUT",
                "headers": {},
                "json": {"userName": u, "password": p},
            },
        ]

    def _extract_token(self, resp: httpx.Response, method_label: str) -> dict[str, Any]:
        """Pull the auth token from the response (header first, then body).

        Mirrors `FCClient._extract_token` (fc_client.py:156-185).
        """
        # Try response header first.
        self.token = resp.headers.get("X-Auth-Token")
        if not self.token:
            # Then the JSON body.
            try:
                data = resp.json()
            except Exception:
                data = {}
            if isinstance(data, dict):
                self.token = (
                    data.get("accessSession")
                    or data.get("token")
                    or data.get("X-Auth-Token")
                )

        if not self.token:
            raise ConnectionError(
                f"Login returned 200 via [{method_label}] but no token found "
                f"in response headers or body."
            )

        logger.info("login.token_captured", via=method_label)
        try:
            return resp.json()  # type: ignore[return-value]
        except Exception:
            return {}

    # ── Logout ────────────────────────────────────────────

    async def logout(self) -> None:
        """End the session. Best-effort; errors are swallowed (fc_client.py:187-194)."""
        if not self.token or self._client is None:
            self.token = None
            return
        url = f"{self.base_url}/session"
        try:
            await self._client.delete(url, headers=self._headers(), timeout=10.0)
        except Exception as exc:
            logger.debug("logout.failed", err=str(exc))
        self.token = None

    # ── Generic GET helpers ───────────────────────────────

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Issue an authenticated GET and return the parsed JSON.

        Mirrors `FCClient._get` (fc_client.py:196-207), with the
        `/service/` URL-stitching fix from the Go rewrite so we
        don't end up with `https://host:port/service/service/...`
        when `path` already starts with `/service/`.
        """
        if self._client is None:
            raise RuntimeError("FCClient.attach() must be called before _get()")
        if path.startswith("/service/"):
            url = f"https://{self.host}:{self.port}{path}"
        else:
            url = f"{self.base_url}{path}"
        logger.debug("fc.get", url=url, params=params)
        resp = await self._client.get(url, params=params, headers=self._headers(), timeout=60.0)
        logger.debug("fc.get.response", status=resp.status_code, bytes=len(resp.content))
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def _get_all(self, path: str, result_key: str) -> list[dict[str, Any]]:
        """Walk the paginated `offset`/`limit` endpoint until the batch is short.

        Mirrors `FCClient._get_all` (fc_client.py:209-230).
        """
        items: list[dict[str, Any]] = []
        offset = 0
        limit = 100
        while True:
            data = await self._get(path, params={"offset": offset, "limit": limit})
            # Top-level list? Use it directly (mirrors the v1.0.0 fallback).
            if isinstance(data, list):
                return data  # type: ignore[return-value]
            # Try expected key, then fallback to common alternatives.
            batch = data.get(result_key)
            if batch is None:
                batch = data.get("items", data.get("result", []))
            if not batch:
                break
            items.extend(batch)
            total = data.get("total", len(items))
            if len(items) >= total:
                break
            offset += limit
        return items

    # ── Site ───────────────────────────────────────────────

    async def get_sites(self) -> list[dict[str, Any]]:
        data = await self._get("/sites")
        return data.get("sites", [])  # type: ignore[return-value]

    # ── Cluster ────────────────────────────────────────────

    async def get_clusters(self, site_uri: str) -> list[dict[str, Any]]:
        return await self._get_all(f"{site_uri}/clusters", "clusters")

    # ── Host ──────────────────────────────────────────────

    async def get_hosts(self, site_uri: str) -> list[dict[str, Any]]:
        return await self._get_all(f"{site_uri}/hosts", "hosts")

    async def get_host_detail(self, host_uri: str) -> dict[str, Any]:
        """host_uri is the full URI from the host list (e.g. `/service/hosts/1`)."""
        return await self._get(host_uri)

    # ── VM ────────────────────────────────────────────────

    async def get_vms(self, site_uri: str) -> list[dict[str, Any]]:
        return await self._get_all(f"{site_uri}/vms", "vms")

    async def get_vm_detail(self, vm_uri: str) -> dict[str, Any]:
        """vm_uri is the full URI from the VM list."""
        return await self._get(vm_uri)

    async def get_vm_nics(
        self, vm_uri: str, inline_nics: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Return NICs for a VM.

        If `inline_nics` (from `vmConfig.nics`) is non-empty, return it
        directly — that's the fast path used by the collector. Otherwise
        fall back to a separate `/nics` GET. Mirrors the inline + fallback
        pattern at fc_client.py:270-273.
        """
        if inline_nics:
            return inline_nics
        data = await self._get(f"{vm_uri}/nics")
        return data.get("nics", data.get("items", []))  # type: ignore[return-value]

    async def get_vm_disks(
        self,
        vm_uri: str,
        inline_disks: list[dict[str, Any]] | None = None,
        inline_volumes: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return disks for a VM with the `disks -> volumes -> /volumes -> /disks`
        fallback chain used by the v1.0.0 collector.
        """
        if inline_disks:
            return inline_disks
        if inline_volumes:
            return inline_volumes
        for sub in ("/volumes", "/disks"):
            try:
                data = await self._get(f"{vm_uri}{sub}")
            except Exception as exc:
                logger.debug("vm.disks.fallback_failed", sub=sub, err=str(exc))
                continue
            key = sub.lstrip("/")
            arr = data.get(key, data.get("items", []))
            if arr:
                return arr  # type: ignore[return-value]
        return []

    # ── Datastore ──────────────────────────────────────────

    async def get_datastores(self, site_uri: str) -> list[dict[str, Any]]:
        return await self._get_all(f"{site_uri}/datastores", "datastores")

    # ── Network ────────────────────────────────────────────

    async def get_dvswitches(self, site_uri: str) -> list[dict[str, Any]]:
        data = await self._get(f"{site_uri}/dvswitchs")
        result = data.get("dvswitchs", data.get("dvSwitchs", data.get("items", [])))
        if not result and isinstance(data, list):
            result = data  # type: ignore[assignment]
        return result  # type: ignore[return-value]

    async def get_portgroups(self, dvswitch_uri: str) -> list[dict[str, Any]]:
        data = await self._get(f"{dvswitch_uri}/portgroups")
        result = data.get("portgroups", data.get("portGroups", data.get("items", [])))
        if not result and isinstance(data, list):
            result = data  # type: ignore[assignment]
        return result  # type: ignore[return-value]

    async def get_site_portgroups(self, site_uri: str) -> list[dict[str, Any]]:
        """Site-level portgroup fallback (mirrors fc_client.py:315-326)."""
        try:
            data = await self._get(f"{site_uri}/portgroups")
        except Exception as exc:
            logger.warning("site_portgroups.failed", err=str(exc))
            return []
        result = data.get("portgroups", data.get("portGroups", data.get("items", [])))
        if not result and isinstance(data, list):
            result = data  # type: ignore[assignment]
        return result  # type: ignore[return-value]
