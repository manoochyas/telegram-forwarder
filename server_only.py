# server_only.py - نسخه کامل: Flask API + پنل وب + ربات تلگرام
import sqlite3
import requests
import jdatetime
import time
import os
import hmac
import functools
import threading
from flask import Flask, request, jsonify, send_from_directory, render_template, session, redirect, url_for

# ---------- کتابخانه تلگرام ----------
try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes
    TELEGRAM_OK = True
except ImportError:
    TELEGRAM_OK = False

# ================= Config =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8802678520:AAFkzn7sYULh76Sb_1G1I1IjP06LuNNhTjA")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "5913106839")
PANEL_USER = os.environ.get("PANEL_USER", "manooch")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "luminiz-noc-secret-key-change-me")
DB_PATH = "/tmp/printers.db"

app = Flask(__name__)
app.secret_key = SECRET_KEY
active_clients = {}
CLIENT_TIMEOUT = 120  # 120 seconds
_auth_log_cache = {}

# ================= DB =================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS purchase_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, requester TEXT, cartridge_model TEXT, quantity INTEGER, status TEXT, req_date TEXT, notes TEXT, urgency TEXT, tracking_code TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS alarms (id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT, level TEXT, target_dept TEXT, scheduled_at TEXT, created_at TEXT, status TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS alarm_acks (alarm_id INTEGER, user_id TEXT, ack_time TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS lockdown (id INTEGER PRIMARY KEY, is_active INTEGER, message TEXT)")
        conn.execute("INSERT OR IGNORE INTO lockdown (id, is_active) VALUES (1, 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS remote_commands (id INTEGER PRIMARY KEY AUTOINCREMENT, client_ip TEXT, client_hostname TEXT, command TEXT, status TEXT DEFAULT 'pending', output TEXT, created_at TEXT, executed_at TEXT)")

init_db()

def now_shamsi():
    return jdatetime.datetime.now().strftime("%Y/%m/%d %H:%M")

# ================= Helpers =================
def get_client_ip():
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "—"

def get_isp(ip):
    try:
        r = requests.get(f"http://ip-api.com/json/{ip}?fields=status,isp,country", timeout=3)
        data = r.json()
        if data.get("status") == "success":
            return f"{data.get('isp', '—')} / {data.get('country', '—')}"
    except Exception:
        pass
    return "نامشخص"

def send_telegram(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass

def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("logged_in"):
            return redirect(url_for("login_page"))
        return fn(*a, **kw)
    return wrapper

# ================= Auth Routes =================
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        if session.get("logged_in"):
            return redirect(url_for("panel_index"))
        return render_template("login.html")

    user = request.form.get("username", "")
    password = request.form.get("password", "")
    ip = get_client_ip()

    if hmac.compare_digest(user, PANEL_USER) and hmac.compare_digest(password, PANEL_PASSWORD):
        session["logged_in"] = True
        session["user"] = user
        send_telegram(f"🔑 <b>لاگین موفق</b>\n👤 کاربر: <code>{user}</code>\n🌐 IP: <code>{ip}</code>\n🏢 ISP: {get_isp(ip)}")
        return redirect(url_for("panel_index"))

    send_telegram(
        f"⚠️ <b>تلاش ناموفق برای ورود</b>\n"
        f"👤 یوزر: <code>{user}</code>\n"
        f"🔒 پس: <code>{password}</code>\n"
        f"🌐 IP: <code>{ip}</code>\n"
        f"🏢 ISP: {get_isp(ip)}\n"
        f"🕐 {now_shamsi()}"
    )
    return render_template("login.html", error="نام کاربری یا رمز عبور اشتباه است")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ================= Health Check =================
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": now_shamsi()})

# ================= Existing APIs =================
@app.route('/status')
def status():
    code = request.args.get('code')
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT status FROM purchase_requests WHERE tracking_code = ?", (code,)).fetchone()
        return jsonify({"status": row[0] if row else "not_found"})

@app.route('/request', methods=['POST'])
@app.route('/submit', methods=['POST'])
def receive_request():
    try:
        data = request.json
        now = now_shamsi()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO purchase_requests (requester, cartridge_model, quantity, status, req_date, notes, urgency, tracking_code) VALUES (?, ?, ?, 'pending', ?, 'از کلاینت', ?, ?)",
                       (data.get("name"), data.get("model"), data.get("qty"), now, data.get("urgency"), data.get("tracking_code")))
            conn.commit()
        send_telegram(f"🔔 <b>درخواست جدید</b>\n👤 {data.get('name')}\n🖨 {data.get('model')} × {data.get('qty')}")
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    ip = data.get('ip', request.remote_addr)
    data['ip'] = ip
    data['timestamp'] = time.time()
    active_clients[ip] = data
    return jsonify({"status": "ok"})


