# WristWire plugin for Hermes

Makes your Garmin watch a place Hermes can talk to. It adds three tools and a dashboard tab:

| | What it does |
|---|---|
| `watch_send` | Queue a message for your watch. Delivery happens in the background, so the result reports the queue ids, not "delivered". |
| `watch_inbox` | What you sent from the watch, how each message was answered, and how long it took. |
| `watch_status` | Relay and phone health, queue depth, pending approval codes, failed deliveries. |
| **WristWire tab** | A live picture of the watch, phone, relay and Hermes links, with a button that sends a test message to your watch. |

The plugin is thin on purpose. The relay (`wristwire adb-bridge`, normally the systemd service) owns the phone link, the queue, approval codes and the audit trail. The plugin holds no phone secret and opens no ports. If no relay answers, the tools hide themselves instead of failing, and the tab says the relay isn't running.

## Install

```bash
# 1. The relay must be running; it owns the phone. From your wristwire folder:
./deploy.sh install-service

# 2. Link the plugin from your clone, then enable it.
ln -sfn ~/wristwire/plugin ~/.hermes/plugins/wristwire
hermes plugins enable wristwire

# 3. Restart the Hermes gateway and dashboard so they load it.
```

Once the repository is public, `hermes plugins install https://github.com/netzlvl/wristwire/tree/main/plugin --enable` works too.

After the restart, WristWire shows on the dashboard's **Plugins** page and as a **WristWire** tab. The tools reach Hermes from the next session onward; a session that's already open keeps its tool list.

To check the plugin: `hermes plugins doctor ~/wristwire/plugin`.

## Settings

Nothing is required when Hermes and the relay share a machine. The plugin looks for the relay's admin key in this order:

1. `admin_key` under the plugin's settings in `config.yaml`
2. `GARMIN_RELAY_ADMIN_KEY` in the environment
3. the `.env` in the clone this plugin lives in, then `~/.config/wristwire/.env`, `~/wristwire/.env` and `~/.hermes/garmin.env`

The relay's address can be changed in `config.yaml`:

```yaml
plugins:
  entries:
    wristwire:
      settings:
        relay_url: http://127.0.0.1:8788
```

If the relay runs on another machine, set `GARMIN_RELAY_ADMIN_KEY` in Hermes's `.env` rather than in `config.yaml`, which is the file people paste into bug reports.

## Files

| Path | What it is |
|---|---|
| `plugin.yaml`, `__init__.py`, `tools.py` | The three tools. |
| `relay_client.py` | Small HTTP client for the relay, shared by the tools and the tab. |
| `dashboard/manifest.json`, `dashboard/dist/` | The WristWire tab. The same script is served by the relay at `/status`. |
| `dashboard/plugin_api.py` | The tab's backend, mounted at `/api/plugins/wristwire/`, behind the dashboard's login. |
