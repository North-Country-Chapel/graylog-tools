#!/usr/bin/env python3
"""
Graylog → Zabbix HTTP receiver with Low-Level Discovery (LLD).

Endpoints:
    POST /zabbix          — receives Graylog HTTP notification payloads,
                            forwards events to Zabbix as trapper item values.
    GET  /discover/{host} — returns Zabbix LLD JSON for the requested host.
    GET  /health          — basic health check.

Environment variables:
    ZABBIX_SERVER     — Zabbix server hostname or IP
                        (default: zabbix.northcountrychapel.com)
    ZABBIX_PORT       — Zabbix trapper port (default: 10051)
    RECEIVER_PORT     — port this service listens on (default: 9999)
    RECEIVER_SECRET   — if set, POST /zabbix requires X-Receiver-Token header
"""

import os
import sys
import signal

# ---------------------------------------------------------------------------
# logger.py lives one directory above the receiver folder
# ---------------------------------------------------------------------------
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logger import get_logger

from flask import Flask, request, jsonify
from pyzabbix import ZabbixMetric, ZabbixSender

# ---------------------------------------------------------------------------
# Logging setup (follows script_logging_standards.md)
# ---------------------------------------------------------------------------
SCRIPT_NAME = "zabbix_receiver"
logger = get_logger(SCRIPT_NAME, __file__)


def log_extra(**kwargs):
    return {"script_name": SCRIPT_NAME, **kwargs}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ZABBIX_SERVER = os.getenv("ZABBIX_SERVER", "zabbix.northcountrychapel.com")
ZABBIX_PORT = int(os.getenv("ZABBIX_PORT", "10051"))
RECEIVER_PORT = int(os.getenv("RECEIVER_PORT", "9999"))
RECEIVER_SECRET = os.getenv("RECEIVER_SECRET")

# ---------------------------------------------------------------------------
# Script → Zabbix host mapping 
# ---------------------------------------------------------------------------
SCRIPT_HOST_MAP = {
    "ftp_upload":       "ncc-scripts-radio",
    "ftp_upload_salem": "ncc-scripts-radio",
    "ftp_delete":       "ncc-scripts-radio",
    "ftp_delete_salem": "ncc-scripts-radio",
    "vimeo_m3u8":       "ncc-scripts-video",
    "check_mainsite_post": "ncc-scripts-web",
    "send_email":       "ncc-scripts-email",
    "telos_disconnect": "ncc-scripts-audio",
    "move_files":        "ncc-scripts-files",
    "zabbix_receiver":   "ncc-scripts-files",
    "study_titles": 	"ncc-scripts-files",
    # add new scripts here
}

# Invert the map once at startup: host → [script, script, ...]
HOST_SCRIPT_MAP = {}
for script, host in SCRIPT_HOST_MAP.items():
    HOST_SCRIPT_MAP.setdefault(host, []).append(script)

# Event type → Zabbix item key suffix
EVENT_TYPE_SUFFIX = {
    "script_stop": "crash",      # only fires when exit_status=error
    "error":       "error",
    "key_event":   "key_event",
}

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


def _check_auth():
    """Return an error response if auth is required and the token is wrong."""
    if RECEIVER_SECRET is None:
        return None
    token = request.headers.get("X-Receiver-Token", "")
    if token != RECEIVER_SECRET:
        logger.warning("Rejected request — invalid or missing auth token",
                       extra=log_extra(event_type="error",
                                       error_message="auth_failed"))
        return jsonify({"error": "unauthorized"}), 401
    return None


