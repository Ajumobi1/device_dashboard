from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room
import sqlite3
import time
import os
import logging
import json
import urllib.parse
import urllib.request
from datetime import datetime
from threading import Lock

# ── VAPID / Web Push ────────────────────────────────────────────────────────
try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid
    WEBPUSH_AVAILABLE = True
except ImportError:
    WEBPUSH_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "pywebpush not installed — push notifications disabled. "
        "Run: pip install pywebpush"
    )

# -----------------------
# Initialize App & Security
# -----------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-production")
thread_lock = Lock()

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
app.logger.setLevel(getattr(logging, log_level, logging.INFO))


def parse_cors_origins(raw):
    if not raw or raw.strip() == "*":
        return "*"
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def parse_env_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

# Professional Buffer: Optimized for high-speed binary data (Live Video/Audio)
# max_http_buffer_size set to 20MB to handle high-res snapshots without crashing
socketio = SocketIO(
    app,
    async_mode="gevent",
    cors_allowed_origins=parse_cors_origins(os.environ.get("CORS_ALLOWED_ORIGINS", "*")),
    ping_timeout=120,
    ping_interval=25,
    max_http_buffer_size=20000000 
)

# -----------------------
# Configuration & Storage
# -----------------------
DB = "devices.db"

# In-memory cache to keep the dashboard responsive
devices = {}
last_trail_write = {}
dashboard_sids = set()

