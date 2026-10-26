#!/usr/bin/env bash
# =============================================================================
# WristWire helper script
# https://github.com/netzlvl/wristwire
# =============================================================================
# Usage: ./deploy.sh <command> [args]
#
#   init-repo [dir]      Install the `wristwire` command into ~/.local/bin and write a
#                        .env with fresh secrets. Never overwrites existing files.
#   install-cli [dir]    Only install the `wristwire` command.
#   install-service      Install and start the background service (systemd, Linux/WSL2).
#   pull                 Update this clone from GitHub (fast-forward only).
#   send "<message>"     Queue a message for the watch through the running relay.
#   print-tailscale      Print a Tailscale ACL that lets the phone reach only the relay.
#
# Phone-side helpers for the HTTP transport (Tasker or a companion app), each signs
# its request with GARMIN_BRIDGE_SECRET:
#   tasker-inspect | tasker-ingest | tasker-poll | tasker-ack | tasker-heartbeat
# =============================================================================

set -euo pipefail

MODE="${1:-help}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Read the relay's .env (without executing it) so these helpers use the same secrets.
# Values already exported in the shell win.
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  while IFS='=' read -r key value; do
    [[ "$key" =~ ^(GARMIN_[A-Z_]+|API_SERVER_KEY)$ ]] || continue
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    if [[ -z "${!key:-}" ]]; then export "$key=$value"; fi
  done < "${SCRIPT_DIR}/.env"
fi

REPO_URL="${GARMIN_GIT_REPO_URL:-https://github.com/netzlvl/wristwire.git}"
RELAY_URL="${GARMIN_RELAY_URL:-http://127.0.0.1:8788}"
DEVICE_ID="${GARMIN_BRIDGE_DEVICE_ID:-android-garmin-agent-01}"
DEVICE_SECRET="${GARMIN_BRIDGE_SECRET:-change-me-garmin-bridge-hmac-secret}"
ADMIN_KEY="${GARMIN_RELAY_ADMIN_KEY:-${API_SERVER_KEY:-change-me-garmin-relay-admin-key}}"

install_cli_binary() {
  local target_dir
  target_dir="$(cd "${1:-.}" && pwd)"
  chmod +x "${target_dir}/wristwire_relay.py" "${target_dir}/deploy.sh"
  local bin_dir="${HOME}/.local/bin"
  if mkdir -p "$bin_dir" 2>/dev/null && ln -sf "${target_dir}/wristwire_relay.py" "${bin_dir}/wristwire" 2>/dev/null; then
    if [[ ":$PATH:" != *":${bin_dir}:"* ]]; then
      if [[ -w "${HOME}" ]] && ! grep -q '\.local/bin' "${HOME}/.bashrc" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "${HOME}/.bashrc" || true
      fi
      export PATH="${bin_dir}:${PATH}"
    fi
    echo "Installed CLI: ${bin_dir}/wristwire -> ${target_dir}/wristwire_relay.py"
  fi
}

# Write stdin to a file only if it does not exist yet. In a clone these files are tracked,
# so overwriting them would leave local changes behind and break `wristwire update`.
write_if_missing() {
  local path="$1"
  if [[ -e "$path" ]]; then
    cat >/dev/null
  else
    cat >"$path"
  fi
}

scaffold_repo_files() {
  local target_dir="${1:-.}"
  mkdir -p "$target_dir"

  write_if_missing "${target_dir}/.gitignore" <<'EOF'
# Local secrets and runtime databases (never commit!)
.env
*.env
!*.env.example
*.db
*.db-shm
*.db-wal
.venv/
__pycache__/
*.py[cod]
*.egg-info/
/dist/
/build/
.pytest_cache/
EOF

  write_if_missing "${target_dir}/.env.example" <<'EOF'
GARMIN_RELAY_HOST=127.0.0.1
GARMIN_RELAY_PORT=8788
GARMIN_BRIDGE_DEVICE_ID=android-garmin-agent-01
GARMIN_BRIDGE_SECRET=replace-with-random-hmac-secret
GARMIN_RELAY_ADMIN_KEY=replace-with-random-admin-secret
HERMES_API_URL=http://127.0.0.1:8642
# API_SERVER_KEY is read from ~/.hermes/.env; set it here only if Hermes keeps it elsewhere
# API_SERVER_KEY=
GARMIN_ALLOWED_SENDERS=owner,watch-owner
GARMIN_OWNER_NAME=owner
# Optional: queue a "Working on it..." note after N seconds (0 = off, the default; it costs one extra delivery)
# GARMIN_ACK_AFTER_SECONDS=0
# Optional: daily (default) starts a fresh Hermes session each day; never = one session forever
# HERMES_GARMIN_SESSION_ROTATION=daily
EOF

  write_if_missing "${target_dir}/pyproject.toml" <<'EOF'
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "wristwire"
version = "0.2.0"
description = "Your direct line to AI: talk to any agent from your Garmin watch through Garmin Messenger"
readme = "README.md"
requires-python = ">=3.10"
license = {text = "MIT"}
dependencies = []

[project.urls]
Homepage = "https://github.com/netzlvl/wristwire"
Repository = "https://github.com/netzlvl/wristwire.git"
Issues = "https://github.com/netzlvl/wristwire/issues"

[project.scripts]
wristwire = "wristwire_relay:main"

[tool.setuptools]
py-modules = ["wristwire_relay", "wristwire_adapters", "wristwire_status"]
EOF

  (cd "$target_dir" && python3 ./wristwire_relay.py init)
  install_cli_binary "$target_dir"
}

