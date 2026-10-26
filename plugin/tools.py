"""Agent-facing tools for the WristWire plugin.

  watch_send    — queue a message to the watch (delivery is asynchronous)
  watch_inbox   — what the watch sent, with run status and the reply that went back
  watch_status  — relay + phone health, queue depth, pending approvals

Every handler returns a JSON string. Sends never claim delivery: the relay owns the outbound
queue, so a send reports the queued ids and the caller polls ``watch_status`` /
``watch_inbox`` for the ack.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from .relay_client import RelayClient, RelayError

logger = logging.getLogger(__name__)

_SEND_KEY = "get_config"


def _schema(name: str, description: str, properties: dict[str, Any], required=None) -> dict[str, Any]:
    params: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    params["additionalProperties"] = False
    return {"name": name, "description": description, "parameters": params}


WATCH_SEND_SCHEMA = _schema(
    "watch_send",
    "Send a short message to the owner's Garmin watch through the local WristWire relay. "
    "Use it to push an alert, a status line, or the answer to a question the owner asked from "
    "the watch. Keep it under ~280 characters — the watch face truncates. Delivery is "
    "asynchronous: this queues the message and returns reply/request ids, it does NOT prove the "
    "watch received it. Check watch_status (failed_outbound_deliveries) or "
    "watch_inbox for the ack before telling the user it arrived.",
    {
        "message": {
            "type": "string",
            "description": "Message text for the watch face. Plain ASCII travels best; emoji and "
            "smart quotes are stripped on the way out.",
        },
        "recipient": {
            "type": "string",
            "description": "Optional Garmin contact/conversation name (defaults to the relay's "
            "GARMIN_OWNER_NAME, normally the owner's own device).",
        },
    },
    required=["message"],
)

WATCH_INBOX_SCHEMA = _schema(
    "watch_inbox",
    "Read the most recent messages the owner sent from the Garmin watch and how each was "
    "handled — policy tier, run status, the reply text that was queued back, token spend and "
    "latency. Use it to see what the watch has asked for, or to confirm a reply actually "
    "landed (status 'delivered' means the phone-side bridge saw its own message on screen).",
    {
        "limit": {
            "type": "integer",
            "description": "How many recent requests to return (default 10, max 50).",
            "minimum": 1,
            "maximum": 50,
        },
        "status": {
            "type": "string",
            "description": "Optional filter: queued | running | needs_approval | completed | "
            "delivered | failed.",
        },
    },
)

WATCH_STATUS_SCHEMA = _schema(
    "watch_status",
    "Health of the Garmin watch channel: whether the relay is up, whether the phone bridge "
    "heartbeat is fresh, how many requests sit in each state, pending approval codes, failed "
    "outbound deliveries, and cumulative token spend/latency. Use it before blaming a missing "
    "message on the model, and to see if a reply is still queued.",
    {
        "include_requests": {
            "type": "boolean",
            "description": "Also include the recent-requests list (default false).",
        }
    },
)


def _client(kwargs: Dict[str, Any]) -> RelayClient:
    get_config = kwargs.get("_plugin_get_config") or kwargs.get("get_config")
    if get_config is None:
        # check_fn / handler wiring always passes it; fail loudly rather than silently defaulting.
        raise RelayError("plugin context missing get_config; cannot resolve relay settings")
    return RelayClient.from_config(get_config)


def _error(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def handle_watch_send(args: Dict[str, Any], **kwargs: Any) -> str:
    message = str(args.get("message") or "").strip()
    if not message:
        return json.dumps({"ok": False, "error": "message is required"})
    recipient = args.get("recipient")
    try:
        result = _client(kwargs).send(message, recipient=str(recipient) if recipient else None)
    except (RelayError, Exception) as exc:  # noqa: BLE001 — handlers must never raise
        return _error(exc)
    status = str(result.get("status") or "")
    if status == "blocked":
        return json.dumps(
            {
                "ok": False,
                "error": "relay kill switch is active; nothing was queued",
                "relay_response": result,
            }
        )
    return json.dumps(
        {
            "ok": status == "queued_for_watch",
            "queued": status == "queued_for_watch",
            "request_id": result.get("request_id"),
            "reply_id": result.get("reply_id"),
            "recipient": result.get("recipient"),
            "reply_text": result.get("reply_text"),
            "note": "Queued for the phone-side bridge. Delivery is confirmed later by the relay "
            "(watch_status -> failed_outbound_deliveries, or watch_inbox status "
            "'delivered'), not by this call.",
        },
        ensure_ascii=False,
    )


def handle_watch_inbox(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 50))
    wanted = str(args.get("status") or "").strip().lower()
    try:
        telemetry = _client(kwargs).telemetry()
    except (RelayError, Exception) as exc:  # noqa: BLE001
        return _error(exc)

    requests = telemetry.get("recent_requests") or []
    if wanted:
        requests = [r for r in requests if str(r.get("status") or "").lower() == wanted]
    trimmed = [
        {
            "request_id": r.get("request_id"),
            "from": r.get("sender_id"),
            "kind": r.get("source_type"),
            "tier": r.get("tier"),
            "status": r.get("status"),
            "message": r.get("message_text_redacted"),
            "reply": r.get("response_text"),
            "tokens": r.get("total_tokens"),
            "latency_ms": r.get("latency_ms"),
        }
        for r in requests[:limit]
    ]
    return json.dumps(
        {
            "ok": True,
            "count": len(trimmed),
            "requests": trimmed,
            "queue_summary": telemetry.get("queue_summary") or {},
            "pending_approvals": telemetry.get("pending_approvals") or [],
        },
        ensure_ascii=False,
    )


def handle_watch_status(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        telemetry = _client(kwargs).telemetry()
    except (RelayError, Exception) as exc:  # noqa: BLE001
        return _error(exc)
    phone = telemetry.get("agent_phone_health") or {}
    bridge = telemetry.get("garmin_bridge_health") or {}
    payload: Dict[str, Any] = {
        "ok": str(telemetry.get("garmin_bridge_health", {}).get("relay_status")) == "ok",
        "relay_status": bridge.get("relay_status"),
        "kill_switch_active": bridge.get("kill_switch_active"),
        "hermes_api": {
            "http_status": bridge.get("hermes_api_http_status"),
            "status": bridge.get("hermes_api_status"),
        },
        "phone": {
            "bridge_mode": phone.get("bridge_mode"),
            "battery_pct": phone.get("battery_pct"),
            "charging": phone.get("is_charging"),
            "screen_locked": phone.get("screen_locked"),
            "stale": phone.get("stale"),
            "last_seen_at": phone.get("last_seen_at"),
        },
        "queue_summary": telemetry.get("queue_summary") or {},
        "pending_approvals": telemetry.get("pending_approvals") or [],
        "spend_and_latency": telemetry.get("spend_and_latency") or {},
    }
    if args.get("include_requests"):
        payload["recent_requests"] = telemetry.get("recent_requests") or []
    return json.dumps(payload, ensure_ascii=False)