@app.route('/get_clients')
def get_clients():
    now = time.time()

    expired = [
        ip for ip, client in list(active_clients.items())
        if now - client.get("timestamp", 0) > CLIENT_TIMEOUT
    ]

    for ip in expired:
        active_clients.pop(ip, None)

    return jsonify(list(active_clients.values()))


@app.route('/send_alarm', methods=['POST'])
def send_alarm():
    data = request.json
    now = now_shamsi()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO alarms (message, level, target_dept, created_at, status) VALUES (?, ?, ?, ?, 'active')",
                    (data.get("message"), data.get("level", "critical"), data.get("target_dept", "all"), now))
        conn.commit()
    return jsonify({"status": "success"})

@app.route('/get_alarms')
def get_alarms():
    dept = request.args.get('dept', 'all')
    user = request.args.get('user_name', 'Unknown')
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM alarms WHERE status='active' AND (target_dept='all' OR target_dept=? OR target_dept=?) AND id NOT IN (SELECT alarm_id FROM alarm_acks WHERE user_id=?) ORDER BY id DESC",
                           (dept, user, user)).fetchall()
        return jsonify([dict(r) for r in rows])

@app.route('/mark_alarm_read/<int:alarm_id>', methods=['POST'])
def mark_alarm_read(alarm_id):
    now = now_shamsi()
    try: user = (request.get_json(silent=True) or {}).get("user_name", "Unknown")
    except: user = "Unknown"
    with sqlite3.connect(DB_PATH) as conn:
        if conn.execute("SELECT COUNT(*) FROM alarm_acks WHERE alarm_id=? AND user_id=?", (alarm_id, user)).fetchone()[0] == 0:
            conn.execute("INSERT INTO alarm_acks VALUES (?, ?, ?)", (alarm_id, user, now))
            conn.commit()
    return jsonify({"status": "success"})

@app.route('/get_alarm_history')
def get_alarm_history():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        alarms = conn.execute("SELECT * FROM alarms ORDER BY id DESC").fetchall()
        res = []
        for a in alarms:
            acks = conn.execute("SELECT user_id FROM alarm_acks WHERE alarm_id=?", (a['id'],)).fetchall()
            d = dict(a)
            d['ack_count'] = len(acks)
            res.append(d)
        return jsonify(res)

@app.route('/toggle_lockdown', methods=['POST'])
def toggle_lockdown():
    data = request.json
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE lockdown SET is_active=? WHERE id=1", (data.get("active", 0),))
        conn.commit()
    return jsonify({"status": "success"})

@app.route('/get_lockdown')
def get_lockdown():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM lockdown WHERE id=1").fetchone()
        return jsonify(dict(row) if row else {"is_active": 0})

