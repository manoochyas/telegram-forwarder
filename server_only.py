
# ============================================================
# LUMINIZ ENTERPRISE NOC - SERVER 2026
# Flask + Dashboard + Clients + Requests + Support Chat
# ============================================================

import os
import time
import json
import uuid
import hmac
import html
import secrets
import sqlite3
import logging
import functools
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests
import jdatetime

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    redirect,
    url_for,
    session,
    send_from_directory
)


# ============================================================
# CONFIGURATION
# ============================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("luminiz")

DB_PATH = os.getenv("DB_PATH", "/tmp/printers.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

CLIENT_TIMEOUT = max(
    60,
    int(os.getenv("CLIENT_TIMEOUT", "120"))
)

DEVICE_TOKEN = os.getenv(
    "CLIENT_DEVICE_TOKEN", ""
).strip()

REQUIRE_DEVICE_TOKEN = (
    os.getenv("REQUIRE_DEVICE_TOKEN", "0") == "1"
)

PANEL_USER = os.getenv(
    "PANEL_USER", "manooch"
).strip()

PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

secret = os.getenv("SECRET_KEY")

if not secret:
    secret = secrets.token_hex(32)
    log.warning(
        "SECRET_KEY missing: sessions reset on restart"
    )

app = Flask(__name__)

app.config.update(
    SECRET_KEY=secret,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        os.getenv("COOKIE_SECURE", "1") != "0"
    ),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=64 * 1024
)

if not PANEL_USER or not PANEL_PASSWORD:
    log.warning("Panel credentials not configured")

if not DEVICE_TOKEN:
    log.warning(
        "CLIENT_DEVICE_TOKEN missing: chat unavailable"
    )

