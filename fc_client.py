"""
FusionCompute REST API Client
Handles authentication and all inventory data retrieval.
"""

import hashlib
import logging

import requests
import urllib3

logger = logging.getLogger(__name__)

# Suppress SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class FCClient:
    """Client for FusionCompute VRM REST API."""

    def __init__(self, host, username, password, port=7443):
        # Strip protocol prefix if user entered it (e.g. https://10.0.0.1)
        host = host.replace("https://", "").replace("http://", "").strip("/")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.base_url = f"https://{host}:{port}"
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json;version=v1.0;charset=UTF-8",
        })
        self.token = None

    def _sha256(self, text):
        """Hash text with SHA-256 (FusionCompute default password encryption)."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def login(self):
        """
        Authenticate to FusionCompute.
        Based on log analysis:
        - Port 7443 + /service/session is the correct endpoint
        - "版本号错误" means the Accept header version is wrong
        - Tries multiple API versions and auth methods automatically
        """
        logger.info(f"Logging in to {self.host} as {self.username}")

        # API versions to try (most likely first)
        versions = ["v8.0", "v6.5", "v6.3", "v6.1", "v1.0", "v9.0"]

        # Auth methods to try
        auth_methods = [
            {
                "label": "POST + headers + plain",
                "method": "POST",
                "headers": {
                    "X-Auth-User": self.username,
                    "X-Auth-Key": self.password,
                    "X-Auth-UserType": "0",
                    "X-ENCRYPT-ALGORITHM": "1",
                },
                "json": None,
            },
            {
                "label": "POST + headers + SHA256",
                "method": "POST",
                "headers": {
                    "X-Auth-User": self.username,
                    "X-Auth-Key": self._sha256(self.password),
                    "X-Auth-UserType": "0",
                    "X-ENCRYPT-ALGORITHM": "0",
                },
                "json": None,
            },
            {
                "label": "PUT + JSON body",
                "method": "PUT",
                "headers": {},
                "json": {"userName": self.username, "password": self.password},
            },
        ]

        # Target: port 7443 + /service/session (confirmed from logs)
        ports_to_try = list(dict.fromkeys([self.port, 7443, 8443]))

        last_error = None
        for port in ports_to_try:
            url = f"https://{self.host}:{port}/service/session"

            for ver in versions:
                for attempt in auth_methods:
                    label = f"[v={ver}] {attempt['label']} -> {url}"
                    logger.info(f"Trying: {label}")

                    # Set version in Accept header for this attempt
                    self.session.headers["Accept"] = \
                        f"application/json;version={ver};charset=UTF-8"

                    try:
                        if attempt["method"] == "POST":
                            resp = self.session.post(
                                url, headers=attempt["headers"],
                                json=attempt["json"], timeout=10,
                            )
                        else:
                            resp = self.session.put(
                                url, headers=attempt["headers"],
                                json=attempt["json"], timeout=10,
                            )

                        logger.debug(f"  -> HTTP {resp.status_code}")
                        try:
                            logger.debug(f"  -> Body: {resp.text[:300]}")
                        except Exception:
                            pass

                        if resp.status_code == 200:
                            self.port = port
                            self.base_url = f"https://{self.host}:{port}/service"
                            logger.info(f"Login OK! version={ver}, base_url={self.base_url}")
                            return self._extract_token(resp, label)

                        body = resp.text[:200]
                        logger.warning(f"  -> Failed ({resp.status_code}): {body}")
                        last_error = f"HTTP {resp.status_code}: {body}"

                        # If version error, skip to next version
                        if "10000022" in body:
                            logger.info(f"  -> Version {ver} rejected, trying next")
                            break

                    except requests.exceptions.ConnectionError:
                        logger.warning(f"  -> Connection refused (port {port})")
                        last_error = f"Connection refused on port {port}"
                        break
                    except requests.exceptions.ReadTimeout:
                        logger.warning(f"  -> Timeout (port {port})")
                        last_error = f"Timeout on port {port}"
                        break
                    except Exception as e:
                        logger.warning(f"  -> Error: {e}")
                        last_error = str(e)
                else:
                    continue
                # If inner loop broke (version error/connection), continue to next version
                continue

        raise ConnectionError(
            f"All login methods failed. Last error: {last_error}\n"
            f"Please verify: 1) Username/Password is correct  "
            f"2) FusionCompute VRM is reachable from this machine"
        )

    def _extract_token(self, resp, method_label):
        """Extract auth token from a successful login response."""
        # Try response header first
        self.token = resp.headers.get("X-Auth-Token")

        # Try response body as fallback
        if not self.token:
            try:
                data = resp.json()
                self.token = (
                    data.get("accessSession")
                    or data.get("token")
                    or data.get("X-Auth-Token")
                )
            except Exception:
                pass

        if not self.token:
            raise ConnectionError(
                f"Login returned 200 via [{method_label}] but no token found "
                f"in response headers or body."
            )

        logger.info(f"Login successful via [{method_label}]")
        self.session.headers["X-Auth-Token"] = self.token

        try:
            return resp.json()
        except Exception:
            return {}

    def logout(self):
        """End the session."""
        try:
            url = f"{self.base_url}/session"
            self.session.delete(url, timeout=10)
        except Exception:
            pass
        self.token = None

    def _get(self, path, params=None):
        """Send GET request and return parsed JSON."""
        # If path already starts with /service, use base host:port only
        if path.startswith("/service/"):
            url = f"https://{self.host}:{self.port}{path}"
        else:
            url = f"{self.base_url}{path}"
        logger.debug(f"GET {url} params={params}")
        resp = self.session.get(url, params=params, timeout=60)
        logger.debug(f"  -> {resp.status_code} ({len(resp.content)} bytes)")
        resp.raise_for_status()
        return resp.json()

    def _get_all(self, path, result_key):
        """Fetch all pages of a paginated list endpoint."""
        items = []
        offset = 0
        limit = 100
        while True:
            data = self._get(path, params={"offset": offset, "limit": limit})
            # Try expected key, fallback to common alternatives
            batch = data.get(result_key)
            if batch is None:
                batch = data.get("items", data.get("result", []))
            if not batch:
                # If top-level is a list, use it directly
                if isinstance(data, list):
                    return data
                break
            items.extend(batch)
            total = data.get("total", len(items))
            if len(items) >= total:
                break
            offset += limit
        return items

    # ── Site ──────────────────────────────────────────────

    def get_sites(self):
        """Get all sites."""
        data = self._get("/sites")
        return data.get("sites", [])

    # ── Cluster ──────────────────────────────────────────

    def get_clusters(self, site_uri):
        """Get all clusters in a site."""
        return self._get_all(f"{site_uri}/clusters", "clusters")

    def get_cluster_resource(self, site_uri, cluster_id):
        """Get compute resource summary of a cluster."""
        data = self._get(f"{site_uri}/clusters/{cluster_id}/compute-resources")
        return data

    # ── Host ─────────────────────────────────────────────

    def get_hosts(self, site_uri):
        """Get all hosts in a site."""
        return self._get_all(f"{site_uri}/hosts", "hosts")

    def get_host_detail(self, host_uri):
        """Get detailed info for a specific host. host_uri = full URI from host list."""
        return self._get(host_uri)

    # ── VM ───────────────────────────────────────────────

    def get_vms(self, site_uri):
        """Get all VMs in a site."""
        return self._get_all(f"{site_uri}/vms", "vms")

    def get_vm_detail(self, vm_uri):
        """Get detailed info for a specific VM. vm_uri = full URI from VM list."""
        return self._get(vm_uri)

    def get_vm_nics(self, vm_uri):
        """Get network interfaces of a VM."""
        data = self._get(f"{vm_uri}/nics")
        return data.get("nics", data.get("items", []))

    def get_vm_disks(self, vm_uri):
        """Get disk volumes of a VM."""
        # Try /volumes first, fallback to /disks
        try:
            data = self._get(f"{vm_uri}/volumes")
            return data.get("volumes", data.get("items", []))
        except Exception:
            try:
                data = self._get(f"{vm_uri}/disks")
                return data.get("disks", data.get("items", []))
            except Exception:
                return []

    # ── Datastore ────────────────────────────────────────

    def get_datastores(self, site_uri):
        """Get all datastores in a site."""
        return self._get_all(f"{site_uri}/datastores", "datastores")

    # ── Network ──────────────────────────────────────────

    def get_dvswitches(self, site_uri):
        """Get all distributed virtual switches."""
        data = self._get(f"{site_uri}/dvswitchs")
        logger.debug(f"DVSwitch response keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict'}")
        result = data.get("dvswitchs", data.get("dvSwitchs", data.get("items", [])))
        if not result and isinstance(data, list):
            result = data
        logger.info(f"Found {len(result)} DVSwitches")
        return result

    def get_portgroups(self, dvswitch_uri):
        """Get port groups of a DVSwitch. dvswitch_uri = full URI from dvswitch list."""
        data = self._get(f"{dvswitch_uri}/portgroups")
        result = data.get("portgroups", data.get("portGroups", data.get("items", [])))
        if not result and isinstance(data, list):
            result = data
        logger.info(f"Found {len(result)} port groups under {dvswitch_uri}")
        return result

    def get_site_portgroups(self, site_uri):
        """Get all port groups in a site (fallback when DVSwitch listing is empty)."""
        try:
            data = self._get(f"{site_uri}/portgroups")
            result = data.get("portgroups", data.get("portGroups", data.get("items", [])))
            if not result and isinstance(data, list):
                result = data
            logger.info(f"Found {len(result)} port groups at site level")
            return result
        except Exception as e:
            logger.warning(f"Site-level portgroup query failed: {e}")
            return []
