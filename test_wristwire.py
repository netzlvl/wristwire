#!/usr/bin/env python3
"""
Automated Acceptance Test Suite for the WristWire relay (Acceptance Tests A-H)
==============================================================================
Verifies:
- Device HMAC-SHA256 authentication, timestamp skew rejection, and nonce replay blocking.
- Immediate Implementation Gate (/v1/garmin/inspect) recording observable notification
  fields and RemoteInput reply capability.
- Acceptance Test A & B: Garmin watch text & voice-note transcript ingestion -> Hermes /v1/runs
  with X-Hermes-Session-Key and Idempotency-Key -> RemoteInput outbound reply queue.
- Acceptance Test C: Tier A read-only query, Tier B bounded calendar action, and Tier C
  high-risk command gated behind a 6-character approval code (plus rejection of bare 'approved').
- Acceptance Test D: Network retry and duplicate message deduplication producing zero
  duplicate Hermes runs.
- Acceptance Test E: Unapproved Garmin sender blocked and logged; native Hermes tool
  approval ('waiting_for_approval' / pending_controls) bridged to watch approval code.
- Acceptance Test F & G: Watch control commands ('status', 'cancel'), admin kill switch,
  Agent phone health heartbeat, delivery failure alerting, and token/latency telemetry.
- Proactive Hermes -> Watch Garmin Messenger send API (POST /v1/garmin/send) & USB ADB
  notification parser verification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Tuple

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wristwire_adapters
from wristwire_adapters import AdapterResult, CliAdapter, build_adapter
from wristwire_relay import (
    AdbGarminBridge,
    GarminRelayEngine,
    RelayConfig,
    make_handler,
)


# ---------------------------------------------------------------------------
# Mock Hermes API Server (/v1/runs, /health/detailed)
# ---------------------------------------------------------------------------


class MockHermesState:
    def __init__(self) -> None:
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.idempotency_ledger: Dict[str, str] = {}
        self.session_keys_seen: List[str] = []
        self.simulate_slow_run: bool = False
        self.simulate_tool_gate: bool = False
        self.approvals_received: List[Dict[str, Any]] = []
        self.stopped_runs: List[str] = []


def make_mock_hermes_handler(state: MockHermesState) -> type[BaseHTTPRequestHandler]:
    class MockHermesHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send(self, code: int, payload: Dict[str, Any]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/health/detailed":
                self._send(200, {"status": "ok", "readiness": {"checks": "all_green"}})
                return

            if self.path.startswith("/v1/runs/"):
                run_id = self.path.split("/")[-1]
                run = state.runs.get(run_id)
                if not run:
                    self._send(404, {"error": "not_found"})
                    return
                polls = run.get("polls", 0) + 1
                run["polls"] = polls

                if run.get("slow") and polls < 3:
                    self._send(
                        200,
                        {
                            "object": "hermes.run",
                            "run_id": run_id,
                            "status": "running",
                            "session_id": run["session_id"],
                        },
                    )
                    return

                if run.get("status") == "waiting_for_approval":
                    # Real wire shape: POST/GET /v1/runs park the pending prompt under
                    # `approval` (an approval.request event), not `pending_controls`.
                    self._send(
                        200,
                        {
                            "object": "hermes.run",
                            "run_id": run_id,
                            "status": "waiting_for_approval",
                            "session_id": run["session_id"],
                            "last_event": "approval.request",
                            "approval": {
                                "event": "approval.request",
                                "run_id": run_id,
                                "request_id": "prompt_gate_99",
                                "execution_generation": 2,
                                "tool": "gmail_send",
                                "command": "Send external email to client",
                                "choices": ["once", "session", "always", "deny"],
                            },
                        },
                    )
                    return

                if run.get("status") == "cancelled":
                    self._send(
                        200,
                        {
                            "object": "hermes.run",
                            "run_id": run_id,
                            "status": "cancelled",
                            "error": "Stopped by user",
                        },
                    )
                    return

                self._send(
                    200,
                    {
                        "object": "hermes.run",
                        "run_id": run_id,
                        "status": "completed",
                        "session_id": run["session_id"],
                        "model": "hermes-agent",
                        "output": f"Executed: {run['input'][:60]}",
                        "usage": {
                            "input_tokens": 42,
                            "output_tokens": 18,
                            "total_tokens": 60,
                            "cache_read_tokens": 10,
                            "cache_write_tokens": 0,
                        },
                        "runtime": {
                            "provider": "minimax",
                            "model": "MiniMax-M3",
                            "route_source": "global",
                        },
                    },
                )
                return

            self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}

            if self.path == "/v1/runs":
                idem = self.headers.get("Idempotency-Key", "")
                sess_key = self.headers.get("X-Hermes-Session-Key", "")
                state.session_keys_seen.append(sess_key)

                if idem in state.idempotency_ledger:
                    existing_id = state.idempotency_ledger[idem]
                    self._send(202, {"run_id": existing_id, "status": "started"})
                    return

                run_id = f"run_{secrets.token_hex(4)}"
                init_status = "waiting_for_approval" if state.simulate_tool_gate else "running"
                state.runs[run_id] = {
                    "run_id": run_id,
                    "input": body.get("input", ""),
                    "session_id": body.get("session_id", ""),
                    "status": init_status,
                    "slow": state.simulate_slow_run,
                    "polls": 0,
                }
                if idem:
                    state.idempotency_ledger[idem] = run_id
                self._send(200, {"run_id": run_id, "status": "started"})
                return

            if self.path.endswith("/stop"):
                run_id = self.path.split("/")[-2]
                state.stopped_runs.append(run_id)
                if run_id in state.runs:
                    state.runs[run_id]["status"] = "cancelled"
                self._send(200, {"status": "stopping"})
                return

            if self.path.endswith("/approval"):
                run_id = self.path.split("/")[-2]
                state.approvals_received.append({"run_id": run_id, **body})
                if run_id in state.runs:
                    state.runs[run_id]["status"] = (
                        "completed" if body.get("choice") == "once" else "cancelled"
                    )
                self._send(200, {"status": "resumed"})
                return

            self._send(404, {"error": "not_found"})

    return MockHermesHandler


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------


class HermesGarminRelayAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_relay.db")

        self.hermes_state = MockHermesState()
        self.hermes_server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_mock_hermes_handler(self.hermes_state)
        )
        self.hermes_port = self.hermes_server.server_address[1]
        self.hermes_thread = threading.Thread(
            target=self.hermes_server.serve_forever, daemon=True
        )
        self.hermes_thread.start()

        self.config = RelayConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            db_path=self.db_path,
            device_id="pixel-garmin-agent",
            device_secret="test-hmac-secret-key-2026",
            relay_admin_key="test-relay-admin-key",
            hermes_api_url=f"http://127.0.0.1:{self.hermes_port}",
            hermes_api_key="hermes-loopback-secret",
            allowed_senders=["owner", "watch-owner", "+15550199"],
            ack_threshold_seconds=0.05,
            poll_interval_seconds=0.03,
            synchronous_execution=True,
        )
        self.engine = GarminRelayEngine(self.config)
        self.relay_server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.engine)
        )
        self.relay_port = self.relay_server.server_address[1]
        self.relay_thread = threading.Thread(
            target=self.relay_server.serve_forever, daemon=True
        )
        self.relay_thread.start()

    def tearDown(self) -> None:
        self.relay_server.server_close()
        self.hermes_server.server_close()
        self.temp_dir.cleanup()

    def _signed_request(
        self,
        method: str,
        path: str,
        payload: Dict[str, Any] | None = None,
        timestamp: float | None = None,
        nonce: str | None = None,
        secret: str | None = None,
    ) -> Tuple[int, Dict[str, Any]]:
        raw_body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        ts_str = f"{timestamp if timestamp is not None else time.time():.3f}"
        nonce_val = nonce if nonce is not None else f"nonce_{secrets.token_hex(8)}"
        sec = secret if secret is not None else self.config.device_secret
        body_hash = hashlib.sha256(raw_body).hexdigest()
        canonical = f"{ts_str}.{nonce_val}.{method.upper()}.{path}.{body_hash}"
        sig = hmac.new(sec.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()

        url = f"http://127.0.0.1:{self.relay_port}{path}"
        headers = {
            "X-Device-Id": self.config.device_id,
            "X-Timestamp": ts_str,
            "X-Nonce": nonce_val,
            "X-Signature": sig,
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(
            url, data=raw_body if method == "POST" else None, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _admin_request(
        self, method: str, path: str, payload: Dict[str, Any] | None = None
    ) -> Tuple[int, Dict[str, Any]]:
        raw_body = json.dumps(payload).encode("utf-8") if payload is not None else None
        url = f"http://127.0.0.1:{self.relay_port}{path}"
        headers = {
            "Authorization": f"Bearer {self.config.relay_admin_key}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_01_auth_clock_skew_and_nonce_replay(self) -> None:
        """Reject bad signatures, stale timestamps, and replayed nonces."""
        code, res = self._signed_request(
            "POST", "/v1/garmin/heartbeat", {"battery_pct": 95}, secret="wrong-secret"
        )
        self.assertEqual(code, 401)
        self.assertEqual(res["error"], "invalid_hmac_signature")

        code, res = self._signed_request(
            "POST",
            "/v1/garmin/heartbeat",
            {"battery_pct": 95},
            timestamp=time.time() - 120,
        )
        self.assertEqual(code, 401)
        self.assertEqual(res["error"], "timestamp_skew_exceeded")

        fixed_nonce = "nonce_fixed_1234567890"
        ts = time.time()
        code1, _ = self._signed_request(
            "POST",
            "/v1/garmin/heartbeat",
            {"battery_pct": 98},
            timestamp=ts,
            nonce=fixed_nonce,
        )
        self.assertEqual(code1, 200)

        code2, res2 = self._signed_request(
            "POST",
            "/v1/garmin/heartbeat",
            {"battery_pct": 98},
            timestamp=ts,
            nonce=fixed_nonce,
        )
        self.assertEqual(code2, 401)
        self.assertEqual(res2["error"], "replayed_nonce")

    def test_02_immediate_implementation_gate_inspection(self) -> None:
        """Record Android notification fields and RemoteInput presence."""
        code, res = self._signed_request(
            "POST",
            "/v1/garmin/inspect",
            {
                "package_name": "com.garmin.android.apps.messenger",
                "sender": "watch-owner",
                "has_text": True,
                "has_voice_transcript": True,
                "has_remote_input": True,
                "action_titles": ["Reply", "Mark as Read"],
                "notification_key": "0|com.garmin.android.apps.messenger|1001|null|10243",
                "screen_locked": True,
                "lte_mode": "always_on",
                "notes": "Voice note transcript present in bigText; RemoteInput Reply action available while locked.",
            },
        )
        self.assertEqual(code, 200)
        self.assertEqual(
            res["recommended_architecture"],
            "tasker_or_notification_listener_remote_input",
        )

    def test_03_tier_a_voice_note_roundtrip_and_deduplication(self) -> None:
        """Acceptance Tests A, B, C, D: Voice note -> Hermes /v1/runs -> Outbox RemoteInput ack + Retry Deduplication."""
        payload = {
            "garmin_message_id": "gmsg_20261026_001",
            "sender_id": "watch-owner",
            "source_type": "voice_note",
            "transcript_text": "Note this: check mountain bike tire sealant pressure before Saturday.",
        }
        code, res = self._signed_request("POST", "/v1/garmin/ingest", payload)
        self.assertEqual(code, 202)
        self.assertEqual(res["tier"], "TIER_A")
        self.assertEqual(res["status"], "completed")
        self.assertFalse(res["replayed"])
        req_id = res["request_id"]

        self.assertIn("agent:main:garmin:dm:owner", self.hermes_state.session_keys_seen)

        run_count_before = len(self.hermes_state.runs)
        code_retry, res_retry = self._signed_request("POST", "/v1/garmin/ingest", payload)
        self.assertEqual(code_retry, 200)
        self.assertTrue(res_retry["replayed"])
        self.assertEqual(res_retry["request_id"], req_id)
        self.assertEqual(len(self.hermes_state.runs), run_count_before)

        code_out, outbox = self._signed_request("GET", "/v1/garmin/outbox")
        self.assertEqual(code_out, 200)
        self.assertEqual(len(outbox["items"]), 1)
        reply_item = outbox["items"][0]
        self.assertEqual(reply_item["request_id"], req_id)
        self.assertIn("Executed:", reply_item["reply_text"])

        code_ack, ack_res = self._signed_request(
            "POST",
            f"/v1/garmin/outbox/{reply_item['reply_id']}/ack",
            {"delivered": True, "delivery_method": "remote_input"},
        )
        self.assertEqual(code_ack, 200)
        self.assertEqual(ack_res["status"], "delivered")

    def _raw(self, method, path, headers=None, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.relay_port}{path}", data=body, headers=headers or {}, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    def test_placeholder_and_empty_keys_never_authenticate(self) -> None:
        self.engine.config.hermes_api_key = "change-me-local-dev"
        for bearer in ("change-me-local-dev", "", " "):
            code, _ = self._raw("GET", "/v1/garmin/status", {"Authorization": f"Bearer {bearer}"})
            self.assertEqual(code, 401, f"bearer {bearer!r} must be rejected")
        self.engine.config.hermes_api_key = ""
        code, _ = self._raw("GET", "/v1/garmin/telemetry", {"Authorization": "Bearer "})
        self.assertEqual(code, 401)

    def test_foreign_host_header_is_refused(self) -> None:
        auth = {"Authorization": f"Bearer {self.config.relay_admin_key}"}
        code, body = self._raw("GET", "/v1/garmin/status", dict(auth, Host="evil.example"))
        self.assertEqual(code, 421)
        self.assertEqual(body["error"], "host_not_allowed")
        code, _ = self._raw("GET", "/v1/garmin/status", dict(auth, Host=f"localhost:{self.relay_port}"))
        self.assertEqual(code, 200)
        self.assertFalse(self.engine.host_allowed("localhost:1@evil.example"))
        self.assertFalse(self.engine.host_allowed("127.0.0.1.evil.example"))
        self.assertTrue(self.engine.host_allowed("[::1]:8788"))

    def test_one_time_dashboard_code(self) -> None:
        code, _ = self._raw("POST", "/v1/garmin/ui/code", {"Content-Type": "application/json"}, b"{}")
        self.assertEqual(code, 401, "issuing a code needs the admin key")
        status, issued = self._admin_request("POST", "/v1/garmin/ui/code", {})
        self.assertEqual(status, 200)
        body = json.dumps({"code": issued["code"]}).encode()
        status, session = self._raw("POST", "/v1/garmin/ui/session", {"Content-Type": "application/json"}, body)
        self.assertEqual(status, 200)
        status, again = self._raw("POST", "/v1/garmin/ui/session", {"Content-Type": "application/json"}, body)
        self.assertEqual(status, 401, "a code works once")
        bearer = {"Authorization": f"Bearer {session['token']}"}
        self.assertEqual(self._raw("GET", "/v1/garmin/status", bearer)[0], 200)
        self.assertEqual(self._raw("GET", "/v1/garmin/telemetry", bearer)[0], 401, "session is status-page only")
        self.assertEqual(
            self._raw("POST", "/v1/garmin/control/killswitch", dict(bearer, **{"Content-Type": "application/json"}), b"{}")[0],
            401,
        )

    def test_bad_json_gets_400_not_a_dropped_connection(self) -> None:
        headers = {"Authorization": f"Bearer {self.config.relay_admin_key}", "Content-Type": "application/json"}
        self.assertEqual(self._raw("POST", "/v1/garmin/send", headers, b"{not json")[0], 400)

    def test_status_tracks_one_test_reply_to_delivery(self) -> None:
        _, sent = self._admin_request("POST", "/v1/garmin/send", {"message": "WristWire test"})
        rid = sent["reply_id"]
        _, st = self._admin_request("GET", f"/v1/garmin/status?reply={rid}")
        self.assertEqual(st["tracked_reply"]["status"], "pending_delivery")
        self.engine.ack_outbox_item(rid, delivered=True, delivery_method="usb_adb_ui")
        _, st = self._admin_request("GET", f"/v1/garmin/status?reply={rid}")
        self.assertEqual(st["tracked_reply"]["status"], "delivered")
        self.assertIsNotNone(st["tracked_reply"]["delivered_at"])

    def test_test_message_is_not_counted_as_watch_traffic(self) -> None:
        self._admin_request("POST", "/v1/garmin/send", {"message": "WristWire test"})
        _, status = self._admin_request("GET", "/v1/garmin/status")
        watch_in = status["links"]["links"][0]["inbound"]
        self.assertNotEqual(watch_in["state"], "ok")
        self.assertNotIn("Last watch message", watch_in["text"])

    def test_status_endpoint_and_page(self) -> None:
        code, _ = self._signed_request("GET", "/v1/garmin/status")
        self.assertEqual(code, 401, "device credentials must not read the status page data")
        code, status = self._admin_request("GET", "/v1/garmin/status")
        self.assertEqual(code, 200)
        links = status["links"]
        self.assertEqual([n["id"] for n in links["nodes"]], ["watch", "phone", "relay", "agent"])
        self.assertEqual(len(links["links"]), 3)
        self.assertIn(links["verdict"]["state"], ("ok", "warn", "down"))
        self.assertIn("recent_requests", status)

        url = f"http://127.0.0.1:{self.relay_port}"
        with urllib.request.urlopen(url + "/status", timeout=5.0) as resp:
            page = resp.read().decode("utf-8")
            csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn('id="wristwire-root"', page)
        self.assertIn("script-src 'self'", csp)
        with urllib.request.urlopen(url + "/status/index.js", timeout=5.0) as resp:
            self.assertIn("createView", resp.read().decode("utf-8"))

    def test_04_working_ack_is_superseded_when_the_answer_is_ready(self) -> None:
        """An ack still waiting when the final lands is dropped, so it never delays the answer."""
        self.hermes_state.simulate_slow_run = True
        code, res = self._signed_request(
            "POST",
            "/v1/garmin/ingest",
            {
                "garmin_message_id": "gmsg_slow_002",
                "sender_id": "owner",
                "source_type": "text",
                "message_text": "Move my 3pm meeting to 4pm tomorrow",
            },
        )
        self.assertEqual(code, 202)
        self.assertEqual(res["tier"], "TIER_B")

        code_out, outbox = self._signed_request("GET", "/v1/garmin/outbox")
        self.assertEqual(code_out, 200)
        kinds = [item["reply_kind"] for item in outbox["items"]]
        self.assertEqual(kinds, ["final"])
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT reply_kind, status, reply_text FROM outbound_replies "
                "WHERE request_id = ? ORDER BY created_at",
                (res["request_id"],),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows[0][0], "ack")
        self.assertEqual(rows[0][1], "superseded")
        self.assertIn("Working on it...", rows[0][2])

    def test_05_unapproved_sender_and_tier_c_approval_binding(self) -> None:
        """Acceptance Test E: Block unknown sender; hold Tier-C commands; reject bare 'approved'; accept bound code."""
        code_bad, _ = self._signed_request(
            "POST",
            "/v1/garmin/ingest",
            {
                "garmin_message_id": "gmsg_stranger_1",
                "sender_id": "random_stranger",
                "message_text": "Status",
            },
        )
        self.assertEqual(code_bad, 403)

        code_c, res_c = self._signed_request(
            "POST",
            "/v1/garmin/ingest",
            {
                "garmin_message_id": "gmsg_tierc_1",
                "sender_id": "owner",
                "source_type": "voice_note",
                "transcript_text": "Send an email to client confirming the wire payment.",
            },
        )
        self.assertEqual(code_c, 202)
        self.assertEqual(res_c["status"], "needs_approval")
        self.assertEqual(res_c["tier"], "TIER_C")
        approval_code = res_c["approval_code"]
        self.assertEqual(len(approval_code), 6)

        code_bare, res_bare = self._signed_request(
            "POST",
            "/v1/garmin/ingest",
            {
                "garmin_message_id": "gmsg_bare_approve",
                "sender_id": "owner",
                "message_text": "approved",
            },
        )
        self.assertEqual(code_bare, 400)
        self.assertEqual(res_bare["error"], "missing_approval_code")

        code_ok, res_ok = self._signed_request(
            "POST",
            "/v1/garmin/ingest",
            {
                "garmin_message_id": "gmsg_bound_approve",
                "sender_id": "owner",
                "message_text": f"approve {approval_code}",
            },
        )
        self.assertEqual(code_ok, 200)
        self.assertEqual(res_ok["decision"], "approved")
        self.assertEqual(res_ok["approved_request_id"], res_c["request_id"])

    def test_06_hermes_native_tool_approval_and_telemetry(self) -> None:
        """Bridge Hermes /v1/runs/{id}/approval and verify telemetry + kill switch."""
        self.hermes_state.simulate_tool_gate = True
        code, res = self._signed_request(
            "POST",
            "/v1/garmin/ingest",
            {
                "garmin_message_id": "gmsg_toolgate_1",
                "sender_id": "owner",
                "message_text": "Check server status summary",
            },
        )
        self.assertEqual(code, 202)
        self.assertEqual(res["status"], "needs_approval")

        t_code, telemetry = self._admin_request("GET", "/v1/garmin/telemetry")
        self.assertEqual(t_code, 200)
        self.assertEqual(len(telemetry["pending_approvals"]), 1)
        gate_code = telemetry["pending_approvals"][0]["approval_code"]

        code_app, _ = self._signed_request(
            "POST",
            "/v1/garmin/ingest",
            {
                "garmin_message_id": "gmsg_toolgate_approve",
                "sender_id": "owner",
                "message_text": f"approve {gate_code}",
            },
        )
        self.assertEqual(code_app, 200)
        self.assertEqual(len(self.hermes_state.approvals_received), 1)
        self.assertEqual(
            self.hermes_state.approvals_received[0]["request_id"], "prompt_gate_99"
        )
        self.assertEqual(self.hermes_state.approvals_received[0]["choice"], "once")

        ks_code, ks_res = self._admin_request(
            "POST",
            "/v1/garmin/control/killswitch",
            {"enabled": True, "reason": "Security drill"},
        )
        self.assertEqual(ks_code, 200)
        self.assertTrue(ks_res["kill_switch"])

        code_blocked, _ = self._signed_request(
            "POST",
            "/v1/garmin/ingest",
            {
                "garmin_message_id": "gmsg_after_kill",
                "sender_id": "owner",
                "message_text": "status",
            },
        )
        self.assertEqual(code_blocked, 503)

    def test_07_hermes_proactive_send_and_adb_parser(self) -> None:
        """Verify Hermes can proactively message the watch via POST /v1/garmin/send and ADB dumpsys parser extracts Garmin notifications."""
        code, res = self._admin_request(
            "POST",
            "/v1/garmin/send",
            {
                "recipient": "owner",
                "message": "Hermes alert: Uptime monitor reports all nodes healthy.",
            },
        )
        self.assertEqual(code, 202)
        self.assertEqual(res["status"], "queued_for_watch")

        code_out, outbox = self._signed_request("GET", "/v1/garmin/outbox")
        self.assertEqual(code_out, 200)
        self.assertEqual(len(outbox["items"]), 1)
        self.assertIn("Uptime monitor reports all nodes healthy", outbox["items"][0]["reply_text"])

        sample_dumpsys = """
        NotificationRecord(0x01a2b3: pkg=com.garmin.android.apps.messenger user=UserHandle{0} id=101
          key=0|com.garmin.android.apps.messenger|101|null|10243
          extras={
            android.title=String (watch-owner)
            android.bigText=String (Voice message transcript: Move my 3pm meeting)
          }
          actions=[Action { RemoteInput }]
        """
        adb_bridge = AdbGarminBridge(self.engine)
        parsed = adb_bridge.parse_garmin_notifications(sample_dumpsys)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["sender"], "watch-owner")
        self.assertIn("Move my 3pm meeting", parsed[0]["body"])
        self.assertTrue(parsed[0]["has_remote_input"])


def _ui_node(
    text: str, rid: str, bounds: Tuple[int, int, int, int], focused: bool = False
) -> str:
    return (
        f'<node text="{text}" resource-id="{rid}" class="android.widget.TextView" '
        f'focused="{"true" if focused else "false"}" '
        f'bounds="[{bounds[0]},{bounds[1]}][{bounds[2]},{bounds[3]}]" />'
    )


def _thread_screen(
    composer_text: str = "",
    messages: Iterable[str] = (),
    title: str = "FENIX",
    focused: bool = True,
) -> str:
    nodes = [
        _ui_node(title, "com.garmin.android.apps.messenger:id/toolbarTitle", (236, 92, 406, 146)),
        _ui_node(
            composer_text,
            "com.garmin.android.apps.messenger:id/newMessageInputEditText",
            (200, 1304, 612, 1365),
            focused=focused,
        ),
        _ui_node("", "com.garmin.android.apps.messenger:id/sendButton", (612, 1304, 708, 1365)),
    ]
    y = 1100
    for message in messages:
        nodes.append(_ui_node(message, "", (80, y, 640, y + 60)))
        y -= 90
    return "<hierarchy>" + "".join(nodes) + "</hierarchy>"


def _chat_list_screen() -> str:
    return "<hierarchy>" + _ui_node("FENIX", "", (40, 300, 600, 360)) + "</hierarchy>"


class ScriptedAdbBridge(AdbGarminBridge):
    """Exercises the UI automation path without a phone, by scripting screen dumps."""

    def __init__(self, engine: GarminRelayEngine, screens: List[str]) -> None:
        super().__init__(engine)
        self.screens = list(screens)
        self.commands: List[Tuple[str, ...]] = []
        self.typed: List[str] = []
        self.scripts: List[str] = []
        self.delays_enabled = False

    def _adb_cmd(self, *args: str) -> str:
        self.commands.append(args)
        if args[:2] == ("shell", "dumpsys") and args[-1] == "power":
            return "mWakefulness=Awake"
        return ""

    def _input_text(self, text: str) -> None:
        self.typed.append(self.escape_input_text(text))

    def _run_script(self, script: str) -> None:
        self.scripts.append(script)

    def _ui_nodes(self) -> List[Dict[str, Any]]:
        xml = self.screens.pop(0) if self.screens else ""
        return self._parse_ui_nodes(xml)


class LinkStatusTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def facts(self, **over):
        from wristwire_status import compute_links  # noqa: F401  (import check)

        base = {
            "now": self.NOW,
            "transport": "usb_adb",
            "bridge": {
                "state": "device",
                "last_poll_ok_at": self.NOW - 3,
                "model": "SM-A015V",
                "battery_pct": 81,
                "charging": True,
                "awake": False,
                "locked": True,
                "messenger_running": True,
            },
            "last_inbound": {"at": self.NOW - 240, "source_type": "voice_note"},
            "delivery": {"last_delivered_at": self.NOW - 200, "pending": 0},
            "agent": {"adapter": "hermes", "label": "Hermes", "via": "Hermes API", "ok": True, "latency_ms": 40},
            "last_answer_at": self.NOW - 210,
            "kill_switch": False,
            "relay": {"started_at": self.NOW - 7200, "version": "0.2.0"},
        }
        base.update(over)
        return base

    def links(self, **over):
        from wristwire_status import compute_links

        return compute_links(self.facts(**over))

    @staticmethod
    def lane(result, link_id, direction):
        link = next(l for l in result["links"] if l["id"] == link_id)
        return link[direction]

    def test_healthy_chain(self) -> None:
        r = self.links()
        self.assertEqual(r["verdict"], {"state": "ok", "text": "Everything is connected."})
        self.assertEqual(self.lane(r, "watch-phone", "outbound")["state"], "unseen")
        self.assertIn("voice note", self.lane(r, "watch-phone", "inbound")["text"])
        phone = next(n for n in r["nodes"] if n["id"] == "phone")
        self.assertIn("81% battery, charging", phone["facts"])
        self.assertEqual(r["problems"], [])

    def test_usb_debugging_not_allowed(self) -> None:
        r = self.links(bridge={"state": "unauthorized"})
        self.assertEqual(r["verdict"]["state"], "down")
        self.assertEqual(self.lane(r, "phone-relay", "inbound")["state"], "down")
        self.assertEqual(self.lane(r, "phone-relay", "outbound")["state"], "down")
        self.assertIn("Allow USB debugging", r["problems"][0]["fix"])

    def test_stuck_draft_names_the_fix(self) -> None:
        r = self.links(
            delivery={
                "last_delivered_at": self.NOW - 900,
                "last_failure": {"at": self.NOW - 30, "error": "compose_view_expanded"},
                "pending": 1,
                "oldest_pending_at": self.NOW - 60,
            }
        )
        out = self.lane(r, "phone-relay", "outbound")
        self.assertEqual(out["state"], "down")
        self.assertIn("compose view expanded", out["text"])
        self.assertIn("Clear it by hand", r["problems"][0]["fix"])

    def test_old_failure_before_a_later_delivery_is_not_shown(self) -> None:
        r = self.links(
            delivery={
                "last_delivered_at": self.NOW - 10,
                "last_failure": {"at": self.NOW - 500, "error": "text_not_entered"},
                "pending": 0,
            }
        )
        self.assertEqual(self.lane(r, "phone-relay", "outbound")["state"], "ok")

    def test_messenger_not_running_breaks_the_watch_link(self) -> None:
        bridge = dict(self.facts()["bridge"], messenger_running=False)
        r = self.links(bridge=bridge)
        self.assertEqual(self.lane(r, "watch-phone", "inbound")["state"], "down")
        self.assertEqual(r["verdict"]["text"], "Garmin Messenger isn't running on the phone")

    def test_stale_phone_app_heartbeat(self) -> None:
        r = self.links(transport="http", bridge={}, heartbeat={"last_seen_at": self.NOW - 900})
        self.assertEqual(self.lane(r, "phone-relay", "inbound")["state"], "down")
        self.assertIn("15\u00a0min", self.lane(r, "phone-relay", "inbound")["text"])

    def test_kill_switch_and_unreachable_agent(self) -> None:
        r = self.links(kill_switch=True)
        self.assertEqual(self.lane(r, "relay-agent", "inbound")["state"], "down")
        r = self.links(agent={"label": "Hermes", "ok": False, "fix": "Start Hermes."})
        self.assertEqual(r["verdict"]["text"], "The relay can't reach Hermes")

    def test_no_traffic_yet_is_not_an_error(self) -> None:
        r = self.links(last_inbound=None, delivery={"pending": 0}, last_answer_at=None)
        self.assertEqual(r["verdict"]["state"], "ok")
        self.assertIn("Waiting for traffic", r["verdict"]["text"])

    def test_probe_and_adb_state_parsing(self) -> None:
        from wristwire_status import classify_adb_state, parse_probe_output

        probe = parse_probe_output(
            "SM-A015V\n@@\n  AC powered: false\n  USB powered: true\n  level: 64\n@@\n"
            "mWakefulness=Asleep\n@@\ndeviceLocked=1\n@@\n12345\n"
        )
        self.assertEqual(probe["model"], "SM-A015V")
        self.assertEqual(probe["battery_pct"], 64)
        self.assertTrue(probe["charging"])
        self.assertFalse(probe["awake"])
        self.assertTrue(probe["locked"])
        self.assertTrue(probe["messenger_running"])
        self.assertFalse(parse_probe_output("x@@@@@@@@none")["messenger_running"])
        self.assertEqual(classify_adb_state(0, "device\n", "")["state"], "device")
        self.assertEqual(classify_adb_state(1, "", "error: device unauthorized.")["state"], "unauthorized")
        self.assertEqual(classify_adb_state(1, "", "error: no devices/emulators found")["state"], "missing")


class SpeedDefaultsTests(unittest.TestCase):
    def test_relay_refuses_to_start_with_example_admin_key(self) -> None:
        from wristwire_relay import is_real_secret, run_server

        self.assertFalse(is_real_secret("change-me-garmin-relay-admin-key"))
        self.assertFalse(is_real_secret("replace-with-random-admin-secret"))
        self.assertFalse(is_real_secret(""))
        self.assertTrue(is_real_secret("hga_" + "x" * 30))
        with self.assertRaises(SystemExit):
            run_server(RelayConfig(relay_admin_key="change-me-garmin-relay-admin-key"))

    def test_working_ack_is_off_by_default(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GARMIN_ACK_AFTER_SECONDS", None)
            self.assertEqual(RelayConfig().ack_threshold_seconds, 0.0)
        with unittest.mock.patch.dict(os.environ, {"GARMIN_ACK_AFTER_SECONDS": "20"}):
            self.assertEqual(RelayConfig().ack_threshold_seconds, 20.0)

    def test_bad_env_values_fall_back_instead_of_crashing(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {"GARMIN_ACK_AFTER_SECONDS": "soon", "HERMES_GARMIN_SESSION_ROTATION": "weekly"},
        ):
            cfg = RelayConfig()
        self.assertEqual(cfg.ack_threshold_seconds, 0.0)
        self.assertEqual(cfg.hermes_session_rotation, "daily")
        with unittest.mock.patch.dict(os.environ, {"GARMIN_ACK_AFTER_SECONDS": "20s"}):
            self.assertEqual(RelayConfig().ack_threshold_seconds, 20.0)

    def test_superseded_ack_is_never_resent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = GarminRelayEngine(RelayConfig(db_path=os.path.join(tmp, "r.db")))
            ack = engine.enqueue_reply("req1", "owner", "Working on it...", reply_kind="ack")
            picked = [i["reply_id"] for i in engine.poll_outbox()]  # delivery starts
            self.assertEqual(picked, [ack])
            engine.enqueue_reply("req1", "owner", "The answer", reply_kind="final")
            self.assertFalse(engine.reply_still_pending(ack))  # stale batch skips it
            engine.ack_outbox_item(ack, delivered=False, error_message="adb offline")
            kinds = [i["reply_kind"] for i in engine.poll_outbox()]
            self.assertEqual(kinds, ["final"], "a failed ack must not come back after the answer")

    def test_hermes_session_rotates_daily_unless_disabled(self) -> None:
        from wristwire_relay import HermesApiClient

        noon = time.mktime((2026, 10, 26, 12, 0, 0, 0, 0, -1))
        daily = HermesApiClient(RelayConfig(hermes_session_id="watch", hermes_session_rotation="daily"))
        self.assertEqual(daily.current_session_id(noon), "watch-2026-10-26")
        self.assertNotEqual(
            daily.current_session_id(noon), daily.current_session_id(noon + 86400)
        )
        fixed = HermesApiClient(RelayConfig(hermes_session_id="watch", hermes_session_rotation="never"))
        self.assertEqual(fixed.current_session_id(noon), "watch")


class AdbAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = RelayConfig(
            db_path=os.path.join(self.temp_dir.name, "adb_test.db"),
            allowed_senders=["fenix"],
            default_owner_recipient="FENIX",
        )
        self.engine = GarminRelayEngine(self.config)
        self.engine.hermes = _NullHermesClient(self.config)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_foreign_and_call_notifications_are_ignored(self) -> None:
        """A system notification mentioning the package must not arrive as a watch message."""
        dumpsys = """
        NotificationRecord(0x01: pkg=android user=UserHandle{-1} id=27
          key=-1|android|27|null|1000
          extras={
            android.title=String (USB for file transfer)
            android.text=String (Tap for other USB options.)
          }
          mArchive=Archive (2 notifications)
            StatusBarNotification(pkg=com.garmin.android.apps.messenger id=101
              key=0|com.garmin.android.apps.messenger|101|1|10205)
        NotificationRecord(0x02: pkg=com.garmin.android.apps.messenger user=UserHandle{0} id=3838
          key=0|com.garmin.android.apps.messenger|3838|null|10205
          Notification(channel=sip_ongoing pri=2 actions=1)
          extras={
            android.title=String (Ongoing call)
            android.text=String (Tap to return to call)
          }
        """
        bridge = AdbGarminBridge(self.engine)
        self.assertEqual(bridge.parse_garmin_notifications(dumpsys), [])

    def test_garmin_message_notification_parsed(self) -> None:
        dumpsys = """
        NotificationRecord(0x01: pkg=com.garmin.android.apps.messenger user=UserHandle{0} id=101
          key=0|com.garmin.android.apps.messenger|101|1|10205
          Notification(channel=messaging pri=1 flags=0x10 actions=2)
          extras={
            android.title=String (FENIX)
            android.text=String (Hello)
          }
        """
        parsed = AdbGarminBridge(self.engine).parse_garmin_notifications(dumpsys)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["sender"], "FENIX")
        self.assertEqual(parsed[0]["body"], "Hello")

    def test_voice_note_wrapper_does_not_trigger_tier_c(self) -> None:
        from wristwire_relay import classify_command_tier, unwrap_voice_note

        raw = "Voice Message: \u201cDownload the Space 92 Barcelona set to my Garmin watch.\u201d"
        inner = unwrap_voice_note(raw)
        self.assertEqual(inner, "Download the Space 92 Barcelona set to my Garmin watch.")
        self.assertEqual(classify_command_tier(inner)[0], "TIER_A")
        self.assertEqual(unwrap_voice_note('Voice Message: "approve AB12CD"'), "approve AB12CD")
        self.assertEqual(unwrap_voice_note("plain text"), "plain text")

    def test_reply_with_non_ascii_is_confirmed_after_send(self) -> None:
        reply = "Clear and 67\u00b0F on the UWS. 5 days to go."
        typed = AdbGarminBridge.escape_input_text(reply).replace("%s", " ")
        bridge = ScriptedAdbBridge(
            self.engine,
            [
                _thread_screen(),
                _thread_screen(),
                _thread_screen(),
                _thread_screen(typed),
                _thread_screen(messages=[typed]),  # the bubble shows what was typed
            ],
        )
        delivered, method, error = bridge.deliver_reply(reply, recipient="fenix")
        self.assertTrue(delivered, error)
        self.assertEqual(method, "usb_adb_ui")

    def test_input_text_escaping(self) -> None:
        escaped = AdbGarminBridge.escape_input_text("All green \u2014 3 nodes up.\nNext run 09:00")
        self.assertEqual(escaped, "All%sgreen%s-%s3%snodes%sup.%sNext%srun%s09:00")

    def test_deliver_reply_sends_into_open_thread(self) -> None:
        reply = "All systems healthy"
        bridge = ScriptedAdbBridge(
            self.engine,
            [
                _thread_screen(),  # _enter_thread: correct thread already open
                _thread_screen(),  # draft check: composer already empty
                _thread_screen(),  # focus probe: composer already focused
                _thread_screen(reply),  # composer echo after typing (also shows send button)
                _thread_screen(messages=[reply]),  # message bubble after send
            ],
        )
        delivered, method, error = bridge.deliver_reply(reply, recipient="fenix")
        self.assertTrue(delivered, error)
        self.assertEqual(method, "usb_adb_ui")
        self.assertEqual(bridge.typed, ["All%ssystems%shealthy"])
        # Send must be a real tap on the composer's send button, not a blind keyevent.
        self.assertIn(("shell", "input", "tap", "660", "1334"), bridge.commands)
        # Awake + unlocked + thread already open: no wake, no relaunch.
        self.assertNotIn(("shell", "input", "keyevent", "KEYCODE_WAKEUP"), bridge.commands)
        self.assertFalse([c for c in bridge.commands if c[:2] == ("shell", "monkey")])

    def test_locked_phone_is_woken_and_unlocked_before_delivery(self) -> None:
        reply = "Wake first"

        class LockedOnce(ScriptedAdbBridge):
            locked_reads = 1

            def _adb_cmd(self, *args: str) -> str:
                self.commands.append(args)
                if args[-1] == "power":
                    return "mWakefulness=Asleep" if self.locked_reads else "mWakefulness=Awake"
                if args[-1] == "trust":
                    if self.locked_reads:
                        self.locked_reads -= 1
                        return "deviceLocked=1"
                    return "deviceLocked=0"
                return ""

        bridge = LockedOnce(
            self.engine,
            [
                _thread_screen(),
                _thread_screen(),
                _thread_screen(),
                _thread_screen(reply),
                _thread_screen(messages=[reply]),
            ],
        )
        delivered, _, error = bridge.deliver_reply(reply, recipient="fenix")
        self.assertTrue(delivered, error)
        self.assertIn(("shell", "input", "keyevent", "KEYCODE_WAKEUP"), bridge.commands)
        self.assertIn(("shell", "wm", "dismiss-keyguard"), bridge.commands)

    def test_thread_not_on_screen_relaunches_messenger(self) -> None:
        reply = "Relaunch"
        bridge = ScriptedAdbBridge(
            self.engine,
            [
                _chat_list_screen(),  # pre-check: thread list, not the thread
                _chat_list_screen(),  # after launch: row visible, tap it
                _thread_screen(),  # thread now open
                _thread_screen(),  # draft check
                _thread_screen(),  # focus probe
                _thread_screen(reply),  # echo after typing
                _thread_screen(messages=[reply]),  # bubble after send
            ],
        )
        delivered, _, error = bridge.deliver_reply(reply, recipient="fenix")
        self.assertTrue(delivered, error)
        self.assertTrue([c for c in bridge.commands if c[:2] == ("shell", "monkey")])

    def test_deliver_reply_focuses_composer_before_typing(self) -> None:
        """Keys follow the focused window; an unfocused composer must be tapped first."""
        reply = "Focus first"
        bridge = ScriptedAdbBridge(
            self.engine,
            [
                _thread_screen(),  # thread open, composer not yet focused
                _thread_screen(),  # draft check: composer empty
                _thread_screen(focused=False),  # focus probe: not focused
                _thread_screen(),  # focused after the tap
                _thread_screen(reply),  # composer echo after typing (also shows send button)
                _thread_screen(messages=[reply]),  # bubble after send
            ],
        )
        delivered, _, error = bridge.deliver_reply(reply, recipient="fenix")
        self.assertTrue(delivered, error)
        self.assertEqual(bridge.typed, ["Focus%sfirst"])
        taps = [c for c in bridge.commands if c[:3] == ("shell", "input", "tap")]
        self.assertEqual(taps[0], ("shell", "input", "tap", "406", "1334"))
        self.assertEqual(taps[-1], ("shell", "input", "tap", "660", "1334"))

    def test_expanded_compose_view_fails_fast_without_touching_the_app(self) -> None:
        """A stretched composer drops injected keys; report it instead of fighting the phone."""
        tall = (
            '<hierarchy>'
            + _ui_node("FENIX", "com.garmin.android.apps.messenger:id/toolbarTitle", (236, 92, 406, 146))
            + _ui_node(
                "",
                "com.garmin.android.apps.messenger:id/newMessageInputEditText",
                (200, 203, 612, 1365),
            )
            + "</hierarchy>"
        )
        bridge = ScriptedAdbBridge(self.engine, [tall, tall, tall])
        delivered, _, error = bridge.deliver_reply("hello", recipient="fenix")
        self.assertFalse(delivered)
        self.assertEqual(error, "compose_view_expanded")
        self.assertEqual(bridge.typed, [])
        self.assertFalse([c for c in bridge.commands if c[:3] == ("shell", "am", "force-stop")])

    def test_deliver_reply_refuses_without_matching_conversation(self) -> None:
        """No thread on screen must fail loudly instead of typing into whatever is focused."""
        bridge = ScriptedAdbBridge(self.engine, [_chat_list_screen() for _ in range(8)])
        bridge._adb_cmd = lambda *args: (
            bridge.commands.append(args) or "mWakefulness=Awake"
        )
        delivered, method, error = bridge.deliver_reply("hello", recipient="fenix")
        self.assertFalse(delivered)
        self.assertEqual(error, "conversation_not_found:fenix")
        self.assertEqual(bridge.typed, [], "must not type when the conversation is not open")

    def test_env_file_loads_from_package_dir_not_cwd(self) -> None:
        """A relative `.env` resolved against the caller's cwd silently lost every setting."""
        import wristwire_relay as relay

        with tempfile.TemporaryDirectory() as pkg_dir, tempfile.TemporaryDirectory() as cwd:
            with open(os.path.join(pkg_dir, ".env"), "w", encoding="utf-8") as fh:
                fh.write("GARMIN_TEST_MARKER=from_package\n")
            with open(os.path.join(cwd, ".env"), "w", encoding="utf-8") as fh:
                fh.write("GARMIN_TEST_MARKER=from_cwd\n")
            os.environ.pop("GARMIN_TEST_MARKER", None)
            previous_cwd = os.getcwd()
            os.chdir(cwd)
            try:
                with unittest.mock.patch.object(relay, "_package_dir", return_value=pkg_dir):
                    relay.load_dotenv_file()
                self.assertEqual(os.environ.get("GARMIN_TEST_MARKER"), "from_package")
            finally:
                os.chdir(previous_cwd)
                os.environ.pop("GARMIN_TEST_MARKER", None)

    def test_input_text_survives_device_shell_special_chars(self) -> None:
        """Quotes/parens/semicolons must reach the device as one quoted script."""
        bridge = AdbGarminBridge(self.engine)
        bridge.adb_bin = "/fake/adb"
        captured: Dict[str, Any] = {}

        def fake_run(cmd, input=None, capture_output=None, timeout=None):  # noqa: A002
            captured["cmd"] = cmd
            captured["input"] = input
            return type("Result", (), {"returncode": 0, "stderr": b""})()

        with unittest.mock.patch("wristwire_relay.subprocess.run", fake_run):
            bridge._input_text("It's (done); echo hi")

        script = captured["input"].decode("utf-8")
        self.assertEqual(captured["cmd"][-1], "shell")
        self.assertTrue(script.startswith("input text '"), script)
        self.assertIn("'\\''", script, "apostrophes must be escaped for the device shell")
        self.assertTrue(script.endswith("'\n"), script)

    def test_default_db_path_is_absolute(self) -> None:
        os.environ.pop("GARMIN_RELAY_DB", None)
        self.assertTrue(os.path.isabs(RelayConfig().db_path))


class SchemaMigrationTests(unittest.TestCase):
    """The harness-neutral rename must upgrade an existing DB in place, not rebuild it."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "legacy.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_legacy_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE watch_requests (
                request_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                garmin_message_id TEXT, sender_id TEXT NOT NULL, source_type TEXT NOT NULL,
                message_text_redacted TEXT NOT NULL, tier TEXT NOT NULL,
                policy_reason TEXT NOT NULL, status TEXT NOT NULL,
                hermes_run_id TEXT, approval_code TEXT, response_text TEXT, error_text TEXT,
                input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
                served_provider TEXT, served_model TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL
            );
            CREATE TABLE pending_approvals (
                approval_code TEXT PRIMARY KEY, request_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                tier TEXT NOT NULL, action_summary TEXT NOT NULL,
                hermes_run_id TEXT, hermes_prompt_id TEXT,
                hermes_execution_generation INTEGER,
                status TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                resolved_at REAL
            );
            INSERT INTO watch_requests (
                request_id, idempotency_key, sender_id, source_type, message_text_redacted,
                tier, policy_reason, status, hermes_run_id, created_at, updated_at
            ) VALUES ('req_legacy', 'k', 'fenix', 'text', 'hello', 'TIER_A', 'r',
                      'completed', 'run_old', 1.0, 2.0);
            """
        )
        conn.commit()
        conn.close()

    def test_legacy_columns_are_renamed_and_data_survives(self) -> None:
        self._write_legacy_db()
        GarminRelayEngine(RelayConfig(db_path=self.db_path, allowed_senders=["fenix"]))
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(watch_requests)")}
        self.assertIn("agent_run_id", columns)
        self.assertNotIn("hermes_run_id", columns)
        self.assertIn("adapter", columns)
        row = conn.execute(
            "SELECT agent_run_id, adapter FROM watch_requests WHERE request_id = 'req_legacy'"
        ).fetchone()
        self.assertEqual(row["agent_run_id"], "run_old")
        self.assertEqual(row["adapter"], "hermes")
        approvals = {r["name"] for r in conn.execute("PRAGMA table_info(pending_approvals)")}
        self.assertIn("agent_prompt_id", approvals)
        self.assertNotIn("hermes_prompt_id", approvals)
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("agent_sessions", tables)
        conn.close()

    def test_adapter_is_recorded_and_session_round_trips(self) -> None:
        engine = GarminRelayEngine(
            RelayConfig(db_path=self.db_path, agent_adapter="claude", allowed_senders=["fenix"])
        )
        code, res = engine.ingest_message({"sender_id": "fenix", "message_text": "status"})
        self.assertIn(int(code), (200, 202))
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT adapter FROM watch_requests WHERE request_id = ?", (res["request_id"],)
        ).fetchone()
        self.assertEqual(row["adapter"], "claude")
        conn.close()
        engine.set_agent_session("FENIX", "claude", "sess-123")
        self.assertEqual(engine.get_agent_session("fenix", "claude"), "sess-123")
        self.assertIsNone(engine.get_agent_session("fenix", "hermes"))


class CliAdapterTests(unittest.TestCase):
    """Claude Code / generic CLI adapter: command shape, JSON parsing, session continuity."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = RelayConfig(
            db_path=os.path.join(self.temp_dir.name, "adapter.db"),
            allowed_senders=["fenix"],
            agent_adapter="claude",
            agent_workdir=self.temp_dir.name,
            synchronous_execution=True,
        )
        self.engine = GarminRelayEngine(self.config)
        self.engine.hermes = _NullHermesClient(self.config)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _claude_json(text="ok", is_error=False, denials=None, session="sess-1", cost=0.01):
        return json.dumps(
            {
                "is_error": is_error,
                "result": text,
                "session_id": session,
                "total_cost_usd": cost,
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 7,
                    "cache_read_input_tokens": 3,
                },
                "permission_denials": denials or [],
            }
        )

    def test_build_adapter_selects_by_name(self) -> None:
        adapter = build_adapter(self.config, self.engine)
        self.assertIsInstance(adapter, CliAdapter)
        self.assertEqual(adapter.profile, "claude")
        generic = build_adapter(
            RelayConfig(db_path=self.config.db_path, agent_adapter="cli"), self.engine
        )
        self.assertEqual(generic.profile, "generic")
        with self.assertRaises(ValueError):
            build_adapter(
                RelayConfig(db_path=self.config.db_path, agent_adapter="nope"), self.engine
            )

    def test_binary_resolution_falls_back_to_user_bin_dirs(self) -> None:
        """systemd gives units a thin PATH; per-user CLIs live in ~/.local/bin."""
        from wristwire_adapters import resolve_binary

        self.assertIsNone(resolve_binary("definitely-not-a-real-agent-xyz"))
        fake_dir = os.path.join(self.temp_dir.name, "bin")
        os.makedirs(fake_dir, exist_ok=True)
        fake = os.path.join(fake_dir, "fakeagent")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho hi\n")
        os.chmod(fake, 0o755)
        self.assertEqual(resolve_binary(fake), fake)
        self.assertIsNone(resolve_binary(os.path.join(fake_dir, "missing")))

    def test_first_message_pins_session_then_resumes(self) -> None:
        adapter = build_adapter(self.config, self.engine)
        first = adapter.build_command("hello", "uuid-1", is_new=True)
        self.assertIn("--session-id", first)
        self.assertIn("uuid-1", first)
        self.assertNotIn("--resume", first)
        second = adapter.build_command("again", "uuid-1", is_new=False)
        self.assertIn("--resume", second)
        self.assertNotIn("--session-id", second)
        # The variadic allowlist flag must stay last or it swallows the flags after it.
        self.assertEqual(first[-len(adapter.allowed_tools) - 1], "--allowedTools")
        self.assertIn("--append-system-prompt", first)
        self.assertIn("Garmin watch", " ".join(first))

    def test_generic_profile_substitutes_message_placeholder(self) -> None:
        config = RelayConfig(
            db_path=self.config.db_path,
            agent_adapter="cli",
            agent_cli_command="ollama run llama3 {message}",
            agent_workdir=self.temp_dir.name,
        )
        adapter = build_adapter(config, self.engine)
        command = adapter.build_command("what is up", "s", is_new=True)
        self.assertEqual(command, ["ollama", "run", "llama3", "what is up"])

    def test_successful_run_parses_envelope_and_persists_session(self) -> None:
        adapter = build_adapter(self.config, self.engine)
        completed = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=self._claude_json("Paris"), stderr=""
        )
        with unittest.mock.patch("wristwire_adapters.subprocess.run", return_value=completed):
            result = adapter.run(
                request_id="req_1", user_text="capital of france", source_type="text",
                tier="TIER_A", sender_id="fenix",
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Paris")
        self.assertEqual(result.usage["total_tokens"], 19)
        self.assertAlmostEqual(result.cost_usd, 0.01)
        self.assertEqual(self.engine.get_agent_session("fenix", "claude"), "sess-1")

        # Second message must resume the stored session.
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=self._claude_json("Lyon"), stderr="")

        with unittest.mock.patch("wristwire_adapters.subprocess.run", fake_run):
            adapter.run(
                request_id="req_2", user_text="and the second city", source_type="text",
                tier="TIER_A", sender_id="fenix",
            )
        self.assertIn("--resume", captured["cmd"])
        self.assertIn("sess-1", captured["cmd"])

    def test_error_envelope_and_missing_binary_are_reported(self) -> None:
        adapter = build_adapter(self.config, self.engine)
        failed = subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout=self._claude_json("Failed to authenticate: OAuth expired", is_error=True), stderr="",
        )
        with unittest.mock.patch("wristwire_adapters.subprocess.run", return_value=failed):
            result = adapter.run(
                request_id="r", user_text="x", source_type="text", tier="TIER_A", sender_id="fenix"
            )
        self.assertFalse(result.ok)
        self.assertIn("OAuth expired", result.error or "")
        with unittest.mock.patch("wristwire_adapters.subprocess.run", side_effect=FileNotFoundError):
            missing = adapter.run(
                request_id="r", user_text="x", source_type="text", tier="TIER_A", sender_id="fenix"
            )
        self.assertFalse(missing.ok)
        self.assertIn("AGENT_CLI_BINARY", missing.error or "")

    def test_relay_records_cli_run_and_queues_reply(self) -> None:
        """End of the loop: watch message -> adapter -> watch_requests row -> outbound reply."""
        calls = []

        def fake_build(config, engine):
            class Fake:
                name = "claude"

                def run(self, **kwargs):
                    calls.append(kwargs)
                    return AdapterResult(
                        ok=True, text="Paris is the capital.", session_id="sess-9",
                        usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                        cost_usd=0.03, provider="anthropic-cli", model="sonnet",
                        denied=["Bash"],
                    )

            return Fake()

        with unittest.mock.patch("wristwire_adapters.build_adapter", fake_build):
            code, res = self.engine.ingest_message(
                {"sender_id": "fenix", "message_text": "capital of france"}
            )
        self.assertIn(int(code), (200, 202))
        conn = sqlite3.connect(self.config.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, adapter, response_text, cost_usd, served_provider, agent_run_id"
            " FROM watch_requests WHERE request_id = ?",
            (res["request_id"],),
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["adapter"], "claude")
        self.assertIn("Paris", row["response_text"])
        self.assertAlmostEqual(row["cost_usd"], 0.03)
        self.assertEqual(row["agent_run_id"], "sess-9")
        reply = conn.execute(
            "SELECT reply_text FROM outbound_replies WHERE request_id = ?", (res["request_id"],)
        ).fetchone()
        self.assertIn("denied: Bash", reply["reply_text"])
        conn.close()
        self.assertEqual(calls[0]["sender_id"], "fenix")

    def test_tier_c_is_gated_before_any_adapter_call(self) -> None:
        """Relay-level approval works for CLI adapters; nothing is dispatched until approved."""
        calls = []

        def fake_build(config, engine):
            class Fake:
                name = "claude"

                def run(self, **kwargs):
                    calls.append(kwargs)
                    return AdapterResult(ok=True, text="sent")

            return Fake()

        with unittest.mock.patch("wristwire_adapters.build_adapter", fake_build):
            code, res = self.engine.ingest_message(
                {"sender_id": "fenix", "message_text": "send money to my client"}
            )
        self.assertEqual(int(code), 202)
        self.assertEqual(res["status"], "needs_approval")
        self.assertIn("approval_code", res)
        self.assertEqual(calls, [], "Tier C must not reach the adapter before approval")


class _NullHermesClient:
    """Stand-in Hermes client so ADB tests never touch the network."""

    def __init__(self, config: RelayConfig) -> None:
        self.config = config

    def create_run(self, *args: Any, **kwargs: Any) -> Tuple[int, Dict[str, Any]]:
        return 503, {"error": "disabled in tests"}

    def get_run(self, *args: Any, **kwargs: Any) -> Tuple[int, Dict[str, Any]]:
        return 404, {"error": "not_found"}

    def stop_run(self, *args: Any, **kwargs: Any) -> Tuple[int, Dict[str, Any]]:
        return 200, {}

    def get_health_detailed(self) -> Tuple[int, Dict[str, Any]]:
        return 200, {"status": "ok"}


if __name__ == "__main__":
    unittest.main(verbosity=2)