# How it works

WristWire has four layers. Only the top one knows which agent you use.

```
transport   phone ↔ relay      USB adb (no app on the phone) or HMAC-signed HTTP (Tasker or a phone app)
relay       queue, safety tiers, approval codes, audit trail, delivery checks, status page
adapter     the agent: hermes | claude | cli
plugin      Hermes tools (watch_send, watch_inbox, watch_status) and the dashboard tab
```

## A message, end to end

1. **Watch to phone.** You send a text or a voice note to the phone's Garmin Messenger account. Garmin transcribes voice notes, so the phone gets a notification with text.
2. **Phone to relay.** With USB, the relay reads `dumpsys notification` every two seconds on its own thread. With HTTP, the phone posts the notification to `/v1/garmin/ingest`, signed with `GARMIN_BRIDGE_SECRET`. Either way the message is deduplicated, checked against `GARMIN_ALLOWED_SENDERS` and rate-limited.
3. **Safety tier.** The relay classifies the text before any agent sees it:
   - *Tier A*, questions and notes, runs immediately.
   - *Tier B*, calendar, task and smart-home changes, runs and is logged.
   - *Tier C*, messaging other people, payments, deletions, network and security changes, waits for `approve CODE` from the watch. The code is bound to that one request and expires after 15 minutes.
4. **Agent.** The adapter runs the request. Hermes is driven through its runs API and polled until it finishes; if Hermes itself pauses for a tool approval, the relay forwards that as a code too. CLI adapters run once with a bounded tool list.
5. **Relay to phone.** The answer is queued in `outbound_replies`. With USB, a separate delivery loop wakes the phone if needed, opens your conversation (or uses it if it's already on screen), types the reply, taps send and checks the reply appeared. With HTTP, the phone polls `/v1/garmin/outbox` and acknowledges each reply.
6. **Phone to watch.** Garmin Messenger sends it to your watch.

## The status page

`GET /v1/garmin/status` returns the relay's telemetry plus a `links` section that [`wristwire_status.py`](../wristwire_status.py) works out from what the relay knows:

- The **USB bridge probe**, every 30 seconds in two `adb` calls (`get-state`, then one shell script): is the phone attached and authorised, its model, battery, screen, and whether Garmin Messenger is running.
- The **last notification read**, the **last watch message**, the **last delivered reply** and the **last delivery error**.
- The **agent check**: Hermes's `/health/detailed`, or whether the CLI agent's command exists.

Each link gets an inbound and an outbound state: `ok`, `warn`, `down`, `idle` (no traffic yet) or `unseen` (WristWire can't observe it). The page itself is one script, [`plugin/dashboard/dist/index.js`](../plugin/dashboard/dist/index.js), used both by the relay's own page and by the Hermes dashboard tab. It builds everything with DOM calls rather than HTML strings, so text from messages can't become markup.

## Files

| Path | What it is |
|---|---|
| `wristwire_relay.py` | The relay: config, HTTP API, queue, tiers, approvals, Hermes client, USB bridge, CLI. |
| `wristwire_adapters.py` | The `claude` and `cli` agent adapters. |
| `wristwire_status.py` | Link health for the status page. Pure functions, tested without a phone. |
| `test_wristwire.py` | The test suite (`python3 -m unittest test_wristwire`). |
| `plugin/` | The Hermes plugin: tools, relay client, and `dashboard/` for the tab. |
| `android/GarminBridgeService.kt` | Reference code for a phone app that replies through notification actions instead of the screen. Not packaged yet. |
| `systemd/wristwire.service` | A user service that runs the relay with the USB bridge. |
| `deploy.sh` | Install helper and signed-request helpers for Tasker. |

## HTTP API

All on `http://127.0.0.1:8788` by default.

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | none | Liveness and kill switch state. |
| `GET /status` | none (page only; data needs the key) | The status page. |
| `POST /v1/garmin/ui/code` | admin key | A one-time sign-in code for the status page (what `wristwire dashboard` uses). |
| `POST /v1/garmin/ui/session` | the one-time code | Trades the code for a 12-hour status-page session. |
| `GET /v1/garmin/status` | admin key or page session | Telemetry plus per-link health. |
| `GET /v1/garmin/telemetry` | admin key | Queue, approvals, phone health, spend and latency. |
| `POST /v1/garmin/send` | admin key or page session | Queue a message for the watch: `{"message": "..."}`. |
| `POST /v1/garmin/control/killswitch` | admin key | `{"enabled": true}` stops agent runs. |
| `POST /v1/garmin/ingest`, `/inspect`, `/heartbeat` | phone HMAC | Used by the HTTP transport. |
| `GET /v1/garmin/outbox`, `POST /v1/garmin/outbox/<reply_id>/ack` | phone HMAC | Used by the HTTP transport. |

The admin key is sent as `Authorization: Bearer <GARMIN_RELAY_ADMIN_KEY>`. Hermes's `API_SERVER_KEY` is accepted too, so Hermes can call the relay with the key it already has. Empty keys and the example values in this repository are never accepted, and requests addressed to an unknown host name are refused.