sign_and_request() {
  local method="$1"
  local path="$2"
  local body="${3:-}"

  local ts
  ts="$(python3 -c 'import time; print(f"{time.time():.3f}")')"
  local nonce
  nonce="n_$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
  local body_hash
  body_hash="$(printf '%s' "$body" | sha256sum | awk '{print $1}')"
  local canonical="${ts}.${nonce}.${method}.${path}.${body_hash}"
  local sig
  sig="$(python3 -c 'import hmac, hashlib, sys; print(hmac.new(sys.argv[1].encode(), sys.argv[2].encode(), hashlib.sha256).hexdigest())' "$DEVICE_SECRET" "$canonical")"

  if [[ "$method" == "POST" ]]; then
    curl -sS -X POST "${RELAY_URL}${path}" \
      -H "X-Device-Id: ${DEVICE_ID}" \
      -H "X-Timestamp: ${ts}" \
      -H "X-Nonce: ${nonce}" \
      -H "X-Signature: ${sig}" \
      -H "Content-Type: application/json" \
      --data-raw "$body"
  else
    curl -sS -X GET "${RELAY_URL}${path}" \
      -H "X-Device-Id: ${DEVICE_ID}" \
      -H "X-Timestamp: ${ts}" \
      -H "X-Nonce: ${nonce}" \
      -H "X-Signature: ${sig}"
  fi
}

case "$MODE" in
  install-cli)
    install_cli_binary "${2:-.}"
    ;;

  install-service)
    # Works from any folder: the unit file is found next to this script.
    if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
      echo "systemd isn't available for your user. On WSL2, add 'systemd=true' under [boot] in"
      echo "/etc/wsl.conf, run 'wsl --shutdown' from Windows, reopen WSL and try again."
      echo "Or run the relay in a terminal instead: wristwire adb-bridge"
      exit 1
    fi
    install_cli_binary "$SCRIPT_DIR" >/dev/null
    mkdir -p "${HOME}/.config/systemd/user"
    cp "${SCRIPT_DIR}/systemd/wristwire.service" "${HOME}/.config/systemd/user/wristwire.service"
    systemctl --user daemon-reload
    systemctl --user enable --now wristwire.service
    sleep 2
    if systemctl --user is-active --quiet wristwire.service; then
      echo "WristWire is running. Logs: ~/.local/state/wristwire/relay.log"
      echo "Next: run 'wristwire dashboard' and open the link it prints."
    else
      echo "The service didn't start. Last log lines:"
      tail -n 20 "${HOME}/.local/state/wristwire/relay.log" 2>/dev/null || systemctl --user status wristwire.service --no-pager
      exit 1
    fi
    ;;

  init-repo)
    TARGET_DIR="${2:-.}"
    scaffold_repo_files "$TARGET_DIR"
    echo "Open-source repo scaffolded and CLI installed."
    ;;

  pull)
    if [[ ! -d ".git" ]]; then
      echo "Not a git repository yet. Run: git clone ${REPO_URL}"
      exit 1
    fi
    git pull --ff-only origin main
    install_cli_binary "."
    echo "Updated from ${REPO_URL}"
    ;;

  send)
    MSG="${2:?Message text required}"
    RECIPIENT="${3:-owner}"
    PAYLOAD="$(python3 -c 'import json, sys; print(json.dumps({"message": sys.argv[1], "recipient": sys.argv[2]}))' "$MSG" "$RECIPIENT")"
    curl -sS -X POST "${RELAY_URL}/v1/garmin/send" \
      -H "Authorization: Bearer ${ADMIN_KEY}" \
      -H "Content-Type: application/json" \
      --data-raw "$PAYLOAD"
    ;;

  tasker-inspect)
    SENDER="${2:-owner}"
    TEXT="${3:-}"
    BIGTEXT="${4:-}"
    REPLY_ACTION="${5:-}"
    NKEY="${6:-}"
    HAS_TEXT="false"
    [[ -n "$TEXT" || -n "$BIGTEXT" ]] && HAS_TEXT="true"
    HAS_REMOTE_INPUT="false"
    [[ -n "$REPLY_ACTION" && "$REPLY_ACTION" != "%anreplyaction" ]] && HAS_REMOTE_INPUT="true"

    PAYLOAD="$(python3 -c '