if not REQUIRE_DEVICE_TOKEN:
    log.warning(
        "Legacy client APIs are open until device "
        "authentication is enforced"
    )


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS purchase_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester TEXT,
                cartridge_model TEXT,
                quantity INTEGER,
                status TEXT,
                req_date TEXT,
                notes TEXT,
                urgency TEXT,
                tracking_code TEXT
            );

            CREATE TABLE IF NOT EXISTS alarms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT,
                level TEXT,
                target_dept TEXT,
                scheduled_at TEXT,
                created_at TEXT,
                status TEXT
            );

            CREATE TABLE IF NOT EXISTS alarm_acks (
                alarm_id INTEGER,
                user_id TEXT,
                ack_time TEXT
            );

            CREATE TABLE IF NOT EXISTS lockdown (
                id INTEGER PRIMARY KEY,
                is_active INTEGER,
                message TEXT
            );

            INSERT OR IGNORE INTO lockdown
                (id, is_active)
            VALUES (1, 0);

            CREATE TABLE IF NOT EXISTS remote_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_ip TEXT,
                client_hostname TEXT,
                command TEXT,
                status TEXT DEFAULT 'pending',
                output TEXT,
                created_at TEXT,
                executed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS client_presence (
                client_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                last_seen REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_presence_seen
            ON client_presence(last_seen);

            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                client_name TEXT NOT NULL,
                department TEXT DEFAULT '',
                sender TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_support_client
            ON support_messages(client_id, id);
        """)


init_db()


# ============================================================
# UTILITIES
# ============================================================

def now_shamsi():
    return jdatetime.datetime.now().strftime(
        "%Y/%m/%d %H:%M"
    )


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def json_input():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def device_authenticated():
    supplied = request.headers.get(
        "X-Device-Token", ""
    )

    return bool(
        DEVICE_TOKEN
        and supplied
        and hmac.compare_digest(supplied, DEVICE_TOKEN)
    )


def device_required_for_legacy():
    if REQUIRE_DEVICE_TOKEN and not device_authenticated():
        return jsonify({
            "error": "device authentication required"
        }), 401

    return None


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):

            api_paths = (
                "/get_clients",
                "/get_alarm_history",
                "/send_alarm",
                "/toggle_lockdown",
                "/execute_command"
            )

            if (
                request.path.startswith("/api/")
                or request.path in api_paths
            ):
                return jsonify({
                    "error": "login required"
                }), 401

            return redirect(url_for("login_page"))

        return fn(*args, **kwargs)

    return wrapper


def same_origin():
    source = (
        request.headers.get("Origin")
        or request.headers.get("Referer")
    )

    if not source:
        return False

    parsed = urlsplit(source)

    return (
        parsed.netloc.lower() == request.host.lower()
        and parsed.scheme in ("http", "https")
    )


@app.before_request
def protect_admin_changes():
    if request.method != "POST":
        return None

    admin_paths = (
        request.path.startswith("/api/panel/")
        or request.path in (
            "/send_alarm",
            "/toggle_lockdown",
            "/execute_command"
        )
    )

    if admin_paths and not same_origin():
        return jsonify({
            "error": "invalid request origin"
        }), 403

    return None


# ============================================================
# TELEGRAM NOTIFICATIONS
# ============================================================

def notify_telegram(message):
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=3
        )

        response.raise_for_status()

    except requests.RequestException:
        log.warning("Telegram notification failed")


# ============================================================
# AUTHENTICATION
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login_page():

    if request.method == "GET":
        if session.get("logged_in"):
            return redirect(url_for("panel_index"))

        return render_template("login.html")

    user = request.form.get("username", "")
    password = request.form.get("password", "")

    valid = (
        bool(PANEL_USER and PANEL_PASSWORD)
        and hmac.compare_digest(user, PANEL_USER)
        and hmac.compare_digest(
            password, PANEL_PASSWORD
        )
    )

    if valid:
        session.clear()

        session["logged_in"] = True
        session["user"] = user
        session.permanent = True

        notify_telegram(
            "🔑 ورود مدیر به پنل Luminiz"
        )

        return redirect(url_for("panel_index"))

    return render_template(
        "login.html",
        error="نام کاربری یا رمز عبور اشتباه است"
    ), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/")
@login_required
def panel_index():
    return render_template("panel.html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "time": now_shamsi()
    })


# ============================================================
# CLIENT PRESENCE
# ============================================================

def client_key(payload):
    client_id = payload.get("client_id")

    if valid_uuid(client_id):
        return "id:" + str(uuid.UUID(str(client_id)))

    hostname = str(
        payload.get("hostname") or "unknown"
    )[:100]

    ip = str(
        payload.get("ip") or "unknown"
    )[:64]

    return "legacy:" + hostname + "|" + ip


def online_clients():
    limit = time.time() - CLIENT_TIMEOUT

    with db() as conn:
        rows = conn.execute(
            """
            SELECT payload_json, last_seen
            FROM client_presence
            WHERE last_seen >= ?
            ORDER BY last_seen DESC
            LIMIT 1000
            """,
            (limit,)
        ).fetchall()

    result = []

    for row in rows:
        try:
            value = json.loads(row["payload_json"])
            value["timestamp"] = row["last_seen"]
            result.append(value)

        except (TypeError, json.JSONDecodeError):
            continue

    return result


@app.route("/heartbeat", methods=["POST"])
def heartbeat():

    auth_error = device_required_for_legacy()

    if auth_error:
        return auth_error

    payload = json_input()

    if not payload.get("hostname"):
        return jsonify({
            "error": "hostname required"
        }), 400

    payload["hostname"] = str(
        payload["hostname"]
    )[:100]

    payload["ip"] = str(
        payload.get("ip")
        or request.remote_addr
        or ""
    )[:64]

    payload["timestamp"] = time.time()

    serialized = json.dumps(
        payload,
        ensure_ascii=False
    )

    if len(serialized) > 48000:
        return jsonify({
            "error": "heartbeat too large"
        }), 413

    with db() as conn:
        conn.execute(
            """
            INSERT INTO client_presence
                (client_key, payload_json, last_seen)
            VALUES (?, ?, ?)

            ON CONFLICT(client_key)
            DO UPDATE SET
                payload_json=excluded.payload_json,
                last_seen=excluded.last_seen
            """,
            (
                client_key(payload),
                serialized,
                payload["timestamp"]
            )
        )

        conn.execute(
            """
            DELETE FROM client_presence
            WHERE last_seen < ?
            """,
            (time.time() - 86400,)
        )

    return jsonify({"status": "ok"})


@app.route("/get_clients")
@login_required
def get_clients():
    return jsonify(online_clients())


# ============================================================
# CARTRIDGE REQUESTS
# ============================================================

@app.route("/request", methods=["POST"])
@app.route("/submit", methods=["POST"])
def receive_request():

    auth_error = device_required_for_legacy()

    if auth_error:
        return auth_error

    data = json_input()

    name = str(
        data.get("name") or ""
    ).strip()[:100]

    model = str(
        data.get("model") or ""
    ).strip()[:150]

    notes = str(
        data.get("notes") or ""
    ).strip()[:2000]

    urgency = str(
        data.get("urgency") or "عادی"
    ).strip()[:30]

    tracking = str(
        data.get("tracking_code") or ""
    ).strip()[:80]

    try:
        qty = int(data.get("qty", 0))
    except (ValueError, TypeError):
        qty = 0

    if not name or not model or not 1 <= qty <= 100:
        return jsonify({
            "error": "invalid request"
        }), 400

    if not tracking:
        tracking = "TRK-" + secrets.token_hex(6).upper()

    with db() as conn:

        already = conn.execute(
            """
            SELECT id FROM purchase_requests
            WHERE tracking_code=?
            """,
            (tracking,)
        ).fetchone()

        if already:
            return jsonify({
                "status": "success",
                "tracking_code": tracking,
                "duplicate": True
            })

        conn.execute(
            """
            INSERT INTO purchase_requests
            (
                requester,
                cartridge_model,
                quantity,
                status,
                req_date,
                notes,
                urgency,
                tracking_code
            )
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                name,
                model,
                qty,
                now_shamsi(),
                notes,
                urgency,
                tracking
            )
        )

    notify_telegram(
        "📥 درخواست کارتریج جدید\n"
        f"👤 {html.escape(name)}\n"
        f"🖨 {html.escape(model)} × {qty}\n"
        f"🔖 {html.escape(tracking)}"
    )

    return jsonify({
        "status": "success",
        "tracking_code": tracking
    })


