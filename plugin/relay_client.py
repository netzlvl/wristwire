"""Client for the local WristWire relay daemon.

The relay (``wristwire serve`` / ``adb-bridge``) owns the phone link, the queue, the
approval codes and the audit trail. This module only speaks its HTTP API with the admin key,
so nothing here needs the device secret or the SQLite file.

Config resolution order for every setting (first hit wins):
  1. ``plugins.entries.wristwire.settings.<key>`` in ``config.yaml`` (via ``ctx.get_config``)
  2. an environment variable (``GARMIN_RELAY_ADMIN_KEY`` / ``GARMIN_RELAY_URL``)
  3. the relay's own ``.env`` (the clone this plugin lives in, ``~/.config/wristwire/.env``,
     ``~/wristwire/.env``, legacy ``~/.hermes/garmin.env``)
  4. the built-in default (loopback URL only — never a credential)
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_RELAY_URL = "http://127.0.0.1:8788"
# The repo this plugin ships in comes first: plugin/ is usually a symlink into the clone.
_REPO_ENV = str(Path(__file__).resolve().parent.parent / ".env")
_ENV_FILES = (_REPO_ENV, "~/.config/wristwire/.env", "~/wristwire/.env", "~/.hermes/garmin.env")


class RelayError(RuntimeError):
    """Relay unreachable, misconfigured, or returned an error status.

    ``kind`` is ``unreachable``, ``auth`` (missing or rejected admin key) or ``error``.
    """

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


def _read_env_file(name: str) -> Optional[str]:
    """Value of ``name`` from the relay's .env files, if present."""
    for candidate in _ENV_FILES:
        path = Path(candidate).expanduser()
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == name:
                    return value.strip().strip("'\"")
        except OSError:
            continue
    return None


class RelayClient:
    """Thin, dependency-free HTTP client for one relay instance."""

    def __init__(self, base_url: str, admin_key: str, timeout: float = 10.0):
        if not admin_key:
            raise RelayError(
                "No relay admin key. Set plugins.entries.wristwire.settings.admin_key "
                "in config.yaml, or export GARMIN_RELAY_ADMIN_KEY.",
                kind="auth",
            )
        self.base_url = base_url.rstrip("/")
        self._admin_key = admin_key
        self.timeout = timeout

    @classmethod
    def from_config(cls, get_config) -> "RelayClient":
        """Build a client from plugin settings, env vars, and the relay's .env."""
        base_url = (
            get_config("relay_url", default=None)
            or os.environ.get("GARMIN_RELAY_URL")
            or _read_env_file("GARMIN_RELAY_URL")
            or DEFAULT_RELAY_URL
        )
        admin_key = (
            get_config("admin_key", default=None)
            or os.environ.get("GARMIN_RELAY_ADMIN_KEY")
            or _read_env_file("GARMIN_RELAY_ADMIN_KEY")
            or ""
        )
        return cls(str(base_url), str(admin_key))

    def _request(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._admin_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code in (401, 403):
                raise RelayError(
                    "Relay rejected the admin key (HTTP "
                    f"{exc.code}). Check plugins.entries.wristwire.settings.admin_key.",
                    kind="auth",
                ) from None
            raise RelayError(f"Relay returned HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RelayError(
                f"Relay unreachable at {self.base_url} ({exc.reason}). "
                "Is the bridge service running? systemctl --user status wristwire",
                kind="unreachable",
            ) from None
        except json.JSONDecodeError:
            raise RelayError("Relay returned a non-JSON response") from None

    # -- API surface -------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Unauthenticated liveness probe (``GET /health``)."""
        request = urllib.request.Request(f"{self.base_url}/health", method="GET")
        with urllib.request.urlopen(request, timeout=min(self.timeout, 3.0)) as response:
            return json.loads(response.read().decode("utf-8") or "{}")

    def telemetry(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/garmin/telemetry")

    def status(self, reply_id: Optional[str] = None) -> Dict[str, Any]:
        """Telemetry plus per-link health (``GET /v1/garmin/status``); optionally where one reply is."""
        query = f"?reply={urllib.parse.quote(reply_id)}" if reply_id else ""
        return self._request("GET", f"/v1/garmin/status{query}")

    def send(self, message: str, recipient: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"message": message}
        if recipient:
            payload["recipient"] = recipient
        return self._request("POST", "/v1/garmin/send", payload)


_reachability_cache: Tuple[float, bool] = (0.0, False)
_REACHABILITY_TTL_S = 15.0


def relay_reachable(get_config) -> bool:
    """Cheap gate for ``check_fn``: is a relay answering on loopback right now?

    Cached for 15s so a tool-visibility check on every turn does not add a probe per
    message. Never raises.
    """
    global _reachability_cache
    checked_at, value = _reachability_cache
    if time.time() - checked_at < _REACHABILITY_TTL_S:
        return value
    try:
        client = RelayClient.from_config(get_config)
        health = client.health()
        value = str(health.get("status") or "") == "ok"
    except Exception:
        value = False
    _reachability_cache = (time.time(), value)
    return value