@app.route("/zabbix", methods=["POST"])
def zabbix_endpoint():
    """Receive a Graylog HTTP notification and forward it to Zabbix."""
    auth_error = _check_auth()
    if auth_error:
        return auth_error

    try:
        payload = request.get_json(force=True)
    except Exception:
        logger.warning("Bad JSON payload", extra=log_extra(event_type="error",
                        error_message="invalid_json"))
        return jsonify({"error": "invalid JSON"}), 400

    event = payload.get("event", {})
    fields = event.get("fields", {})

    # The original log message lives in the backlog, not event.message
    backlog = payload.get("backlog") or []
    original_message = backlog[0].get("message", "") if backlog else ""

    event_type = fields.get("event_type", "")
    script_name = fields.get("script_name", "")
    error_message = fields.get("error_message", "")

    # Determine suffix; skip if event type isn't one we forward
    suffix = EVENT_TYPE_SUFFIX.get(event_type)
    if suffix is None:
        logger.debug(f"Ignoring event_type={event_type} for {script_name}",
                     extra=log_extra())
        return jsonify({"status": "ignored", "reason": "unhandled event_type"}), 200

    if event_type == "key_event" and script_name == SCRIPT_NAME:
        return jsonify({"status": "ignored", "reason": "key_event suppressed for receiver"}), 200

    # Resolve Zabbix host from the script name
    zabbix_host = SCRIPT_HOST_MAP.get(script_name)
    if zabbix_host is None:
        logger.warning(f"Unknown script_name: {script_name}",
                    extra=log_extra(event_type="error",
                                    error_message=f"unknown_script:{script_name}"))
        # Also send a dedicated trap so Zabbix can alert on unknown scripts directly
        try:
            unknown_metric = ZabbixMetric(
                "ncc-scripts-receiver",
                "script.unknown",
                f"{script_name} ({event_type})",
            )
            ZabbixSender(ZABBIX_SERVER, ZABBIX_PORT).send([unknown_metric])
        except Exception as e:
            logger.error(f"Failed to send unknown_script trap: {e}",
                        extra=log_extra(event_type="error", error_message=str(e)))
        return jsonify({"error": f"unknown script: {script_name}"}), 400

    # Build item key and value
    item_key = f"script.{suffix}[{script_name}]"

    if event_type == "script_stop":
        value = f"CRASH: {error_message}"
    elif event_type == "error":
        value = f"ERROR: {error_message}"
    else:  # key_event
        value = original_message or "key event"

    # Send to Zabbix
    metric = ZabbixMetric(zabbix_host, item_key, value)
    try:
        sender = ZabbixSender(ZABBIX_SERVER, ZABBIX_PORT)
        result = sender.send([metric])
        logger.info(f"Sent to Zabbix: {zabbix_host}/{item_key} → {value}",
                    extra=log_extra(event_type="key_event"))
        return jsonify({"status": "sent", "zabbix_response": str(result)}), 200
    except Exception as e:
        logger.error(f"Zabbix send failed: {e}",
                     extra=log_extra(event_type="error",
                                     error_message=str(e)))
        return jsonify({"error": f"zabbix send failed: {e}"}), 502


@app.route("/discover/<host_name>", methods=["GET"])
def discover(host_name):
    """Return Zabbix LLD JSON for the given host."""
    scripts = HOST_SCRIPT_MAP.get(host_name)
    if scripts is None:
        return jsonify({"error": f"unknown host: {host_name}"}), 404

    lld_data = [{ "{#SCRIPT_NAME}": s} for s in sorted(scripts)]
    return jsonify({"data": lld_data}), 200


@app.route("/health", methods=["GET"])
def health():
    """Basic health check."""
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
def _shutdown_handler(signum, frame):
    """Log a clean stop on SIGTERM/SIGINT."""
    logger.info(f"Stopped service: {SCRIPT_NAME}",
                extra=log_extra(event_type="script_stop", exit_status="success"))
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    logger.info(f"Starting service: {SCRIPT_NAME}",
                extra=log_extra(event_type="script_start"))
    logger.info(f"Zabbix target: {ZABBIX_SERVER}:{ZABBIX_PORT}",
                extra=log_extra())
    logger.info(f"Auth: {'enabled' if RECEIVER_SECRET else 'disabled'}",
                extra=log_extra())
    logger.info(f"Hosts: {', '.join(sorted(HOST_SCRIPT_MAP.keys()))}",
                extra=log_extra())

    try:
        app.run(host="0.0.0.0", port=RECEIVER_PORT)
    except Exception as e:
        logger.error(f"Service crashed: {e}",
                     extra=log_extra(event_type="script_stop", exit_status="error",
                                     error_message=str(e)))
        sys.exit(1)