import json, sys
sender, has_text, has_ri, nkey, body = sys.argv[1:6]
has_voice = any(k in body.lower() for k in ("voice", "transcript", "audio", "🎤"))
print(json.dumps({
    "package_name": "com.garmin.android.apps.messenger",
    "sender": sender,
    "has_text": has_text == "true",
    "has_voice_transcript": has_voice,
    "has_remote_input": has_ri == "true",
    "action_titles": ["Reply"] if has_ri == "true" else [],
    "notification_key": nkey,
    "screen_locked": True,
    "lte_mode": "always_on",
    "notes": "Tasker AutoNotification gate inspection"
}))
' "$SENDER" "$HAS_TEXT" "$HAS_REMOTE_INPUT" "$NKEY" "${BIGTEXT:-$TEXT}")"
    sign_and_request "POST" "/v1/garmin/inspect" "$PAYLOAD"
    ;;

  tasker-ingest)
    SENDER="${2:-owner}"
    BODY="${3:-}"
    MSG_ID="${4:-}"
    SOURCE_TYPE="${5:-text}"
    PAYLOAD="$(python3 -c '
import json, sys, time
sender, body, msg_id, stype = sys.argv[1:5]
print(json.dumps({
    "garmin_message_id": msg_id,
    "sender_id": sender,
    "source_type": stype,
    "message_text": body,
    "transcript_text": body if stype == "voice_note" else "",
    "message_timestamp": time.time()
}))
' "$SENDER" "$BODY" "$MSG_ID" "$SOURCE_TYPE")"
    sign_and_request "POST" "/v1/garmin/ingest" "$PAYLOAD"
    ;;

  tasker-poll)
    sign_and_request "GET" "/v1/garmin/outbox" ""
    ;;

  tasker-ack)
    REPLY_ID="${2:?reply_id required}"
    DELIVERED="${3:-true}"
    METHOD="${4:-remote_input}"
    PAYLOAD="$(python3 -c '
import json, sys
print(json.dumps({
    "delivered": sys.argv[1].lower() == "true",
    "delivery_method": sys.argv[2]
}))
' "$DELIVERED" "$METHOD")"
    sign_and_request "POST" "/v1/garmin/outbox/${REPLY_ID}/ack" "$PAYLOAD"
    ;;

  tasker-heartbeat)
    BATT="${2:-100}"
    CHARGING="${3:-true}"
    PAYLOAD="$(python3 -c '
import json, sys
print(json.dumps({
    "battery_pct": int(sys.argv[1]),
    "is_charging": sys.argv[2].lower() == "true",
    "wifi_connected": True,
    "screen_locked": True,
    "garmin_app_running": True,
    "bridge_mode": "tasker_autonotification"
}))
' "$BATT" "$CHARGING")"
    sign_and_request "POST" "/v1/garmin/heartbeat" "$PAYLOAD"
    ;;

  print-tailscale)
    cat <<'EOF'
// Tailscale ACL snippet:
// Restrict the dedicated Android Agent phone (tag:agent-phone) so it can ONLY
// initiate outbound connections to the Hermes Garmin Relay port (8788) on your relay host.
{
  "tagOwners": {
    "tag:agent-phone": ["autogroup:admin"],
    "tag:relay-host": ["autogroup:admin"]
  },
  "acls": [
    {
      "action": "accept",
      "src":    ["tag:agent-phone"],
      "dst":    ["tag:relay-host:8788"]
    }
  ]
}
EOF
    ;;

  *)
    echo "Usage: $0 {init-repo|install-cli|install-service|pull|send|print-tailscale|tasker-inspect|tasker-ingest|tasker-poll|tasker-ack|tasker-heartbeat}"
    ;;
esac