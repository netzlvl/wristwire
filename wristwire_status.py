"""Link health for the status page: turn what the relay knows into per-link states.

The relay can see some hops directly (is the phone on USB, did the last reply land in
Garmin Messenger, does the agent answer) and others only by inference (a watch message
arriving proves the watch reached the phone). One hop it cannot see at all: the phone's
Garmin Messenger handing a reply to the watch. That lane is reported as ``unseen``
rather than guessed.

Everything here is a pure function of a ``facts`` dict, so it is testable without a
phone, an agent, or a clock.

States, worst first: ``down``, ``warn``, ``idle`` (nothing to judge yet), ``ok``,
``unseen`` (not observable by WristWire).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

OK, WARN, DOWN, IDLE, UNSEEN = "ok", "warn", "down", "idle", "unseen"
_SEVERITY = {DOWN: 4, WARN: 3, IDLE: 2, OK: 1, UNSEEN: 0}

# A phone that has not been read for this long is treated as disconnected.
STALE_POLL_SECONDS = 90
STALE_HEARTBEAT_SECONDS = 180
SLOW_DELIVERY_SECONDS = 180

DELIVERY_FIXES = {
    "compose_view_expanded": (
        "A draft is stuck in Garmin Messenger's message box. Clear it by hand on the "
        "phone; WristWire retries by itself."
    ),
    "conversation_not_found": (
        "Garmin Messenger has no conversation named “{target}”. Open that thread on "
        "the phone, or set GARMIN_OWNER_NAME to the thread's exact title."
    ),
    "message_input_not_found": (
        "The conversation opened without a message box. Close any pop-up on the phone."
    ),
    "composer_not_focused": (
        "Messenger wouldn't focus the message box. Close any pop-up or keyboard on the phone."
    ),
    "text_not_entered": (
        "Typed text didn't reach Messenger. Close the keyboard or any pop-up on the phone."
    ),
    "send_not_confirmed": (
        "Send was tapped but the reply didn't appear in the thread. Check the thread on the phone."
    ),
    "empty_reply_text": "The agent sent back an empty reply.",
}

_ADB_STATES = {
    "no_adb": (
        "adb isn't installed",
        "Install Android platform-tools, or set GARMIN_ADB_BIN to the adb binary.",
    ),
    "missing": (
        "No phone on USB",
        "Plug the phone in with a data cable and turn on USB debugging.",
    ),
    "unauthorized": (
        "Phone hasn't allowed USB debugging",
        "Unlock the phone and accept the “Allow USB debugging” prompt.",
    ),
    "offline": (
        "Phone shows as offline on USB",
        "Unplug and replug the USB cable, then unlock the phone.",
    ),
    "error": (
        "Couldn't talk to the phone over USB",
        "Run `wristwire doctor` to see the adb error.",
    ),
}


def ago(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    seconds = max(0.0, float(seconds))
    if seconds < 10:
        return "just now"
    # Non-breaking spaces keep "4 min" together when a caption wraps.
    if seconds < 60:
        return f"{int(seconds)}\u00a0s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}\u00a0min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}\u00a0h ago"
    days = int(seconds // 86400)
    return "1\u00a0day ago" if days == 1 else f"{days}\u00a0days ago"


def delivery_fix(error: Optional[str]) -> str:
    raw = (error or "").strip()
    key, _, target = raw.partition(":")
    template = DELIVERY_FIXES.get(key)
    if template:
        return template.format(target=target or "your owner contact")
    return "Check the phone's screen. The relay log names the failing step."


def _lane(state: str, text: str) -> Dict[str, str]:
    return {"state": state, "text": text}


def _worst(*states: str) -> str:
    return max(states, key=lambda s: _SEVERITY.get(s, 0))


def compute_links(f: Dict[str, Any]) -> Dict[str, Any]:
    now = float(f["now"])
    problems: List[Dict[str, str]] = []

    def since(ts: Optional[float]) -> Optional[float]:
        return None if not ts else now - float(ts)

    def problem(state: str, title: str, fix: str) -> None:
        problems.append({"state": state, "title": title, "fix": fix})

    transport = f.get("transport") or "none"
    bridge = f.get("bridge") or {}
    heartbeat = f.get("heartbeat") or None
    delivery = f.get("delivery") or {}
    agent = f.get("agent") or {}
    kill_switch = bool(f.get("kill_switch"))

    # -- phone <-> relay -------------------------------------------------------
    messenger_running: Optional[bool] = None
    phone_facts: List[str] = []
    if transport == "usb_adb":
        via = "USB (adb)"
        state = bridge.get("state")
        if state in _ADB_STATES:
            title, fix = _ADB_STATES[state]
            detail = bridge.get("state_detail")
            phone_in = _lane(DOWN, title)
            problem(DOWN, title, fix + (f" ({detail})" if detail and state == "error" else ""))
        elif state == "device":
            poll_age = since(bridge.get("last_poll_ok_at"))
            err_age = since(bridge.get("last_poll_error_at"))
            if poll_age is not None and poll_age <= STALE_POLL_SECONDS and (
                err_age is None or err_age > poll_age
            ):
                phone_in = _lane(OK, f"Reading notifications, checked {ago(poll_age)}")
            elif bridge.get("last_poll_error"):
                phone_in = _lane(WARN, f"Notification reads failing: {bridge['last_poll_error']}")
                problem(
                    WARN,
                    "The relay can't read the phone's notifications",
                    "Unlock the phone once. If it keeps failing, run `wristwire doctor`.",
                )
            else:
                phone_in = _lane(IDLE, "Connected, waiting for the first read")
        else:
            phone_in = _lane(IDLE, "Checking the phone…")
        messenger_running = bridge.get("messenger_running")
        if bridge.get("model"):
            phone_facts.append(str(bridge["model"]))
        if bridge.get("battery_pct") is not None:
            charge = ", charging" if bridge.get("charging") else ", not charging"
            phone_facts.append(f"{bridge['battery_pct']}% battery{charge}")
        if bridge.get("awake") is not None:
            screen = "Screen on" if bridge.get("awake") else "Screen off"
            if bridge.get("locked"):
                screen += ", locked"
            phone_facts.append(screen)
    elif transport == "http":
        via = "Network (phone app)"
        hb_age = since((heartbeat or {}).get("last_seen_at"))
        if hb_age is not None and hb_age <= STALE_HEARTBEAT_SECONDS:
            phone_in = _lane(OK, f"Phone app checked in {ago(hb_age)}")
        else:
            text = (
                "The phone app hasn't checked in yet"
                if hb_age is None
                else f"No word from the phone app for {ago(hb_age).replace(' ago', '')}"
            )
            phone_in = _lane(DOWN, text)
            problem(
                DOWN,
                text,
                "Open the companion app on the phone and make sure it can reach the relay.",
            )
        if heartbeat:
            messenger_running = heartbeat.get("garmin_app_running")
            if heartbeat.get("battery_pct") is not None:
                charge = ", charging" if heartbeat.get("is_charging") else ", not charging"
                phone_facts.append(f"{heartbeat['battery_pct']}% battery{charge}")
    else:
        via = "Not connected"
        phone_in = _lane(DOWN, "No phone link is running")
        problem(
            DOWN,
            "No phone link is running",
            "Start the relay with `wristwire adb-bridge` for a USB phone, or point the "
            "phone app at `wristwire serve`.",
        )

    if messenger_running is True:
        phone_facts.append("Garmin Messenger running")
    elif messenger_running is False:
        phone_facts.append("Garmin Messenger not running")

    # relay -> phone: did replies land in Garmin Messenger?
    last_delivered_age = since(delivery.get("last_delivered_at"))
    failure = delivery.get("last_failure") or None
    pending = int(delivery.get("pending") or 0)
    oldest_pending_age = since(delivery.get("oldest_pending_at"))
    if phone_in["state"] == DOWN:
        phone_out = _lane(
            DOWN,
            f"{pending} repl{'y' if pending == 1 else 'ies'} waiting for the phone"
            if pending
            else "Can't deliver until the phone is connected",
        )
    elif failure and (
        delivery.get("last_delivered_at") is None
        or float(failure["at"]) > float(delivery["last_delivered_at"])
    ):
        err = str(failure.get("error") or "delivery failed")
        phone_out = _lane(DOWN, f"Last reply failed: {err.split(':')[0].replace('_', ' ')}")
        problem(DOWN, "Replies aren't reaching Garmin Messenger", delivery_fix(err))
    elif pending and oldest_pending_age is not None and oldest_pending_age > SLOW_DELIVERY_SECONDS:
        phone_out = _lane(
            WARN, f"{pending} repl{'y' if pending == 1 else 'ies'} waiting {ago(oldest_pending_age).replace(' ago', '')}"
        )
        problem(
            WARN,
            "Replies are queued but not going out",
            "Check that the relay is running with the phone link (`wristwire adb-bridge`).",
        )
    elif pending:
        phone_out = _lane(OK, "Delivering a reply now")
    elif last_delivered_age is not None:
        phone_out = _lane(OK, f"Last reply delivered {ago(last_delivered_age)}")
    else:
        phone_out = _lane(IDLE, "No replies sent yet")

    # -- watch <-> phone (through Garmin's network) -------------------------------
    last_in = f.get("last_inbound") or None
    last_in_age = since((last_in or {}).get("at"))
    if messenger_running is False:
        watch_in = _lane(DOWN, "Garmin Messenger isn't running on the phone")
        problem(
            DOWN,
            "Garmin Messenger isn't running on the phone",
            "Open Garmin Messenger on the phone and sign in. It needs to stay running in the background.",
        )
    elif phone_in["state"] == DOWN and last_in_age is None:
        watch_in = _lane(IDLE, "Unknown until the phone is connected")
    elif last_in_age is not None:
        kind = " (voice note)" if (last_in or {}).get("source_type") == "voice_note" else ""
        watch_in = _lane(OK, f"Last watch message {ago(last_in_age)}{kind}")
    else:
        watch_in = _lane(IDLE, "No watch messages yet. Send one from your watch to test it.")
    watch_out = _lane(
        UNSEEN,
        "Not visible to WristWire. Always On LTE makes the watch check often.",
    )

    # -- relay <-> agent ------------------------------------------------------------
    agent_label = str(agent.get("label") or "Agent")
    if kill_switch:
        agent_in = _lane(DOWN, "Paused by the kill switch")
        agent_out = _lane(DOWN, "Paused by the kill switch")
        problem(
            DOWN,
            "The kill switch is on, so watch messages aren't sent to the agent",
            "Turn it off: POST /v1/garmin/control/killswitch with {\"enabled\": false}.",
        )
    elif agent.get("ok"):
        latency = agent.get("latency_ms")
        agent_in = _lane(
            OK, f"{agent_label} is answering" + (f" ({int(latency)}\u00a0ms)" if latency is not None else "")
        )
        run_fail = f.get("last_run_failure") or None
        last_answer_age = since(f.get("last_answer_at"))
        if run_fail and (
            f.get("last_answer_at") is None or float(run_fail["at"]) > float(f["last_answer_at"])
        ):
            reason = str(run_fail.get("error") or "run failed")[:80]
            agent_out = _lane(WARN, f"Last run failed: {reason}")
            problem(
                WARN,
                f"{agent_label}'s last run failed",
                f"{reason}. Check {agent_label}'s logs; the next message will try again.",
            )
        elif last_answer_age is not None:
            agent_out = _lane(OK, f"Last answer {ago(last_answer_age)}")
        else:
            agent_out = _lane(IDLE, "No answers yet")
    else:
        detail = str(agent.get("detail") or "not reachable")
        agent_in = _lane(DOWN, f"{agent_label} isn't reachable")
        agent_out = _lane(DOWN, f"{agent_label} isn't reachable")
        problem(DOWN, f"The relay can't reach {agent_label}", str(agent.get("fix") or detail))

    # -- nodes ----------------------------------------------------------------------
    relay = f.get("relay") or {}
    relay_facts = []
    if relay.get("started_at"):
        relay_facts.append(f"Up {ago(since(relay['started_at'])).replace(' ago', '')}")
    if relay.get("version"):
        relay_facts.append(f"Version {relay['version']}")
    agent_facts = [f"Adapter: {agent.get('adapter') or 'hermes'}"]
    if f.get("last_model"):
        agent_facts.append(f"Model: {f['last_model']}")

    nodes = [
        {
            "id": "watch",
            "label": "Garmin watch",
            "state": watch_in["state"] if watch_in["state"] != IDLE else IDLE,
            "facts": [f"Last heard {ago(last_in_age)}"] if last_in_age is not None else ["Not heard from yet"],
        },
        {
            "id": "phone",
            "label": "Agent phone",
            "state": _worst(phone_in["state"], DOWN if messenger_running is False else OK),
            "facts": phone_facts,
        },
        {
            "id": "relay",
            "label": "WristWire relay",
            "state": DOWN if kill_switch else OK,
            "facts": relay_facts + (["Kill switch on"] if kill_switch else []),
        },
        {
            "id": "agent",
            "label": agent_label,
            "state": agent_in["state"],
            "facts": agent_facts,
        },
    ]
    links = [
        {"id": "watch-phone", "via": "Garmin Messenger", "inbound": watch_in, "outbound": watch_out},
        {"id": "phone-relay", "via": via, "inbound": phone_in, "outbound": phone_out},
        {
            "id": "relay-agent",
            "via": str(agent.get("via") or "Agent API"),
            "inbound": agent_in,
            "outbound": agent_out,
        },
    ]

    lane_states = [lane["state"] for link in links for lane in (link["inbound"], link["outbound"])]
    worst = _worst(*lane_states)
    if worst == DOWN or worst == WARN:
        first = next(p for p in problems if p["state"] == worst) if problems else None
        verdict = {"state": worst, "text": first["title"] if first else "Something needs attention"}
    elif IDLE in lane_states:
        verdict = {"state": OK, "text": "Connected. Waiting for traffic to confirm every link."}
    else:
        verdict = {"state": OK, "text": "Everything is connected."}

    problems.sort(key=lambda p: -_SEVERITY[p["state"]])
    return {"verdict": verdict, "nodes": nodes, "links": links, "problems": problems, "now": now}


def parse_probe_output(text: str) -> Dict[str, Any]:
    """Parse the one-shot device probe script (sections separated by ``@@``)."""
    parts = (text or "").split("@@")
    parts += [""] * (5 - len(parts))
    model, battery, power, trust, pid = (p.strip() for p in parts[:5])
    out: Dict[str, Any] = {"model": model.splitlines()[0].strip() if model else None}
    level = None
    charging = False
    for line in battery.splitlines():
        key, _, value = line.strip().partition(":")
        value = value.strip().lower()
        if key == "level":
            try:
                level = int(value)
            except ValueError:
                level = None
        elif key.endswith("powered") and value == "true":
            charging = True
    out["battery_pct"] = level
    out["charging"] = charging if level is not None else None
    out["awake"] = ("mWakefulness=Awake" in power) if power else None
    out["locked"] = ("deviceLocked=1" in trust) if trust else None
    first_pid = pid.split()[0] if pid else ""
    out["messenger_running"] = first_pid.isdigit() if pid else None
    return out


def classify_adb_state(returncode: int, stdout: str, stderr: str) -> Dict[str, Optional[str]]:
    """Map `adb get-state` output to a state the status page understands."""
    out = (stdout or "").strip()
    err = (stderr or "").strip()
    if returncode == 0 and out in ("device", "recovery", "sideload", "bootloader"):
        return {"state": "device" if out == "device" else "offline", "detail": None if out == "device" else out}
    text = f"{out} {err}".lower()
    if "unauthorized" in text:
        return {"state": "unauthorized", "detail": None}
    if "offline" in text:
        return {"state": "offline", "detail": None}
    if "no devices" in text or "not found" in text:
        return {"state": "missing", "detail": None}
    return {"state": "error", "detail": (err or out)[:160] or f"exit {returncode}"}
