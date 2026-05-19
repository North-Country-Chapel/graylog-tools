# Graylog → Zabbix Receiver with Low-Level Discovery (LLD)

Setup guide for the HTTP receiver that bridges Graylog event notifications to Zabbix trapper items, with automatic discovery of monitored scripts.

---

## Architecture Overview

```
Scripts → GELF → Graylog → HTTP notification → Receiver → Zabbix trapper → Items/Triggers
                                                         ↑
                                            Zabbix HTTP agent polls 
                                            /discover/{host} to auto-create  
                                            items and triggers per script

Unmapped script → Receiver → Zabbix trapper (script.unknown) → ncc-scripts-receiver
```

The receiver is a Flask app running on the Graylog VM. It has three endpoints:

- **POST /zabbix** — receives Graylog HTTP notification payloads and forwards them to Zabbix as trapper item values. Events from scripts not in `SCRIPT_HOST_MAP` are forwarded to the `ncc-scripts-receiver` host as `script.unknown` traps.
- **GET /discover/{host}** — returns Zabbix LLD JSON listing which scripts belong to a given Zabbix host.
- **GET /health** — returns `{"status": "ok"}`.

---

## Infrastructure

| Component | Address | Notes |
|---|---|---|
| Graylog VM | 10.10.0.77 | Ubuntu 24.04, SSH user `smoo` |
| Zabbix server | zabbix.northcountrychapel.com (10.10.0.76) | |
| Receiver | 10.10.0.77:9999 | Runs on the Graylog VM |
| Zabbix trapper port | 10051 | |

---

## File Layout on Graylog VM

```
/opt/scripts/
└── docs/
    └── receiver-setup.md
├── logger.py                          ← shared logging module (do not modify)
└── receiver/
    ├── .venv/                        
    └── graylog_zabbix_receiver.py    
```

---

## Dependencies

Installed inside the venv at `/opt/scripts/receiver/.venv`:

```bash
source /opt/scripts/receiver/.venv/bin/activate
pip install flask py-zabbix pygelf
```

---

## Script → Host Mapping

This is the single source of truth inside `graylog_zabbix_receiver.py`. When adding a new script, this is the only thing that needs to change in the receiver.

```python
SCRIPT_HOST_MAP = {
    "ftp_upload":          "ncc-scripts-radio",
    "ftp_upload_salem":    "ncc-scripts-radio",
    "ftp_delete":          "ncc-scripts-radio",
    "ftp_delete_salem":    "ncc-scripts-radio",
    "vimeo_m3u8":          "ncc-scripts-video",
    "check_mainsite_post": "ncc-scripts-web",
    "send_email":          "ncc-scripts-email",
    "telos_disconnect":    "ncc-scripts-audio",
    "move_files":          "ncc-scripts-files",
    "zabbix_receiver":     "ncc-scripts-files",
    "study_titles":        "ncc-scripts-files",
}
```

At startup, this is automatically inverted into `HOST_SCRIPT_MAP` (host → list of scripts) for the discovery endpoint.

