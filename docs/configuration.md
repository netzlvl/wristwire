# Configuration

WristWire reads settings from the environment first, then from these `.env` files in order. When several files set the same value, the earliest one wins:

1. `.env` next to `wristwire_relay.py` (your clone), even when the relay is started from another folder or by systemd
2. `.env` in the folder you started it from
3. `~/.config/wristwire/.env`
4. `~/.hermes/garmin.env`, then `~/.hermes/.env`

Because Hermes's own `~/.hermes/.env` is read last, the relay picks up Hermes's `API_SERVER_KEY` without you copying it.

`./deploy.sh init-repo` writes the first one with fresh random secrets. Never commit it; `.gitignore` already excludes it.

## Who and where

| Setting | Default | What it does |
|---|---|---|
| `GARMIN_OWNER_NAME` | `owner` | Title of your conversation in the phone's Garmin Messenger. Replies are typed into this thread. |
| `GARMIN_ALLOWED_SENDERS` | `owner,watch-owner` | Comma-separated senders allowed to talk to the agent. Garmin Messenger shows the sender as the conversation title, so this is usually the same name as `GARMIN_OWNER_NAME`. Case doesn't matter. |
| `GARMIN_RELAY_HOST` | `127.0.0.1` | Address the relay listens on. Keep it on loopback or a VPN address; never expose it to the internet. |
| `GARMIN_RELAY_PORT` | `8788` | Port for the relay, its API and the status page. |
| `GARMIN_RELAY_ALLOWED_HOSTS` | | Extra host names the relay answers to, comma-separated. Requests are accepted for `localhost`, `127.0.0.1`, `::1` and the listen address (and any plain IP address when listening on `0.0.0.0`); add a name here if the phone app reaches the relay by a DNS name. Other names are refused so web pages can't reach the relay through DNS tricks. |
| `GARMIN_RELAY_DB` | `~/.local/state/wristwire/relay.db` | The queue, approvals and audit trail. |
| `GARMIN_ADB_BIN` | found automatically | Full path to `adb`. Under WSL2, Windows' `adb.exe` is found by itself. |

## Secrets

| Setting | What it does |
|---|---|
| `GARMIN_RELAY_ADMIN_KEY` | Required. Signs in to the relay's admin API, issues status-page sign-in links, and lets the Hermes plugin talk to the relay. The relay won't start without a real one. Status-page sessions last 12 hours; restarting the relay signs every page out. |
| `GARMIN_BRIDGE_SECRET` | Shared HMAC secret for the HTTP transport. The phone signs every request with it. Without a real one, the HTTP transport stays off; the USB bridge doesn't need it. |
| `GARMIN_BRIDGE_DEVICE_ID` | Name the phone uses in those signed requests. Default `android-garmin-agent-01`. |

## Agent

| Setting | Default | What it does |
|---|---|---|
| `AGENT_ADAPTER` | `hermes` | `hermes`, `claude` or `cli`. |
| `HERMES_API_URL` | `http://127.0.0.1:8642` | Hermes API server. |
| `API_SERVER_KEY` | read from `~/.hermes/.env` | Hermes API key. Only set it here if Hermes keeps its `.env` elsewhere. |
| `HERMES_GARMIN_SESSION_ID` | `garmin-watch-owner-session` | Base name of the Hermes session for watch messages. |
| `HERMES_GARMIN_SESSION_ROTATION` | `daily` | `daily` starts a fresh session each day (`<base>-YYYY-MM-DD`) so runs stay quick; `never` keeps one session. |
| `HERMES_GARMIN_SESSION_KEY` | `agent:main:garmin:dm:owner` | Session key header sent to Hermes. |
| `AGENT_CLI_BINARY` | `claude` | Command for the `claude` adapter. Use a full path under systemd. |
| `AGENT_CLI_COMMAND` | | For the `cli` adapter: an argv template with a `{message}` placeholder, e.g. `ollama run llama3 {message}`. |
| `AGENT_CLI_MODEL` | | Optional model name passed to `claude`. |
| `AGENT_CLI_ALLOWED_TOOLS` | `Read,Grep,Glob` | Tools `claude -p` may use. Keep it as narrow as the job allows. |
| `AGENT_CLI_PERMISSION_MODE` | `default` | `--permission-mode` for `claude -p`. |
| `AGENT_CLI_TIMEOUT_SECONDS` | `180` | Longest a CLI agent run may take. |
| `AGENT_WORKDIR` | `~/.local/state/wristwire/workspace` | Working folder for CLI agents, away from any real checkout. |

## Behaviour

| Setting | Default | What it does |
|---|---|---|
| `GARMIN_ACK_AFTER_SECONDS` | `0` (off) | Send "Working on it..." when a run takes longer than this. It costs one extra delivery, so leave it off unless you want the receipt. A note still waiting when the answer is ready is dropped. |

## Hermes plugin settings

In `~/.hermes/config.yaml`, all optional:

```yaml
plugins:
  entries:
    wristwire:
      settings:
        relay_url: http://127.0.0.1:8788
```

The plugin finds the admin key in the relay's `.env` on the same machine. If the relay runs elsewhere, set `GARMIN_RELAY_ADMIN_KEY` in Hermes's `.env` (or `admin_key` under `settings`, though `config.yaml` is the file people paste into bug reports).
