"""Dashboard backend for the WristWire tab, mounted at /api/plugins/wristwire/.

Runs inside the Hermes dashboard process, behind the dashboard's login, and talks to the
WristWire relay over loopback with the relay admin key (from plugin settings, the
environment, or the relay's .env, the same lookup the plugin's tools use). Handlers are
plain ``def`` so the blocking HTTP call runs in a worker thread, not on the event loop.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

from fastapi import APIRouter

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGIN_ID = "wristwire"


def _load_relay_client():
    # The dashboard imports this file on its own, outside the plugin package.
    spec = importlib.util.spec_from_file_location("wristwire_relay_client", PLUGIN_DIR / "relay_client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rc = _load_relay_client()
router = APIRouter()


def _get_config(key, default=None):
    try:
        from hermes_cli.config import load_config_readonly

        entries = ((load_config_readonly() or {}).get("plugins", {}) or {}).get("entries", {}) or {}
        return ((entries.get(PLUGIN_ID) or {}).get("settings") or {}).get(key, default)
    except Exception:
        return default


def _client():
    return rc.RelayClient.from_config(_get_config)


@router.get("/status")
def status():
    try:
        client = _client()
    except rc.RelayError as exc:
        return {"relay_reachable": False, "error_kind": exc.kind, "error": str(exc)}
    try:
        data = client.status()
    except rc.RelayError as exc:
        return {
            "relay_reachable": False,
            "error_kind": exc.kind,
            "relay_url": client.base_url,
            "error": str(exc),
        }
    data["relay_reachable"] = True
    data["relay_url"] = client.base_url
    return data


@router.post("/test")
def send_test():
    try:
        result = _client().send(
            f"WristWire test, {time.strftime('%H:%M')}. If this reached your watch, replies work."
        )
    except rc.RelayError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": result.get("status") == "queued_for_watch", "result": result}