@app.route("/status")
def request_status():
    code = (
        request.args.get("code") or ""
    )[:80]

    if not code:
        return jsonify({"status": "not_found"})

    with db() as conn:
        row = conn.execute(
            """
            SELECT status
            FROM purchase_requests
            WHERE tracking_code=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (code,)
        ).fetchone()

    return jsonify({
        "status": row["status"] if row else "not_found"
    })


# ============================================================
# ADMIN REQUEST MANAGEMENT
# ============================================================

@app.route("/api/panel/requests")
@login_required
def panel_requests():

    status = request.args.get("status", "")

    if status and status not in (
        "pending", "approved", "rejected"
    ):
        return jsonify({
            "error": "invalid status"
        }), 400

    with db() as conn:

        if status:
            rows = conn.execute(
                """
                SELECT * FROM purchase_requests
                WHERE status=?
                ORDER BY id DESC
                LIMIT 200
                """,
                (status,)
            ).fetchall()

        else:
            rows = conn.execute(
                """
                SELECT * FROM purchase_requests
                ORDER BY id DESC
                LIMIT 200
                """
            ).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route(
    "/api/panel/request/<int:req_id>",
    methods=["POST"]
)
@login_required
def panel_decide_request(req_id):

    new_status = json_input().get("status")

    if new_status not in (
        "approved", "rejected"
    ):
        return jsonify({
            "error": "invalid status"
        }), 400

    with db() as conn:
        cur = conn.execute(
            """
            UPDATE purchase_requests
            SET status=?
            WHERE id=?
            AND status='pending'
            """,
            (new_status, req_id)
        )

    if not cur.rowcount:
        return jsonify({
            "error": "request not found or already handled"
        }), 409

    return jsonify({"status": "success"})


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/api/panel/dashboard")
@login_required
def panel_dashboard():

    with db() as conn:

        counts = {
            r["status"]: r["n"]
            for r in conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM purchase_requests
                GROUP BY status
                """
            )
        }

        alarms = conn.execute(
            """
            SELECT COUNT(*)
            FROM alarms
            WHERE status='active'
            """
        ).fetchone()[0]

        recent = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM purchase_requests
                ORDER BY id DESC
                LIMIT 8
                """
            )
        ]

        row = conn.execute(
            """
            SELECT is_active
            FROM lockdown
            WHERE id=1
            """
        ).fetchone()

    return jsonify({
        "clients": len(online_clients()),
        "pending": counts.get("pending", 0),
        "approved": counts.get("approved", 0),
        "alarms": alarms,
        "locked": bool(row[0]) if row else False,
        "recent": recent
    })


# ============================================================
# ALARMS
# ============================================================

@app.route("/send_alarm", methods=["POST"])
@login_required
def send_alarm():

    data = json_input()

    message = str(
        data.get("message") or ""
    ).strip()

    level = data.get("level", "critical")

    dept = str(
        data.get("target_dept") or "all"
    )[:100]

    if (
        not 1 <= len(message) <= 2000
        or level not in ("info", "warning", "critical")
    ):
        return jsonify({
            "error": "invalid alarm"
        }), 400

    with db() as conn:
        conn.execute(
            """
            INSERT INTO alarms
            (
                message, level, target_dept,
                created_at, status
            )
            VALUES (?, ?, ?, ?, 'active')
            """,
            (
                message,
                level,
                dept,
                now_shamsi()
            )
        )

    return jsonify({"status": "success"})


@app.route("/get_alarms")
def get_alarms():

    auth_error = device_required_for_legacy()

    if auth_error:
        return auth_error

    dept = (
        request.args.get("dept") or "all"
    )[:100]

    user = (
        request.args.get("user_name") or "Unknown"
    )[:100]

    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM alarms
            WHERE status='active'
            AND (
                target_dept='all'
                OR target_dept=?
                OR target_dept=?
            )
            AND id NOT IN (
                SELECT alarm_id FROM alarm_acks
                WHERE user_id=?
            )
            ORDER BY id DESC
            LIMIT 100
            """,
            (dept, user, user)
        ).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route(
    "/mark_alarm_read/<int:alarm_id>",
    methods=["POST"]
)
def mark_alarm_read(alarm_id):

    auth_error = device_required_for_legacy()

    if auth_error:
        return auth_error

    user = str(
        json_input().get("user_name") or "Unknown"
    )[:100]

    with db() as conn:

        exists = conn.execute(
            """
            SELECT 1 FROM alarm_acks
            WHERE alarm_id=?
            AND user_id=?
            """,
            (alarm_id, user)
        ).fetchone()

        if not exists:
            conn.execute(
                """
                INSERT INTO alarm_acks
                    (alarm_id, user_id, ack_time)
                VALUES (?, ?, ?)
                """,
                (
                    alarm_id,
                    user,
                    now_shamsi()
                )
            )

    return jsonify({"status": "success"})