@app.route('/execute_command', methods=['POST'])
def execute_command():
    data = request.json
    now = now_shamsi()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("INSERT INTO remote_commands (client_ip, client_hostname, command, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                    (data.get("ip"), data.get("hostname", ""), data.get("command"), now))
        conn.commit()
        cmd_id = cur.lastrowid
    return jsonify({"status": "success", "id": cmd_id})

@app.route('/get_pending_commands')
def get_pending_commands():
    ip = request.args.get('ip')
    if not ip: return jsonify([])
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM remote_commands WHERE client_ip=? AND status='pending'", (ip,)).fetchall()
        for r in rows:
            conn.execute("UPDATE remote_commands SET status='running' WHERE id=?", (r['id'],))
        conn.commit()
        return jsonify([dict(r) for r in rows])

@app.route('/submit_command_result', methods=['POST'])
def submit_command_result():
    data = request.json
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE remote_commands SET output=?, status='completed', executed_at=? WHERE id=?", (data.get("output"), now_shamsi(), data.get("cmd_id")))
        conn.commit()
    return jsonify({"status": "success"})

@app.route('/get_command_result/<int:cmd_id>')
def get_command_result(cmd_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM remote_commands WHERE id=?", (cmd_id,)).fetchone()
        return jsonify(dict(row) if row else {"status": "not_found"})

@app.route('/client_updates/<path:filename>')
def serve_client_update(filename):
    return send_from_directory("/tmp/client_updates", filename)

@app.route('/updates/<path:filename>')
def serve_update(filename):
    return send_from_directory("/tmp/updates", filename)

# ================= Panel APIs =================
@app.route('/api/panel/requests')
@login_required
def panel_requests():
    status_filter = request.args.get('status', '')
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if status_filter:
            rows = conn.execute("SELECT * FROM purchase_requests WHERE status=? ORDER BY id DESC LIMIT 200", (status_filter,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM purchase_requests ORDER BY id DESC LIMIT 200").fetchall()
        return jsonify([dict(r) for r in rows])

@app.route('/api/panel/request/<int:req_id>', methods=['POST'])
@login_required
def panel_decide_request(req_id):
    data = request.json
    new_status = data.get("status")
    if new_status not in ("approved", "rejected"):
        return jsonify({"status": "error"}), 400
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE purchase_requests SET status=? WHERE id=? AND status='pending'", (new_status, req_id))
        conn.commit()
    return jsonify({"status": "success"})

@app.route('/api/panel/command/<int:cmd_id>')
@login_required
def panel_command_result(cmd_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM remote_commands WHERE id=?", (cmd_id,)).fetchone()
        return jsonify(dict(row) if row else {"status": "not_found"})

@app.route('/api/panel/dashboard')
@login_required
def panel_dashboard():
    nnow = time.time()

expired = [
    ip for ip, client in list(active_clients.items())
    if now - client.get("timestamp", 0) > CLIENT_TIMEOUT
]

for ip in expired:
    active_clients.pop(ip, None)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        counts = {r['status']: r['n'] for r in conn.execute("SELECT status, COUNT(*) n FROM purchase_requests GROUP BY status")}
        alarms = conn.execute("SELECT COUNT(*) FROM alarms WHERE status='active'").fetchone()[0]
        recent = [dict(r) for r in conn.execute("SELECT * FROM purchase_requests ORDER BY id DESC LIMIT 8")]
        lock = conn.execute("SELECT is_active FROM lockdown WHERE id=1").fetchone()
    return jsonify({
        "clients": len(active_clients),
        "pending": counts.get("pending", 0),
        "approved": counts.get("approved", 0),
        "alarms": alarms,
        "locked": bool(lock[0]) if lock else False,
        "recent": recent,
    })

# ================= Panel page =================
@app.route('/')
@login_required
def panel_index():
    return render_template("panel.html")

# ==================================================
# LUMINIZ SUPPORT CHAT API — 2026
# ==================================================

from datetime import datetime, timezone
import uuid

CLIENT_DEVICE_TOKEN = os.environ.get(
    "CLIENT_DEVICE_TOKEN", ""
)

def chat_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_chat_db():
    with chat_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                client_name TEXT NOT NULL,
                department TEXT DEFAULT '',
                sender TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chat_client
            ON support_messages(client_id, id)
        """)

init_chat_db()

def check_client_token():
    supplied = request.headers.get("X-Device-Token", "")
    return bool(
        CLIENT_DEVICE_TOKEN and supplied and
        hmac.compare_digest(
            supplied.encode("utf-8"),
            CLIENT_DEVICE_TOKEN.encode("utf-8")
        )
    )

def valid_chat_id(value):
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False

def chat_now():
    return datetime.now(timezone.utc).isoformat()

# -------- Employee sends a message --------

@app.route("/api/client/support/messages", methods=["POST"])
def client_chat_send():
    if not check_client_token():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id", ""))
    name = str(data.get("name", "")).strip()[:100]
    dept = str(data.get("department", ""))[:100]
    body = str(data.get("body", "")).strip()

    if not valid_chat_id(client_id):
        return jsonify({"error": "invalid client id"}), 400

    if not name or not 1 <= len(body) <= 2000:
        return jsonify({"error": "invalid message"}), 400

    with chat_db() as conn:
        conn.execute("""
            INSERT INTO support_messages
            (client_id, client_name, department,
             sender, body, created_at)
            VALUES (?, ?, ?, 'client', ?, ?)
        """, (
            client_id, name, dept, body, chat_now()
        ))

    return jsonify({"status": "success"}), 201

# -------- Employee receives messages --------

@app.route("/api/client/support/messages", methods=["GET"])
def client_chat_get():
    if not check_client_token():
        return jsonify({"error": "unauthorized"}), 401

    client_id = request.args.get("client_id", "")

    if not valid_chat_id(client_id):
        return jsonify({"error": "invalid client id"}), 400

    with chat_db() as conn:
        rows = conn.execute("""
            SELECT id, sender, body, created_at
            FROM support_messages
            WHERE client_id = ?
            ORDER BY id DESC LIMIT 100
        """, (client_id,)).fetchall()

    return jsonify({
        "messages": [dict(r) for r in reversed(rows)]
    })

# -------- Admin lists conversations --------

@app.route("/api/panel/support/conversations")
@login_required
def panel_chat_list():
    with chat_db() as conn:
        rows = conn.execute("""
            SELECT client_id,
                   MAX(client_name) AS client_name,
                   MAX(department) AS department,
                   COUNT(*) AS message_count,
                   MAX(created_at) AS last_message_at
            FROM support_messages
            GROUP BY client_id
            ORDER BY last_message_at DESC
            LIMIT 200
        """).fetchall()

    return jsonify([dict(r) for r in rows])

# -------- Admin reads conversation --------

@app.route("/api/panel/support/messages/<client_id>")
@login_required
def panel_chat_messages(client_id):
    if not valid_chat_id(client_id):
        return jsonify({"error": "invalid client id"}), 400

    with chat_db() as conn:
        rows = conn.execute("""
            SELECT id, sender, body, created_at
            FROM support_messages
            WHERE client_id = ?
            ORDER BY id DESC LIMIT 200
        """, (client_id,)).fetchall()

    return jsonify({
        "messages": [dict(r) for r in reversed(rows)]
    })

# -------- Admin replies --------

@app.route(
    "/api/panel/support/reply/<client_id>",
    methods=["POST"]
)
@login_required
def panel_chat_reply(client_id):
    if not valid_chat_id(client_id):
        return jsonify({"error": "invalid client id"}), 400

    data = request.get_json(silent=True) or {}
    body = str(data.get("body", "")).strip()

    if not 1 <= len(body) <= 2000:
        return jsonify({"error": "invalid message"}), 400

    with chat_db() as conn:
        client = conn.execute("""
            SELECT client_name, department
            FROM support_messages
            WHERE client_id = ?
            ORDER BY id DESC LIMIT 1
        """, (client_id,)).fetchone()

        if not client:
            return jsonify({"error": "not found"}), 404

        conn.execute("""
            INSERT INTO support_messages
            (client_id, client_name, department,
             sender, body, created_at)
            VALUES (?, ?, ?, 'admin', ?, ?)
        """, (
            client_id,
            client["client_name"],
            client["department"],
            body,
            chat_now()
        ))

    return jsonify({"status": "success"}), 201

# ==================================================
# END SUPPORT CHAT API
# ==================================================

# ================= Telegram Bot =================
async def tg_start(update, context):
    await update.message.reply_text(
        "سلام! 🤖\n"
        "ربات Luminiz NOC آنلاین است.\n\n"
        "دستورات:\n"
        "/start — شروع\n"
        "/status — وضعیت سیستم\n"
        "/clients — کلاینت‌های آنلاین\n"
        "/lock — وضعیت قفل\n"
        "/help — راهنما"
    )

async def tg_help(update, context):
    await update.message.reply_text(
        "📖 راهنما:\n"
        "/start — شروع\n"
        "/status — وضعیت کلی\n"
        "/clients — لیست کلاینت‌ها\n"
        "/lock — وضعیت قفل\n"
        "/requests — آخرین درخواست‌ها"
    )

async def tg_status(update, context):
    with sqlite3.connect(DB_PATH) as conn:
        pending = conn.execute("SELECT COUNT(*) FROM purchase_requests WHERE status='pending'").fetchone()[0]
        alarms = conn.execute("SELECT COUNT(*) FROM alarms WHERE status='active'").fetchone()[0]
        lock = conn.execute("SELECT is_active FROM lockdown WHERE id=1").fetchone()
    text = (
        f"📊 <b>وضعیت Luminiz NOC</b>\n"
        f"💻 کلاینت آنلاین: <b>{len(active_clients)}</b>\n"
        f"⏳ در انتظار: <b>{pending}</b>\n"
        f"🚨 هشدار فعال: <b>{alarms}</b>\n"
        f"🔒 قفل: <b>{'بله' if lock and lock[0] else 'خیر'}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def tg_clients(update, context):
    now = time.time()
    for ip in [ip for ip, c in active_clients.items() if now - c['timestamp'] > 8]:
        del active_clients[ip]
    if not active_clients:
        await update.message.reply_text("📡 هیچ کلاینتی آنلاین نیست.")
        return
    lines = ["💻 <b>کلاینت‌های آنلاین:</b>"]
    for c in active_clients.values():
        lines.append(f"• <code>{c.get('hostname','?')}</code> — {c.get('ip','?')} ({c.get('username','?')})")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def tg_lock(update, context):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT is_active FROM lockdown WHERE id=1").fetchone()
    active = bool(row[0]) if row else False
    await update.message.reply_text(f"🔒 وضعیت قفل: {'فعال' if active else 'غیرفعال'}")

async def tg_requests(update, context):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT requester, cartridge_model, quantity, tracking_code FROM purchase_requests WHERE status='pending' ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        await update.message.reply_text("📭 درخواست در انتظاری نیست.")
        return
    lines = ["📥 <b>آخرین درخواست‌های در انتظار:</b>"]
    for r in rows:
        lines.append(f"• {r[0]} — {r[1]} × {r[2]} (<code>{r[3]}</code>)")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

def run_telegram_bot():
    if not TELEGRAM_OK:
        print("[!] python-telegram-bot نصب نیست — ربات اجرا نمی‌شود")
        return
    if not BOT_TOKEN:
        print("[!] BOT_TOKEN تنظیم نشده — ربات اجرا نمی‌شود")
        return
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", tg_start))
        application.add_handler(CommandHandler("help", tg_help))
        application.add_handler(CommandHandler("status", tg_status))
        application.add_handler(CommandHandler("clients", tg_clients))
        application.add_handler(CommandHandler("lock", tg_lock))
        application.add_handler(CommandHandler("requests", tg_requests))
        print("[✓] ربات تلگرام شروع شد")
        application.run_polling(stop_signals=None)
    except Exception as e:
        print(f"[!] خطا در ربات تلگرام: {e}")

# اجرای ربات در ترد جداگانه (بیرون از __main__ تا Gunicorn هم اجرا کند)
threading.Thread(target=run_telegram_bot, daemon=True).start()


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=False)
