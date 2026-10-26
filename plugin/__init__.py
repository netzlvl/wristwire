"""WristWire plugin — the watch is a first-class agent surface.

Registers three tools against the local relay daemon:

  watch_send    queue a message for the watch face
  watch_inbox   read what the watch sent + how each was answered
  watch_status  relay/phone health, queue depth, pending approvals

The daemon (``wristwire serve`` / ``adb-bridge``, a systemd service) owns the phone link,
the outbound queue, approval codes and the audit trail. This plugin is deliberately thin: it
holds no device secret, opens no ports, and never claims a delivery the relay has not confirmed.

Settings (``plugins.entries.wristwire.settings`` in ``config.yaml``):
  relay_url   default http://127.0.0.1:8788
  admin_key   relay admin key; falls back to ``GARMIN_RELAY_ADMIN_KEY`` or the relay's .env
"""

from __future__ import annotations

import functools
import logging

from .relay_client import relay_reachable
from .tools import (
    WATCH_INBOX_SCHEMA,
    WATCH_SEND_SCHEMA,
    WATCH_STATUS_SCHEMA,
    handle_watch_inbox,
    handle_watch_send,
    handle_watch_status,
)

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Wire schemas to handlers. No I/O here — registration stays side-effect free."""
    get_config = ctx.get_config

    def _handler(fn):
        return functools.partial(fn, _plugin_get_config=get_config)

    def _check() -> bool:
        try:
            return relay_reachable(get_config)
        except Exception:  # pragma: no cover — a gate must never crash the session
            return False

    tools = (
        ("watch_send", WATCH_SEND_SCHEMA, handle_watch_send, "⌚"),
        ("watch_inbox", WATCH_INBOX_SCHEMA, handle_watch_inbox, "📥"),
        ("watch_status", WATCH_STATUS_SCHEMA, handle_watch_status, "🟢"),
    )
    for name, schema, handler, emoji in tools:
        ctx.register_tool(
            name=name,
            toolset="watch",
            schema=schema,
            handler=_handler(handler),
            check_fn=_check,
            emoji=emoji,
        )
    logger.debug("wristwire plugin registered %d tools", len(tools))