Scripts not in this map are caught by the receiver and forwarded to `ncc-scripts-receiver` as `script.unknown` traps — see the [Unknown Script Monitoring](#unknown-script-monitoring) section.

---

## Event Type → Zabbix Item Key Mapping

```python
EVENT_TYPE_SUFFIX = {
    "script_stop": "crash",      # only fires when exit_status=error
    "error":       "error",
    "key_event":   "key_event",
}
```

Zabbix item keys use the format `script.{suffix}[{script_name}]`:
- `script.crash[ftp_upload]`
- `script.error[ftp_upload]`
- `script.key_event[ftp_upload]`

Value construction:
- `script_stop` → `"CRASH: {error_message}"`
- `error` → `"ERROR: {error_message}"`
- `key_event` → the raw Graylog event message string

---

## Systemd Service

File: `/etc/systemd/system/graylog-zabbix-receiver.service`

```ini
[Unit]
Description=Graylog to Zabbix HTTP receiver
After=network.target

[Service]
Type=simple
User=smoo
WorkingDirectory=/opt/scripts/receiver
ExecStart=/opt/scripts/receiver/.venv/bin/python3 /opt/scripts/receiver/graylog_zabbix_receiver.py
Restart=on-failure
RestartSec=5
Environment=ZABBIX_SERVER=zabbix.northcountrychapel.com

[Install]
WantedBy=multi-user.target
```

Key points:
- `ExecStart` points at the **venv Python**, not `/usr/bin/python3` — system Python doesn't have the dependencies.
- `User=smoo` 
- `WorkingDirectory=/opt/scripts/receiver`.
- The `Environment=ZABBIX_SERVER=...` line is optional since it matches the default in the receiver code; leave it in for explicitness or override here if the Zabbix server moves.

### Service Management

```bash
# Start and enable on boot
sudo systemctl enable --now graylog-zabbix-receiver

# Check status
sudo systemctl status graylog-zabbix-receiver

# View logs
sudo journalctl -u graylog-zabbix-receiver -n 50 --no-pager

# Restart after config changes
sudo systemctl restart graylog-zabbix-receiver

# Reload systemd after editing the .service file
sudo systemctl daemon-reload
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ZABBIX_SERVER` | `zabbix.northcountrychapel.com` | Zabbix server hostname or IP |
| `ZABBIX_PORT` | `10051` | Zabbix trapper port |
| `RECEIVER_PORT` | `9999` | Port the receiver listens on |
| `RECEIVER_SECRET` | (none) | If set, POST /zabbix requires `X-Receiver-Token` header |

---

## Testing

### Health check
```bash
curl http://localhost:9999/health
# {"status": "ok"}
```

### Discovery
```bash
curl http://localhost:9999/discover/ncc-scripts-radio
# {"data":[{"{#SCRIPT_NAME}":"ftp_delete"},{"{#SCRIPT_NAME}":"ftp_delete_salem"},{"{#SCRIPT_NAME}":"ftp_upload"},{"{#SCRIPT_NAME}":"ftp_upload_salem"}]}
```

### Simulate an event
```bash
curl -X POST http://localhost:9999/zabbix \
  -H "Content-Type: application/json" \
  -d '{"event":{"message":"Test crash event","fields":{"event_type":"script_stop","script_name":"ftp_upload","error_message":"test error from curl"}}}'
# {"status": "sent", "zabbix_response": "..."}
```

### Test unknown-script trap
```bash
zabbix_sender -z zabbix.northcountrychapel.com -s ncc-scripts-receiver -k script.unknown -o "test_script (error)"
```

---

## Zabbix Configuration

### Template: NCC Script Monitoring

Create one template. All six per-category hosts link to it.

#### Discovery Rule

- Name: `Script discovery`
- Type: `HTTP agent`
- Key: `script.discovery`
- URL: `http://10.10.0.77:9999/discover/{HOST.NAME}`
- Update interval: `1h`
- Keep lost resources period: `7d`

Note: `{HOST.NAME}` resolves to the Zabbix host name at runtime.

#### Item Prototypes (3)

All type **Zabbix trapper**, type of information **Text**:

| Name | Key |
|---|---|
| `{#SCRIPT_NAME} — crash` | `script.crash[{#SCRIPT_NAME}]` |
| `{#SCRIPT_NAME} — error` | `script.error[{#SCRIPT_NAME}]` |
| `{#SCRIPT_NAME} — key event` | `script.key_event[{#SCRIPT_NAME}]` |

#### Trigger Prototypes (3)

| Name | Severity | Expression | Recovery Expression |
|---|---|---|---|
| `{#SCRIPT_NAME} crashed` | High | `last(/NCC Script Monitoring/script.crash[{#SCRIPT_NAME}])<>""` | `nodata(/NCC Script Monitoring/script.crash[{#SCRIPT_NAME}],300)=1` |
| `{#SCRIPT_NAME} mid-run error` | Average | `last(/NCC Script Monitoring/script.error[{#SCRIPT_NAME}])<>""` | `nodata(/NCC Script Monitoring/script.error[{#SCRIPT_NAME}],300)=1` |
| `{#SCRIPT_NAME} key event` | Information | `last(/NCC Script Monitoring/script.key_event[{#SCRIPT_NAME}])<>""` | `nodata(/NCC Script Monitoring/script.key_event[{#SCRIPT_NAME}],300)=1` |

The `nodata(300)` recovery means triggers auto-resolve 5 minutes after the last value arrives, since trapper items never self-clear.

### Hosts (6 template-linked + 1 standalone)

The six per-category hosts share the same settings:
- Templates: `NCC Script Monitoring`
- Host groups: `NCC Scripts` (or similar)
- Interfaces: Agent, `127.0.0.1`, disabled (required by Zabbix but not used)

| Host name | Scripts covered |
|---|---|
| `ncc-scripts-radio` | ftp_upload, ftp_upload_salem, ftp_delete, ftp_delete_salem |
| `ncc-scripts-video` | vimeo_m3u8 |
| `ncc-scripts-audio` | telos_disconnect |
| `ncc-scripts-files` | move_files, zabbix_receiver, study_titles |
| `ncc-scripts-web` | check_mainsite_post |
| `ncc-scripts-email` | send_email |

After creating each host, run discovery manually: go to the host's Discovery rules, click into Script discovery, and click **Execute now**.

The seventh host, `ncc-scripts-receiver`, is configured manually (no template) — see below.

---

## Unknown Script Monitoring

When a script logs an event to Graylog with a `script_name` that isn't in `SCRIPT_HOST_MAP`, the receiver sends a `script.unknown` trap to the `ncc-scripts-receiver` host with the value `{script_name} ({event_type})`. This catches scripts that haven't been added to the map yet so you find out within minutes instead of whenever you happen to look at the error log.

### Host

- Host name: `ncc-scripts-receiver`
- Host groups: same as the other script hosts (e.g., `NCC Scripts`)
- Templates: **none** (manual config — this host is not part of the LLD setup)
- Interfaces: Agent, `127.0.0.1`, disabled (required but unused)

### Item

- Name: `Unknown script reported to receiver`
- Type: `Zabbix trapper`
- Key: `script.unknown`
- Type of information: `Text`
- History storage period: `30d`
- Trend storage period: `0` (text items don't trend)

### Trigger

- Name: `Unknown script reported: {ITEM.LASTVALUE}`
- Severity: `Warning`
- Expression: `nodata(/ncc-scripts-receiver/script.unknown,1h)=0`
- OK event generation: `Recovery expression`
- Recovery expression: `nodata(/ncc-scripts-receiver/script.unknown,1h)=1`

Fires when a `script.unknown` trap is received in the last hour; auto-recovers after an hour of silence. Adjust the window if you want it stickier or shorter.

### When this fires

Either a new script was added to production without a corresponding `SCRIPT_HOST_MAP` entry, or an existing script's `SCRIPT_NAME` constant was renamed without updating the map. To resolve:

1. Add the script to `SCRIPT_HOST_MAP` in the receiver (see [Adding a New Script](#adding-a-new-script)).
2. Restart the receiver.
3. The trigger will auto-recover after the recovery window.

---

## Graylog Configuration

Each script monitoring event definition in Graylog needs an HTTP notification pointing at:

```
http://localhost:9999/zabbix
```

(localhost because the receiver runs on the same VM as Graylog.)

The three event definitions that forward to Zabbix:
- **Script crash** — `event_type:script_stop AND exit_status:error`
- **Mid-run error** — `event_type:error`
- **Key event** — `event_type:key_event`

Each event definition's Fields tab must include `script_name`, `event_type`, and `error_message` using `${source.fields.field_name}` syntax (e.g., `${source.fields.event_type}`) so the notification payload contains the data the receiver needs.

**Important:** the correct template syntax for this Graylog instance is `${source.fields.event_type}` — not `${source.fields._event_type}` or `${source.field_name}`. Using the wrong variant causes the field to come through empty and the receiver silently ignores the event. Verify by checking test event output before saving.

---

## Adding a New Script

1. Pick a `SCRIPT_NAME` that matches the registry in `script_logging_standards.md`, or plan to update both together. Mismatches surface as `script.unknown` alerts.
2. Add `"new_script_name": "ncc-scripts-{category}"` to `SCRIPT_HOST_MAP` in the receiver.
3. Restart the receiver: `sudo systemctl restart graylog-zabbix-receiver`
4. In Zabbix: run discovery on the relevant host (or wait up to 1 hour).
5. Three items and three triggers appear automatically.
6. Add `new_script_name` to the script name registry in `script_logging_standards.md`.

If the script belongs to a new category, also create a new Zabbix host with the matching name and link the `NCC Script Monitoring` template.

---

## Troubleshooting

**Receiver won't start (ModuleNotFoundError)**
The systemd unit must point at the venv Python (`/opt/scripts/receiver/.venv/bin/python3`), not system Python. System Python doesn't have flask/py-zabbix/pygelf.

**Discovery returns 404**
The `{HOST.NAME}` macro must match a key in `HOST_SCRIPT_MAP`. Check that the Zabbix **Host name** field (not Visible name) matches exactly (e.g., `ncc-scripts-radio`). If it was recently changed, edit the discovery rule on the template (change nothing, just click Update) to force macro re-evaluation.

**Events from a known script not appearing in Zabbix**
Most often a Graylog event definition is missing `event_type` (or another required field) in its Fields tab. Symptoms: the event fires in Graylog but the receiver logs `Ignoring event_type=` or returns 400. Open the event definition, confirm `event_type` is set to `${source.fields.event_type}` (with `.fields.` and no leading underscore), and re-test.

**Unknown script alert firing**
A `script.unknown` trigger means a script logged to Graylog with a `script_name` that's not in `SCRIPT_HOST_MAP`. The trap value shows which script and which event type. Add it to the map and restart the receiver.

**Check what's hitting the receiver**
```bash
sudo journalctl -u graylog-zabbix-receiver -n 30 --no-pager
```
Flask logs every request with the full path, so you can see exactly what URL Zabbix is requesting.

**Test Zabbix connectivity from the receiver**
```bash
source /opt/scripts/receiver/.venv/bin/activate
python3 -c "from pyzabbix import ZabbixSender; print(ZabbixSender('zabbix.northcountrychapel.com', 10051))"
```