@app.route("/get_alarm_history")
@login_required
def get_alarm_history():

    with db() as conn:
        rows = conn.execute(
            """
            SELECT
                a.*,
                (
                    SELECT COUNT(*)
                    FROM alarm_acks aa
                    WHERE aa.alarm_id=a.id
                ) AS ack_count
            FROM alarms a
            ORDER BY a.id DESC
            LIMIT 200
            """
        ).fetchall()

    return jsonify([dict(r) for r in rows])


# ============================================================
# LOCKDOWN
# ============================================================

@app.route("/get_lockdown")
def get_lockdown():

    auth_error = device_required_for_legacy()

    if auth_error:
        return auth_error

    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM lockdown
            WHERE id=1
            """
        ).fetchone()

    return jsonify(
        dict(row) if row else {"is_active": 0}
    )


@app.route("/toggle_lockdown", methods=["POST"])
@login_required
def toggle_lockdown():

    active = json_input().get("active")

    if active not in (0, 1, False, True):
        return jsonify({
            "error": "invalid lock state"
        }), 400

    with db() as conn:
        conn.execute(
            """
            UPDATE lockdown
            SET is_active=?
            WHERE id=1
            """,
            (int(active),)
        )

    return jsonify({"status": "success"})


# ============================================================
# SUPPORT CHAT - CLIENT
# ============================================================

@app.route(
    "/api/client/support/messages",
    methods=["GET", "POST"]
)
def client_support_messages():

    if not device_authenticated():
        return jsonify({
            "error": "invalid device token"
        }), 401

    if request.method == "POST":
        data = json_input()
    else:
        data = request.args

    client_id = str(
        data.get("client_id") or ""
    )

    if not valid_uuid(client_id):
        return jsonify({
            "error": "invalid client_id"
        }), 400

    if request.method == "POST":

        name = str(
            data.get("name") or ""
        ).strip()[:100]

        dept = str(
            data.get("department") or ""
        ).strip()[:100]

        body = str(
            data.get("body") or ""
        ).strip()

        if not name or not 1 <= len(body) <= 2000:
            return jsonify({
                "error": "invalid message"
            }), 400

        with db() as conn:

            recent = conn.execute(
                """
                SELECT COUNT(*)
                FROM support_messages
                WHERE client_id=?
                AND sender='client'
                AND created_at >= ?
                """,
                (
                    client_id,
                    (
                        datetime.now(timezone.utc)
                        - timedelta(minutes=1)
                    ).isoformat()
                )
            ).fetchone()[0]

            if recent >= 10:
                return jsonify({
                    "error": "too many messages"
                }), 429

            conn.execute(
                """
                INSERT INTO support_messages
                (
                    client_id,
                    client_name,
                    department,
                    sender,
                    body,
                    created_at
                )
                VALUES (?, ?, ?, 'client', ?, ?)
                """,
                (
                    client_id,
                    name,
                    dept,
                    body,
                    now_utc()
                )
            )

        return jsonify({"status": "success"}), 201

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, sender, body, created_at
            FROM support_messages
            WHERE client_id=?
            ORDER BY id DESC
            LIMIT 100
            """,
            (client_id,)
        ).fetchall()

    return jsonify({
        "messages": [
            dict(r) for r in reversed(rows)
        ]
    })