# Performance tuning
DASHBOARD_BROADCAST_INTERVAL = 1.0
TRAIL_LOG_INTERVAL_SECONDS = 15
DASHBOARD_ROOM = "dashboard_viewers"
OFFLINE_DEVICE_TIMEOUT_SECONDS = int(os.environ.get("OFFLINE_DEVICE_TIMEOUT_SECONDS", "60"))
DEVICE_SWEEP_INTERVAL_SECONDS = int(os.environ.get("DEVICE_SWEEP_INTERVAL_SECONDS", "15"))
MAX_DEVICE_ID_LENGTH = 128
MAX_TEXT_FIELD_LENGTH = 256
MAX_MESSAGE_LENGTH = 2048
TELEGRAM_ALERTS_ENABLED = parse_env_bool(os.environ.get("TELEGRAM_ALERTS_ENABLED", "0"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
telegram_last_error = ""

last_dashboard_emit = 0.0
dashboard_emit_scheduled = False
APP_START_TS = time.time()
sweeper_started = False

# Per-device push subscriptions stored in memory (also persisted in DB)
push_subscriptions = {}        # device_id -> subscription dict
# Throttle Telegram GPS pings (avoid spam every 6 s)
last_telegram_location = {}    # device_id -> (lat, lon, timestamp)
TELEGRAM_GPS_MIN_DELTA   = 0.001    # ~111 m
TELEGRAM_GPS_MIN_SECONDS = 120      # at least 2 min between pins

# ── VAPID key bootstrap ──────────────────────────────────────────────────────
VAPID_PRIVATE_FILE = os.environ.get("VAPID_PRIVATE_FILE", "vapid_private.pem")
VAPID_PUBLIC_KEY   = ""
VAPID_CLAIMS       = {"sub": "mailto:admin@connectsafe.app"}

def _load_or_generate_vapid():
    global VAPID_PUBLIC_KEY
    if not WEBPUSH_AVAILABLE:
        return
    try:
        vapid = Vapid()
        if os.path.exists(VAPID_PRIVATE_FILE):
            vapid.from_file(VAPID_PRIVATE_FILE)
        else:
            vapid.generate_keys()
            vapid.save_key(VAPID_PRIVATE_FILE)
        VAPID_PUBLIC_KEY = vapid.public_key_urlsafe_base64
        logging.getLogger(__name__).info("VAPID public key loaded.")
    except Exception:
        logging.getLogger(__name__).exception("VAPID key init failed")

_load_or_generate_vapid()

metrics = {
    "http_requests_total": 0,
    "http_by_path": {},
    "dashboard_emits": 0,
    "telemetry_events": 0,
    "system_alerts": 0,
    "telegram_sent": 0,
    "telegram_failed": 0,
    "pings_sent": 0,
    "pongs_received": 0,
}


def inc_metric(key, amount=1):
    with thread_lock:
        metrics[key] = metrics.get(key, 0) + amount


def safe_text(value, max_len=MAX_TEXT_FIELD_LENGTH):
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def safe_float(value, min_value=None, max_value=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if min_value is not None and number < min_value:
        return None
    if max_value is not None and number > max_value:
        return None
    return number


def device_status_sweeper():
    while True:
        time.sleep(max(DEVICE_SWEEP_INTERVAL_SECONDS, 1))
        now = int(time.time())
        changed = False

        with thread_lock:
            for _, info in devices.items():
                if info.get("status") != "online":
                    continue
                last_seen = int(info.get("last_seen") or 0)
                if (now - last_seen) > OFFLINE_DEVICE_TIMEOUT_SECONDS:
                    info["status"] = "offline"
                    info["sid"] = None
                    changed = True

        if changed:
            emit_dashboard_update(force=True)


def ensure_background_tasks():
    global sweeper_started
    with thread_lock:
        if sweeper_started:
            return
        sweeper_started = True
    socketio.start_background_task(device_status_sweeper)


@app.before_request
def record_http_metrics():
    with thread_lock:
        metrics["http_requests_total"] += 1
        path_key = request.path
        path_counts = metrics["http_by_path"]
        path_counts[path_key] = path_counts.get(path_key, 0) + 1


def get_db_connection():
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def serialize_devices():
    return {
        device_id: {k: v for k, v in device.items() if k != "sid"}
        for device_id, device in devices.items()
    }


def get_device_id_by_sid(sid):
    for current_device_id, info in devices.items():
        if info.get("sid") == sid:
            return current_device_id
    return None


def get_request_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return safe_text(forwarded.split(",")[0], MAX_TEXT_FIELD_LENGTH)
    return safe_text(request.headers.get("X-Real-IP") or request.remote_addr, MAX_TEXT_FIELD_LENGTH)


def send_system_alert(message, level="info", device_id="system"):
    safe_message = safe_text(message, MAX_MESSAGE_LENGTH)
    payload = {
        "device_id": safe_text(device_id, MAX_DEVICE_ID_LENGTH) or "system",
        "level": safe_text(level, 32) or "info",
        "message": safe_message,
        "timestamp": int(time.time()),
    }
    inc_metric("system_alerts")
    socketio.emit("system_alert", payload, room=DASHBOARD_ROOM)


def record_device_log(device_id, event_type, message, sender="client"):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO logs (device_id, type, sender, message, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            safe_text(device_id, MAX_DEVICE_ID_LENGTH),
            safe_text(event_type, 64),
            safe_text(sender, 64),
            safe_text(message, MAX_MESSAGE_LENGTH),
            int(time.time()),
        ),
    )
    conn.commit()
    conn.close()


def emit_device_event(device_id, level, event_type, message):
    log_text = safe_text(message, MAX_MESSAGE_LENGTH)
    send_system_alert(log_text, level=level, device_id=device_id)
    record_device_log(device_id, event_type, log_text)
    send_telegram_alert(
        f"🔔 Device Event\n"
        f"ID: {device_id}\n"
        f"Type: {event_type}\n"
        f"Level: {level}\n"
        f"Message: {log_text}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram helpers — text, photo, location pin
# ─────────────────────────────────────────────────────────────────────────────
def send_telegram_alert(text):
    global telegram_last_error
    if not TELEGRAM_ALERTS_ENABLED:
        telegram_last_error = "telegram alerts disabled"
        return False
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        telegram_last_error = "missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
        inc_metric("telegram_failed")
        app.logger.warning("Telegram alert skipped: %s", telegram_last_error)
        return False

    try:
        endpoint = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw_body = resp.read().decode("utf-8")
            if resp.status != 200:
                telegram_last_error = f"http {resp.status}: {raw_body[:180]}"
                inc_metric("telegram_failed")
                app.logger.warning("Telegram API non-200: %s", telegram_last_error)
                return False
            data = json.loads(raw_body)
            if data.get("ok"):
                telegram_last_error = ""
                inc_metric("telegram_sent")
                return True
            description = str(data.get("description", "unknown error"))
            telegram_last_error = f"telegram api error: {description[:180]}"
            app.logger.warning("Telegram API error: %s", telegram_last_error)
    except Exception:
        telegram_last_error = "exception while sending telegram alert"
        app.logger.exception("Telegram send failed")

    inc_metric("telegram_failed")
    return False


def send_telegram_location(lat, lon, caption=""):
    """Send a live location pin to the Telegram chat."""
    if not TELEGRAM_ALERTS_ENABLED or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        # First send the map pin
        params = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "latitude":  str(round(lat, 6)),
            "longitude": str(round(lon, 6)),
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendLocation",
            data=params, method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                inc_metric("telegram_sent")
                # Follow up with a text caption if provided
                if caption:
                    send_telegram_alert(caption)
                return True
    except Exception:
        app.logger.exception("Telegram location send failed")
    inc_metric("telegram_failed")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Web Push helper
# ─────────────────────────────────────────────────────────────────────────────
def send_push_to_device(device_id, title, body, url="/client"):
    """Send a web-push notification to all subscriptions for a device_id."""
    if not WEBPUSH_AVAILABLE or not VAPID_PUBLIC_KEY:
        return
    sub = push_subscriptions.get(device_id)
    if not sub:
        return
    payload = json.dumps({"title": title, "body": body,
                          "url": url, "icon": "/static/icon-192.png"})
    try:
        webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_FILE,
            vapid_claims=VAPID_CLAIMS,
        )
    except WebPushException as exc:
        app.logger.warning("Push to %s failed: %s", device_id[:12], exc)
        # If subscription expired/invalid, remove it
        if "410" in str(exc) or "404" in str(exc):
            push_subscriptions.pop(device_id, None)
            _db_delete_push_subscription(device_id)
    except Exception:
        app.logger.exception("Push send error for %s", device_id[:12])


def _db_delete_push_subscription(device_id):
    try:
        conn = get_db_connection()
        conn.execute("DELETE FROM push_subscriptions WHERE device_id = ?", (device_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _delayed_dashboard_emit(delay):
    global last_dashboard_emit, dashboard_emit_scheduled
    time.sleep(max(delay, 0))
    with thread_lock:
        if not dashboard_emit_scheduled:
            return
        dashboard_emit_scheduled = False
        last_dashboard_emit = time.time()
        payload = serialize_devices()
    inc_metric("dashboard_emits")
    socketio.emit("dashboard_update", payload, room=DASHBOARD_ROOM)


def emit_dashboard_update(force=False):
    global last_dashboard_emit, dashboard_emit_scheduled
    with thread_lock:
        now = time.time()
        elapsed = now - last_dashboard_emit

        if force or elapsed >= DASHBOARD_BROADCAST_INTERVAL:
            last_dashboard_emit = now
            dashboard_emit_scheduled = False
            payload = serialize_devices()
        elif not dashboard_emit_scheduled:
            dashboard_emit_scheduled = True
            delay = DASHBOARD_BROADCAST_INTERVAL - elapsed
            socketio.start_background_task(_delayed_dashboard_emit, delay)
            return
        else:
            return

    inc_metric("dashboard_emits")
    socketio.emit("dashboard_update", payload, room=DASHBOARD_ROOM)


@socketio.on("join_dashboard")
def join_dashboard_view():
    ensure_background_tasks()
    with thread_lock:
        dashboard_sids.add(request.sid)
    join_room(DASHBOARD_ROOM)
    emit("dashboard_update", serialize_devices())


@socketio.on("push_subscribe")
def handle_push_subscribe(data):
    """Client sends its push subscription after user grants consent."""
    device_id = safe_text(data.get("device_id"), MAX_DEVICE_ID_LENGTH)
    subscription = data.get("subscription")
    if not device_id or not isinstance(subscription, dict):
        return
    push_subscriptions[device_id] = subscription
    # Persist so restarts don't lose it
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO push_subscriptions (device_id, subscription, created_at) "
            "VALUES (?, ?, ?)",
            (device_id, json.dumps(subscription), int(time.time()))
        )
        conn.commit()
        conn.close()
    except Exception:
        app.logger.exception("Failed to save push subscription for %s", device_id[:12])
    app.logger.info("Push subscription saved for %s", device_id[:12])
    # Confirm to the device
    send_push_to_device(
        device_id,
        title="ConnectSafe Connected",
        body="Push notifications are now active on this device.",
        url="/client"
    )


@socketio.on("client_event")
def handle_client_event(data):
    """Ingest explicit app-generated events (consent-gated)."""
    device_id = safe_text(data.get("device_id"), MAX_DEVICE_ID_LENGTH)
    event_type = safe_text(data.get("type"), 64) or "event"
    level = safe_text(data.get("level"), 16) or "info"
    message = safe_text(data.get("message"), MAX_MESSAGE_LENGTH)
    if not device_id or not message:
        return

    device = devices.get(device_id, {})
    has_consent = bool(data.get("consent_granted", device.get("consent_granted", False)))
    if not has_consent:
        app.logger.info("Ignoring client_event without consent for %s", device_id[:12])
        return

    emit_device_event(device_id, level, event_type, message)


@app.route("/push-test/<device_id>")
def push_test(device_id):
    safe_device_id = safe_text(device_id, MAX_DEVICE_ID_LENGTH)
    if not safe_device_id:
        return jsonify({"ok": False, "error": "invalid device id"}), 400

    device = devices.get(safe_device_id)
    if not device:
        return jsonify({"ok": False, "error": "unknown device id"}), 404
    if not device.get("consent_granted"):
        return jsonify({"ok": False, "error": "consent not granted"}), 403

    send_push_to_device(
        safe_device_id,
        title="ConnectSafe Test",
        body="Push notification path is working.",
        url="/client",
    )

    emit_device_event(
        safe_device_id,
        level="info",
        event_type="push_test",
        message="Dashboard triggered push delivery test."
    )
    return jsonify({"ok": True, "device_id": safe_device_id})


@app.route("/vapid-public-key")
def vapid_public_key():
    """Return the VAPID public key so the client can subscribe to push."""
    if not WEBPUSH_AVAILABLE or not VAPID_PUBLIC_KEY:
        return jsonify({"error": "push not available"}), 503
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route("/healthz")
def healthz():
    return jsonify({
        "status": "ok",
        "uptime_seconds": int(max(time.time() - APP_START_TS, 0))
    })


@app.route("/test-telegram")
def test_telegram():
    ok = send_telegram_alert("Test alert from your PhoneTracker server")
    return jsonify({
        "sent": ok,
        "enabled": TELEGRAM_ALERTS_ENABLED,
        "configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "chat_id_suffix": TELEGRAM_CHAT_ID[-4:] if TELEGRAM_CHAT_ID else "",
        "last_error": telegram_last_error,
    })


@app.route("/metrics")
def get_metrics():
    now = time.time()
    uptime_seconds = max(now - APP_START_TS, 1)

    with thread_lock:
        online_devices = sum(1 for d in devices.values() if d.get("status") == "online")
        total_devices = len(devices)
        payload = {
            "uptime_seconds": int(uptime_seconds),
            "devices": {
                "online": online_devices,
                "offline": max(total_devices - online_devices, 0),
                "total": total_devices,
            },
            "telegram": {
                "enabled": TELEGRAM_ALERTS_ENABLED,
                "configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
                "chat_id_suffix": TELEGRAM_CHAT_ID[-4:] if TELEGRAM_CHAT_ID else "",
                "last_error": telegram_last_error,
            },
            "sockets": {
                "dashboard_viewers": len(dashboard_sids)
            },
            "events": dict(metrics),
            "rates_per_second": {
                "dashboard_emits": round(metrics.get("dashboard_emits", 0) / uptime_seconds, 3),
                "telemetry_events": round(metrics.get("telemetry_events", 0) / uptime_seconds, 3),
                "system_alerts": round(metrics.get("system_alerts", 0) / uptime_seconds, 3),
                "telegram_sent": round(metrics.get("telegram_sent", 0) / uptime_seconds, 3),
                "telegram_failed": round(metrics.get("telegram_failed", 0) / uptime_seconds, 3),
            }
        }

    return jsonify(payload)

# -----------------------
# Database Engine (Expanded)
# -----------------------
def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    # 1. Core Device Table
    c.execute("""
    CREATE TABLE IF NOT EXISTS devices(
        device_id TEXT PRIMARY KEY,
        battery TEXT,
        charging TEXT,
        platform TEXT,
        model TEXT,
        network TEXT,
        ip_address TEXT,
        browser TEXT,
        lat REAL,
        lon REAL,
        last_seen INTEGER
    )
    """)

    # 2. 📍 Movement Trail Table (Professional History)
    c.execute("""
    CREATE TABLE IF NOT EXISTS trails(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT,
        lat REAL,
        lon REAL,
        timestamp INTEGER
    )
    """)

    # 3. 📱 SMS & Notification Logs (The Extractor Sink)
    c.execute("""
    CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT,
        type TEXT,
        sender TEXT,
        message TEXT,
        timestamp INTEGER
    )
    """)

    # 4. Push Subscriptions Table
    c.execute("""
    CREATE TABLE IF NOT EXISTS push_subscriptions(
        device_id TEXT PRIMARY KEY,
        subscription TEXT NOT NULL,
        created_at INTEGER
    )
    """)

    c.execute("CREATE INDEX IF NOT EXISTS idx_trails_device_time ON trails(device_id, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_logs_device_time ON logs(device_id, timestamp)")

    existing_columns = {row[1] for row in c.execute("PRAGMA table_info(devices)").fetchall()}
    if "ip_address" not in existing_columns:
        c.execute("ALTER TABLE devices ADD COLUMN ip_address TEXT")
    if "browser" not in existing_columns:
        c.execute("ALTER TABLE devices ADD COLUMN browser TEXT")
    if "consent_granted" not in existing_columns:
        c.execute("ALTER TABLE devices ADD COLUMN consent_granted INTEGER DEFAULT 0")

    conn.commit()
    conn.close()


def load_push_subscriptions():
    """Load all push subscriptions from DB into memory at startup."""
    try:
        conn = get_db_connection()
        rows = conn.execute("SELECT device_id, subscription FROM push_subscriptions").fetchall()
        for row in rows:
            try:
                push_subscriptions[row[0]] = json.loads(row[1])
            except Exception:
                pass
        conn.close()
    except Exception:
        app.logger.exception("Failed to load push subscriptions")


def save_device(data, log_trail=True):
    conn = get_db_connection()
    c = conn.cursor()

    # Save/Update main device info
    c.execute("""
    INSERT OR REPLACE INTO devices
    (device_id, battery, charging, platform, model, network, ip_address, browser, lat, lon, last_seen, consent_granted)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["device_id"],
        data.get("battery"),
        data.get("charging"),
        data.get("platform"),
        data.get("model", "Unknown"),
        data.get("network", "Unknown"),
        data.get("ip_address", ""),
        data.get("browser", ""),
        data.get("lat"),
        data.get("lon"),
        data["last_seen"],
        1 if data.get("consent_granted") else 0
    ))

    # Log the coordinate for movement trail tracking
    if log_trail and data.get("lat") is not None and data.get("lon") is not None:
        c.execute("""
        INSERT INTO trails (device_id, lat, lon, timestamp)
        VALUES (?, ?, ?, ?)
        """, (data["device_id"], data.get("lat"), data.get("lon"), data["last_seen"]))

    conn.commit()
    conn.close()

def load_devices():
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    try:
        rows = c.execute("SELECT * FROM devices").fetchall()
        for r in rows:
            devices[r["device_id"]] = {
                "device_id": r["device_id"],
                "battery": r["battery"],
                "charging": r["charging"],
                "platform": r["platform"],
                "model": r["model"],
                "network": r["network"],
                "ip_address": r["ip_address"] if "ip_address" in r.keys() else "",
                "browser": r["browser"] if "browser" in r.keys() else "",
                "lat": r["lat"],
                "lon": r["lon"],
                "consent_granted": bool(r["consent_granted"]) if "consent_granted" in r.keys() else False,
                "last_seen": r["last_seen"],
                "status": "offline",
                "sid": None
            }
            last_trail_write[r["device_id"]] = r["last_seen"] or 0
    except Exception as e:
        print(f"Database Load Error: {e}")
    finally:
        conn.close()

# -----------------------
# Web Routes
# -----------------------
@app.route("/")
def client():
    return render_template("client.html")

@app.route("/dashboard")
def dashboard():
    try:
        return render_template("dashboard.html")
    except Exception as error:
        app.logger.exception("Dashboard render failed: %s", error)
        return render_template("device.html")

# -----------------------
# Device Logic & Connection
# -----------------------
@socketio.on("register_device")
def register_device(data):
    ensure_background_tasks()
    device_id = safe_text(data.get("device_id"), MAX_DEVICE_ID_LENGTH)
    if not device_id:
        return
    ip_address = safe_text(data.get("ip_address") or get_request_ip(), MAX_TEXT_FIELD_LENGTH)
    browser = safe_text(data.get("browser") or request.headers.get("User-Agent", ""), MAX_TEXT_FIELD_LENGTH)
    consent = bool(data.get("consent_granted", False))

    print(f"[*] Device Online: {device_id}")
    devices.setdefault(device_id, {})
    devices[device_id]["sid"] = request.sid
    devices[device_id]["status"] = "online"
    devices[device_id]["last_seen"] = int(time.time())
    devices[device_id]["ip_address"] = ip_address
    devices[device_id]["browser"] = browser
    devices[device_id]["consent_granted"] = consent

    send_system_alert(
        f"Device online ({device_id[:12]}), consent={'yes' if consent else 'no'}",
        level="info",
        device_id=device_id,
    )
    send_telegram_alert(
        f"\U0001f4f1 Device Online\n"
        f"ID: {device_id}\n"
        f"Consent: {'\u2705 YES' if consent else '\u274c NO'}\n"
        f"IP: {ip_address or 'unknown'}\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    emit_dashboard_update(force=True)


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    with thread_lock:
        dashboard_sids.discard(sid)

    for d in devices:
        if devices[d].get("sid") == sid:
            devices[d]["status"] = "offline"
            print(f"[!] Device Offline: {d}")
            send_system_alert(f"Device offline ({d[:12]})", level="warning", device_id=d)
            send_telegram_alert(f"Device offline: {d}")
            break
    emit_dashboard_update(force=True)

# -----------------------
# Device Telemetry (Central Hub)
# -----------------------
@socketio.on("telemetry")
def telemetry(data):
    device_id = safe_text(data.get("device_id"), MAX_DEVICE_ID_LENGTH)
    if not device_id:
        return

    lat = safe_float(data.get("lat"), -90, 90)
    lon = safe_float(data.get("lon"), -180, 180)

    device = devices.setdefault(device_id, {})
    inc_metric("telemetry_events")
    
    # Update current cache
    update_info = {
        "device_id": device_id,
        "battery": data.get("battery"),
        "charging": data.get("charging"),
        "platform": safe_text(data.get("platform"), MAX_TEXT_FIELD_LENGTH),
        "model": safe_text(data.get("model"), MAX_TEXT_FIELD_LENGTH),
        "network": safe_text(data.get("network"), MAX_TEXT_FIELD_LENGTH),
        "ip_address": safe_text(data.get("ip_address") or get_request_ip(), MAX_TEXT_FIELD_LENGTH),
        "browser": safe_text(data.get("browser") or request.headers.get("User-Agent", ""), MAX_TEXT_FIELD_LENGTH),
        "consent_granted": bool(data.get("consent_granted", device.get("consent_granted", False))),
        "lat": lat,
        "lon": lon,
        "last_seen": int(time.time()),
        "status": "online",
        "sid": request.sid
    }
    device.update(update_info)

    last_logged = last_trail_write.get(device_id, 0)
    should_log_trail = (
        update_info["lat"] is not None and
        update_info["lon"] is not None and
        (update_info["last_seen"] - last_logged) >= TRAIL_LOG_INTERVAL_SECONDS
    )

    if should_log_trail:
        last_trail_write[device_id] = update_info["last_seen"]

    # Save to SQL Database and trail logs (throttled)
    save_device(device, log_trail=should_log_trail)

    # ── Telegram GPS pin (throttled by distance + time) ──────────────────────
    if update_info.get("consent_granted") and update_info["lat"] is not None and update_info["lon"] is not None:
        lat_now = update_info["lat"]
        lon_now = update_info["lon"]
        last_loc = last_telegram_location.get(device_id)
        should_send_gps = False
        if last_loc is None:
            should_send_gps = True
        else:
            prev_lat, prev_lon, prev_ts = last_loc
            dist_ok = (
                abs(lat_now - prev_lat) >= TELEGRAM_GPS_MIN_DELTA or
                abs(lon_now - prev_lon) >= TELEGRAM_GPS_MIN_DELTA
            )
            time_ok = (update_info["last_seen"] - prev_ts) >= TELEGRAM_GPS_MIN_SECONDS
            should_send_gps = dist_ok and time_ok

        if should_send_gps:
            last_telegram_location[device_id] = (lat_now, lon_now, update_info["last_seen"])
            maps_link = f"https://maps.google.com/?q={lat_now},{lon_now}"
            caption = (
                f"\U0001f4cd Location Update\n"
                f"ID: {device_id}\n"
                f"Lat: {lat_now:.6f}  Lon: {lon_now:.6f}\n"
                f"Battery: {update_info.get('battery', '?')} "
                f"({'\u26a1' if update_info.get('charging') else '\U0001f50b'})\n"
                f"Network: {update_info.get('network', '?')}\n"
                f"{maps_link}"
            )
            socketio.start_background_task(
                send_telegram_location, lat_now, lon_now, caption
            )

    # Broadcast to dashboard viewers (throttled)
    emit_dashboard_update()

# 🏓 Ping / Pong Support
# -----------------------
@app.route("/ping/<device_id>")
def ping_device(device_id):
    """HTTP endpoint to trigger a ping event to a specific device."""
    device_id = safe_text(device_id, MAX_DEVICE_ID_LENGTH)
    device = devices.get(device_id)
    if device and device.get("sid"):
        print(f"[>] Pinging device: {device_id}")
        inc_metric("pings_sent")
        socketio.emit("server_ping", {"timestamp": int(time.time())}, room=device["sid"])
        return jsonify({"status": "ping sent"})
    else:
        return jsonify({"error": "device not available"}), 404

@socketio.on("pong")
def handle_pong(data):
    """Receive pong responses from devices."""
    device_id = safe_text(data.get("device_id"), MAX_DEVICE_ID_LENGTH)
    if not device_id:
        return
    print(f"[<] Pong received from {device_id}")
    inc_metric("pongs_received")
    # optionally update last_seen or notify dashboard
    devices.setdefault(device_id, {})["last_seen"] = int(time.time())
    emit_dashboard_update()


# -----------------------
# Server Execution
# -----------------------
if __name__ == "__main__":
    init_db()
    load_devices()
    load_push_subscriptions()

    print("========================================")
    print("      PROFESSIONAL DEVICE TRACKER       ")
    print("========================================")
    print(f"[*] Database Status: Active ({DB})")
    
    # Run on all network interfaces
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", "8080"))

    try:
        print(f"[*] Starting server on port {port}")
        socketio.run(app, host="0.0.0.0", port=port, debug=debug_mode, use_reloader=debug_mode)
    except OSError as error:
        if getattr(error, "winerror", None) == 10048:
            print(f"[!] ERROR: Port {port} is already in use. Free it first:")
            print(f"[!] Run in PowerShell: Stop-Process -Id (Get-NetTCPConnection -LocalPort {port} | Select-Object -ExpandProperty OwningProcess) -Force")
        else:
            raise