# ============================================================
# SUPPORT CHAT - ADMIN
# ============================================================

@app.route("/api/panel/support/conversations")
@login_required
def panel_support_conversations():

    with db() as conn:
        rows = conn.execute(
            """
            SELECT
                client_id,
                MAX(client_name) AS client_name,
                MAX(department) AS department,
                COUNT(*) AS message_count,
                MAX(created_at) AS last_message_at
            FROM support_messages
            GROUP BY client_id
            ORDER BY last_message_at DESC
            LIMIT 200
            """
        ).fetchall()

    return jsonify([dict(r) for r in rows])


@app.route("/api/panel/support/messages/<client_id>")
@login_required
def panel_support_read(client_id):

    if not valid_uuid(client_id):
        return jsonify({
            "error": "invalid client_id"
        }), 400

    with db() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                client_name,
                department,
                sender,
                body,
                created_at
            FROM support_messages
            WHERE client_id=?
            ORDER BY id DESC
            LIMIT 200
            """,
            (client_id,)
        ).fetchall()

    return jsonify({
        "messages": [
            dict(r) for r in reversed(rows)
        ]
    })


@app.route(
    "/api/panel/support/reply/<client_id>",
    methods=["POST"]
)
@login_required
def panel_support_reply(client_id):

    if not valid_uuid(client_id):
        return jsonify({
            "error": "invalid client_id"
        }), 400

    body = str(
        json_input().get("body") or ""
    ).strip()

    if not 1 <= len(body) <= 2000:
        return jsonify({
            "error": "invalid message"
        }), 400

    with db() as conn:

        client = conn.execute(
            """
            SELECT client_name, department
            FROM support_messages
            WHERE client_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (client_id,)
        ).fetchone()

        if client is None:
            return jsonify({
                "error": "client not found"
            }), 404

        conn.execute(
            """
            INSERT INTO support_messages
            (
                client_id,
                client_name,
                department,
                sender,
                body,
                created_at
            )
            VALUES (?, ?, ?, 'admin', ?, ?)
            """,
            (
                client_id,
                client["client_name"],
                client["department"],
                body,
                now_utc()
            )
        )

    return jsonify({"status": "success"}), 201


# ============================================================
# REMOTE COMMAND SAFETY
# ============================================================

# Unrestricted remote command execution is disabled.
# Read-only diagnostics can be added later with approval
# and independent authorization.

@app.route("/execute_command", methods=["POST"])
@login_required
def execute_command():
    return jsonify({
        "error": "remote shell disabled; use approved diagnostics"
    }), 403


@app.route("/get_pending_commands")
def get_pending_commands():
    return jsonify([])


@app.route(
    "/submit_command_result",
    methods=["POST"]
)
def submit_command_result():
    return jsonify({
        "error": "remote shell disabled"
    }), 403


@app.route("/get_command_result/<int:cmd_id>")
@login_required
def get_command_result(cmd_id):
    return jsonify({"status": "not_found"})


@app.route("/api/panel/command/<int:cmd_id>")
@login_required
def panel_command_result(cmd_id):
    return jsonify({"status": "not_found"})


# ============================================================
# CLIENT UPDATE FILES
# ============================================================

@app.route("/client_updates/<path:filename>")
def serve_client_update(filename):
    directory = os.getenv(
        "CLIENT_UPDATES_DIR",
        "/tmp/client_updates"
    )

    return send_from_directory(
        directory,
        filename
    )


@app.route("/updates/<path:filename>")
def serve_update(filename):
    directory = os.getenv(
        "UPDATES_DIR",
        "/tmp/updates"
    )

    return send_from_directory(
        directory,
        filename
    )


# ============================================================
# APPLICATION START
# ============================================================

# Production:
# gunicorn server_only:app
#
# Telegram polling must run in a separate worker.

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False
    )
