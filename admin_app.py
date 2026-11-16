# -*- coding: utf-8 -*-
r"""
Luminiz Enterprise NOC  v2
- Flask server (clients API)  +  CustomTkinter admin dashboard
- Settings come from environment variables or  <NOC_DIR>\config.json :
      BOT_TOKEN, ADMIN_CHAT_ID, ADMIN_API_KEY (optional), PORT (optional)
"""
import html
import hmac
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import wraps
from tkinter import messagebox, font as tkfont

import customtkinter as ctk
import jdatetime
import requests
from flask import Flask, jsonify, request, send_from_directory

# ============================================================
#  Config
# ============================================================
BASE_DIR = os.environ.get("NOC_DIR", r"C:\TelegramForwarder")
DB_PATH = os.path.join(BASE_DIR, "printers.db")
UPDATES_DIR = os.path.join(BASE_DIR, "updates")


def _load_cfg():
    path = os.path.join(BASE_DIR, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_CFG = _load_cfg()


def setting(key, default=""):
    return os.environ.get(key) or _CFG.get(key) or default


BOT_TOKEN = str(setting("BOT_TOKEN"))
ADMIN_CHAT_ID = str(setting("ADMIN_CHAT_ID"))
ADMIN_API_KEY = str(setting("ADMIN_API_KEY"))
PORT = int(setting("PORT", 5000))

CLIENT_TTL = 8            # seconds without heartbeat -> offline
UPTIME_LIMIT_H = 168      # hours
DISK_LIMIT_GB = 10
REFRESH_MS = 5000

# ============================================================
#  Theme
# ============================================================
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

COLOR_BG = "#060914"
COLOR_SIDEBAR = "#0b101d"
COLOR_CARD = "#111829"
COLOR_CARD_HOVER = "#1a2440"
COLOR_CHIP = "#18213a"
COLOR_BORDER = "#222f4b"
COLOR_TEXT = "#e6ecf5"
COLOR_TEXT_DIM = "#7d8fb0"
COLOR_ACCENT = "#4f8cff"
COLOR_SUCCESS = "#22c55e"
COLOR_DANGER = "#f43f5e"
COLOR_WARNING = "#f59e0b"
COLOR_INFO = "#22d3ee"

FONT_FAMILY = "Tahoma"


def F(size=13, bold=False):
    return (FONT_FAMILY, size, "bold") if bold else (FONT_FAMILY, size)


def tint(fg, bg=COLOR_CARD, t=0.18):
    """Blend fg over bg (customtkinter has no alpha)."""
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(b[i] + (f[i] - b[i]) * t) for i in range(3))


LEVELS = {"info": ("اطلاع", COLOR_INFO), "warning": ("هشدار", COLOR_WARNING), "critical": ("بحرانی", COLOR_DANGER)}
LEVEL_BY_LABEL = {v[0]: k for k, v in LEVELS.items()}
STATUSES = {"pending": ("در انتظار", COLOR_WARNING), "approved": ("تایید شد", COLOR_SUCCESS), "rejected": ("رد شد", COLOR_DANGER)}
REQ_FILTERS = {"در انتظار": "pending", "تایید شده": "approved", "رد شده": "rejected", "همه": None}

PRINTER_CMD = "Get-Printer | Select-Object Name, DriverName, PortName | Format-Table -AutoSize | Out-String -Width 200"
QUICK_COMMANDS = {
    "🖨 لیست پرینترها": PRINTER_CMD,
    "📈 پردازش‌های پرمصرف": "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name, CPU, WS | Format-Table -AutoSize | Out-String -Width 200",
    "🌐 تنظیمات شبکه": "Get-NetIPConfiguration | Out-String -Width 200",
    "💾 فضای دیسک‌ها": "Get-PSDrive -PSProvider FileSystem | Format-Table Name, Used, Free -AutoSize | Out-String",
    "⏱ زمان آخرین بوت": "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime",
    "🧹 ریست Print Spooler": "Restart-Service Spooler -Force; 'Spooler restarted'",
    "🔄 Flush DNS": "ipconfig /flushdns",
}

# ============================================================
#  Database layer
# ============================================================
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(UPDATES_DIR, exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS alarms (id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT, level TEXT,
            target_dept TEXT, scheduled_at TEXT, created_at TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS alarm_acks (alarm_id INTEGER, user_id TEXT, ack_time TEXT);
        CREATE TABLE IF NOT EXISTS lockdown (id INTEGER PRIMARY KEY, is_active INTEGER, message TEXT);
        INSERT OR IGNORE INTO lockdown (id, is_active) VALUES (1, 0);
        CREATE TABLE IF NOT EXISTS remote_commands (id INTEGER PRIMARY KEY AUTOINCREMENT, client_ip TEXT,
            client_hostname TEXT, command TEXT, status TEXT DEFAULT 'pending', output TEXT, created_at TEXT, executed_at TEXT);
        CREATE TABLE IF NOT EXISTS purchase_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, requester TEXT,
            cartridge_model TEXT, quantity INTEGER, status TEXT, req_date TEXT, notes TEXT, urgency TEXT, tracking_code TEXT);
        CREATE TABLE IF NOT EXISTS inventory (cartridge_model TEXT, quantity INTEGER);
        CREATE INDEX IF NOT EXISTS idx_cmd_ip_status ON remote_commands(client_ip, status);
        CREATE INDEX IF NOT EXISTS idx_ack ON alarm_acks(alarm_id, user_id);
        CREATE INDEX IF NOT EXISTS idx_req_status ON purchase_requests(status);
        CREATE INDEX IF NOT EXISTS idx_req_code ON purchase_requests(tracking_code);
        """)


def now_shamsi(fmt="%Y/%m/%d %H:%M"):
    return jdatetime.datetime.now().strftime(fmt)


def to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------- clients (in-memory) ----------
active_clients = {}
clients_lock = threading.Lock()


def clients_snapshot():
    now = time.time()
    with clients_lock:
        for ip in [ip for ip, c in active_clients.items() if now - c["timestamp"] > CLIENT_TTL]:
            del active_clients[ip]
        return [dict(c) for c in active_clients.values()]


def parse_uptime_hours(s):
    m = re.search(r"\d+(?:\.\d+)?", s or "")
    return float(m.group()) if m else None


def parse_disks(s):
    return [(d.upper(), float(g)) for d, g in re.findall(r"([A-Za-z]):\s*(\d+(?:\.\d+)?)\s*GB", s or "")]


def client_issues(c):
    """List of (text, color) problems for one client."""
    issues = []
    for letter, free in parse_disks(c.get("hw_disks")):
        if letter == "C" and free < DISK_LIMIT_GB:
            issues.append((f"فضای C فقط {free:g} GB", COLOR_DANGER))
    h = parse_uptime_hours(c.get("hw_uptime"))
    if h is not None and h > UPTIME_LIMIT_H:
        issues.append((f"{int(h // 24)} روز بدون ری‌استارت", COLOR_WARNING))
    return issues


# ---------- domain operations ----------
def add_alarm(message, level="critical", dept="all", scheduled_at=None):
    with db() as c:
        c.execute("INSERT INTO alarms (message, level, target_dept, scheduled_at, created_at, status) VALUES (?,?,?,?,?, 'active')",
                  (message, level, dept, scheduled_at, now_shamsi()))


def alarm_history(limit=200):
    with db() as c:
        alarms = [dict(r) for r in c.execute("SELECT * FROM alarms ORDER BY id DESC LIMIT ?", (limit,))]
        acks = {}
        for r in c.execute("SELECT alarm_id, user_id FROM alarm_acks"):
            acks.setdefault(r["alarm_id"], []).append(r["user_id"])
    for a in alarms:
        users = acks.get(a["id"], [])
        a["ack_users"], a["ack_count"] = users, len(users)
    return alarms


def get_lockdown():
    with db() as c:
        row = c.execute("SELECT * FROM lockdown WHERE id = 1").fetchone()
    return dict(row) if row else {"is_active": 0, "message": ""}


def set_lockdown(active, message="سیستم قفل است"):
    with db() as c:
        c.execute("UPDATE lockdown SET is_active=?, message=? WHERE id=1", (1 if active else 0, message))


def queue_command(ip, hostname, command):
    with db() as c:
        cur = c.execute("INSERT INTO remote_commands (client_ip, client_hostname, command, status, created_at) VALUES (?,?,?, 'pending', ?)",
                        (ip, hostname, command, now_shamsi()))
        return cur.lastrowid


def get_command(cmd_id):
    with db() as c:
        row = c.execute("SELECT * FROM remote_commands WHERE id=?", (cmd_id,)).fetchone()
    return dict(row) if row else None


def set_request_status(req_id, status):
    """Only pending requests can change; approving decrements inventory once."""
    with db() as c:
        req = c.execute("SELECT * FROM purchase_requests WHERE id=? AND status='pending'", (req_id,)).fetchone()
        if not req:
            return False
        c.execute("UPDATE purchase_requests SET status=? WHERE id=?", (status, req_id))
        if status == "approved":
            c.execute("UPDATE inventory SET quantity=MAX(0, quantity-?) WHERE cartridge_model=?",
                      (to_int(req["quantity"]), req["cartridge_model"]))
    return True


def dashboard_data():
    clients = clients_snapshot()
    with db() as c:
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM purchase_requests GROUP BY status")}
        alarms = c.execute("SELECT COUNT(*) FROM alarms WHERE status='active'").fetchone()[0]
        recent = [dict(r) for r in c.execute("SELECT * FROM purchase_requests ORDER BY id DESC LIMIT 6")]
    attention = [(c.get("hostname", "Unknown"), t, col) for c in clients for t, col in client_issues(c)]
    pending = counts.get("pending", 0)
    if not clients:
        insight = "هیچ کلاینتی آنلاین نیست؛ اتصال شبکه و سرویس کلاینت‌ها را بررسی کنید."
    elif attention:
        insight = f"{len(attention)} مورد نیازمند توجه روی کلاینت‌ها وجود دارد."
    elif pending:
        insight = f"{pending} درخواست تایید‌نشده منتظر بررسی شماست."
    elif alarms:
        insight = f"در حال حاضر {alarms} هشدار فعال در سیستم وجود دارد."
    else:
        insight = "زیرساخت پایدار است و اقدام فوری لازم نیست."
    return dict(clients=len(clients), pending=pending, approved=counts.get("approved", 0), alarms=alarms,
                locked=bool(get_lockdown()["is_active"]), recent=recent, attention=attention, insight=insight)


# ============================================================
#  Telegram + background monitors
# ============================================================
def tg_send(text):
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        return

    def worker():
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=8)
        except requests.RequestException:
            pass
    threading.Thread(target=worker, daemon=True).start()


def alerts_loop():
    alerted = set()
    while True:
        time.sleep(600)
        for c in clients_snapshot():
            ip, host = c.get("ip"), html.escape(str(c.get("hostname", "Unknown")))
            h = parse_uptime_hours(c.get("hw_uptime"))
            key = f"uptime_{ip}"
            if h is not None and h > UPTIME_LIMIT_H:
                if key not in alerted:
                    alerted.add(key)
                    tg_send(f"⚠️ <b>هشدار آپتایم بالا</b>\n\n💻 {host} ({ip})\n⏱ {h:g} ساعت روشن بوده.\nلطفاً ری‌استارت کنید.")
            else:
                alerted.discard(key)
            key = f"disk_{ip}"
            low = [f for l, f in parse_disks(c.get("hw_disks")) if l == "C" and f < DISK_LIMIT_GB]
            if low:
                if key not in alerted:
                    alerted.add(key)
                    tg_send(f"🚨 <b>هشدار فضای دیسک</b>\n\n💻 {host} ({ip})\n💾 فضای C فقط {low[0]:g} GB مانده!")
            else:
                alerted.discard(key)


def generate_and_send_health_report():
    d = dashboard_data()
    low_disk, high_up = [], []
    for c in clients_snapshot():
        host = html.escape(str(c.get("hostname", "Unknown")))
        for l, f in parse_disks(c.get("hw_disks")):
            if l == "C" and f < DISK_LIMIT_GB:
                low_disk.append(f"{host} ({f:g}GB آزاد)")
        h = parse_uptime_hours(c.get("hw_uptime"))
        if h is not None and h > UPTIME_LIMIT_H:
            high_up.append(f"{host} ({int(h // 24)} روز)")
    msg = f"📊 <b>گزارش سلامت روزانه سیستم</b>\n📅 {now_shamsi('%Y/%m/%d')}\n────────────────────\n\n"
    msg += f"💻 کلاینت آنلاین: <b>{d['clients']}</b>\n📥 درخواست در انتظار: <b>{d['pending']}</b>\n🚨 هشدار فعال: <b>{d['alarms']}</b>\n\n"
    if low_disk:
        msg += f"⚠️ <b>فضای دیسک کم ({len(low_disk)}):</b>\n" + "".join(f"   ▪️ {x}\n" for x in low_disk) + "\n"
    if high_up:
        msg += f"⏱ <b>آپتایم بالا ({len(high_up)}):</b>\n" + "".join(f"   ▪️ {x}\n" for x in high_up) + "\n"
    if not low_disk and not high_up:
        msg += "✅ <b>همه منابع نرمال هستند.</b>\n"
    tg_send(msg)


def daily_report_loop():
    last = ""
    while True:
        try:
            now = jdatetime.datetime.now()
            today = now.strftime("%Y/%m/%d")
            if now.hour == 8 and last != today:
                generate_and_send_health_report()
                last = today
        except Exception:
            pass
        time.sleep(60)


# ============================================================
#  Flask API
# ============================================================
app_flask = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


def require_admin(fn):
    """Admin routes: X-API-Key when ADMIN_API_KEY is set, otherwise localhost only."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if ADMIN_API_KEY:
            if not hmac.compare_digest(request.headers.get("X-API-Key", ""), ADMIN_API_KEY):
                return jsonify({"error": "unauthorized"}), 401
        elif request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*a, **kw)
    return wrapper


def body():
    return request.get_json(silent=True) or {}


@app_flask.route("/status")
def status():
    with db() as c:
        row = c.execute("SELECT status FROM purchase_requests WHERE tracking_code=?", (request.args.get("code"),)).fetchone()
    return jsonify({"status": row["status"] if row else "not_found"})


@app_flask.route("/request", methods=["POST"])
@app_flask.route("/submit", methods=["POST"])
def receive_request():
    d = body()
    if not d.get("name") or not d.get("model"):
        return jsonify({"status": "error", "msg": "name/model required"}), 400
    try:
        with db() as c:
            c.execute("INSERT INTO purchase_requests (requester, cartridge_model, quantity, status, req_date, notes, urgency, tracking_code) "
                      "VALUES (?,?,?, 'pending', ?, ?, ?, ?)",
                      (d.get("name"), d.get("model"), to_int(d.get("qty"), 1), now_shamsi(), "دریافت از کلاینت", d.get("urgency"), d.get("tracking_code")))
        tg_send(f"🔔 <b>درخواست جدید!</b>\n👤 {html.escape(str(d.get('name')))}\n🖨 {html.escape(str(d.get('model')))} ({to_int(d.get('qty'), 1)} عدد)")
        return jsonify({"status": "success"}), 200
    except sqlite3.Error as e:
        return jsonify({"status": "error", "msg": str(e)}), 500


@app_flask.route("/heartbeat", methods=["POST"])
def heartbeat():
    d = body()
    ip = d.get("ip") or request.remote_addr
    d["ip"], d["timestamp"] = ip, time.time()
    with clients_lock:
        active_clients[ip] = d
    return jsonify({"status": "ok"})


@app_flask.route("/get_clients")
@require_admin
def api_get_clients():
    return jsonify(clients_snapshot())


@app_flask.route("/send_alarm", methods=["POST"])
@require_admin
def api_send_alarm():
    d = body()
    if not d.get("message"):
        return jsonify({"status": "error"}), 400
    add_alarm(d["message"], d.get("level", "critical"), d.get("target_dept", "all"), d.get("scheduled_at"))
    return jsonify({"status": "success"})


@app_flask.route("/get_alarms")
def get_alarms():
    dept, user = request.args.get("dept", "all"), request.args.get("user_name", "Unknown")
    with db() as c:
        rows = c.execute("SELECT * FROM alarms WHERE status='active' AND (scheduled_at IS NULL OR scheduled_at <= ?) "
                         "AND (target_dept='all' OR target_dept=? OR target_dept=?) "
                         "AND id NOT IN (SELECT alarm_id FROM alarm_acks WHERE user_id=?) ORDER BY id DESC",
                         (now_shamsi(), dept, user, user)).fetchall()
    return jsonify([dict(r) for r in rows])


@app_flask.route("/mark_alarm_read/<int:alarm_id>", methods=["POST"])
def mark_alarm_read(alarm_id):
    user = body().get("user_name", "Unknown")
    with db() as c:
        if not c.execute("SELECT 1 FROM alarm_acks WHERE alarm_id=? AND user_id=?", (alarm_id, user)).fetchone():
            c.execute("INSERT INTO alarm_acks (alarm_id, user_id, ack_time) VALUES (?,?,?)", (alarm_id, user, now_shamsi()))
    return jsonify({"status": "success"})


@app_flask.route("/get_alarm_history")
@require_admin
def api_alarm_history():
    return jsonify(alarm_history())


@app_flask.route("/toggle_lockdown", methods=["POST"])
@require_admin
def api_toggle_lockdown():
    d = body()
    set_lockdown(d.get("active", 0), d.get("message", "سیستم قفل است"))
    return jsonify({"status": "success"})


@app_flask.route("/get_lockdown")
def api_get_lockdown():
    return jsonify(get_lockdown())


@app_flask.route("/updates/<path:filename>")
def serve_update(filename):
    return send_from_directory(UPDATES_DIR, filename)


@app_flask.route("/execute_command", methods=["POST"])
@require_admin
def api_execute_command():
    d = body()
    if not d.get("ip") or not d.get("command"):
        return jsonify({"status": "error"}), 400
    return jsonify({"status": "success", "id": queue_command(d["ip"], d.get("hostname", ""), d["command"])})


@app_flask.route("/get_pending_commands")
def get_pending_commands():
    ip = request.args.get("ip")
    if not ip:
        return jsonify([])
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        rows = [dict(r) for r in c.execute("SELECT * FROM remote_commands WHERE client_ip=? AND status='pending' ORDER BY id", (ip,))]
        if rows:
            c.executemany("UPDATE remote_commands SET status='running' WHERE id=?", [(r["id"],) for r in rows])
    return jsonify(rows)


@app_flask.route("/submit_command_result", methods=["POST"])
def submit_command_result():
    d = body()
    with db() as c:
        c.execute("UPDATE remote_commands SET output=?, status='completed', executed_at=? WHERE id=?",
                  (d.get("output"), now_shamsi(), d.get("cmd_id")))
    return jsonify({"status": "success"})


@app_flask.route("/get_command_result/<int:cmd_id>")
@require_admin
def api_command_result(cmd_id):
    return jsonify(get_command(cmd_id) or {"status": "not_found"})


@app_flask.route("/get_command_history")
@require_admin
def get_command_history():
    ip = request.args.get("ip")
    with db() as c:
        if ip:
            rows = c.execute("SELECT * FROM remote_commands WHERE client_ip=? ORDER BY id DESC LIMIT 50", (ip,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM remote_commands ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([dict(r) for r in rows])


def run_server():
    try:
        app_flask.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
    except OSError as e:
        print(f"[!] Flask could not start on port {PORT}: {e}")


# ============================================================
#  UI helpers
# ============================================================
def card(parent, **kw):
    kw.setdefault("fg_color", COLOR_CARD)
    return ctk.CTkFrame(parent, corner_radius=16, border_width=1, border_color=COLOR_BORDER, **kw)


def pill(parent, text, color, bg=COLOR_CARD):
    return ctk.CTkLabel(parent, text=f"  {text}  ", font=F(11, True), text_color=color,
                        fg_color=tint(color, bg, 0.18), corner_radius=8, height=24)


def chip(parent, text, color=COLOR_TEXT_DIM):
    return ctk.CTkLabel(parent, text=f"  {text}  ", font=F(11), text_color=color,
                        fg_color=COLOR_CHIP, corner_radius=8, height=26)


def empty_state(parent, icon, text):
    box = ctk.CTkFrame(parent, fg_color="transparent")
    box.pack(pady=70)
    ctk.CTkLabel(box, text=icon, font=("Segoe UI Emoji", 44)).pack()
    ctk.CTkLabel(box, text=text, font=F(15, True), text_color=COLOR_TEXT_DIM).pack(pady=8)


def clear(widget):
    for w in widget.winfo_children():
        w.destroy()


# ============================================================
#  Main app
# ============================================================
class AdminApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        global FONT_FAMILY
        fams = set(tkfont.families())
        FONT_FAMILY = next((f for f in ("Vazirmatn", "Vazir", "Tahoma", "Segoe UI") if f in fams), "Tahoma")

        self.title("Luminiz Enterprise NOC")
        self.geometry("1400x900")
        self.minsize(1200, 780)
        self.configure(fg_color=COLOR_BG)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.ui_q = queue.Queue()
        self.current_view = None
        self.req_filter_label = "در انتظار"
        self.clients_cache, self._client_sig = [], None
        self._toast = None

        self._build_sidebar()
        self._build_main()

        self.views = {
            "dashboard": ("داشبورد مدیریت", "پایش زنده وضعیت زیرساخت", self.build_dashboard),
            "requests": ("مدیریت درخواست‌ها", "تایید و رد درخواست‌های کارتریج", self.build_requests),
            "clients": ("مانیتورینگ کلاینت‌ها", "کنترل و فرمان‌دهی به سیستم‌های آنلاین", self.build_clients),
            "alarms": ("تاریخچه هشدارها", "گزارش هشدارهای ارسال‌شده و تاییدها", self.build_alarms),
        }
        self.show_view("dashboard")
        self.after(80, self._pump)
        self.after(REFRESH_MS, self._tick)
        self._update_clock()
        if not BOT_TOKEN or not ADMIN_CHAT_ID:
            self.after(800, lambda: self.toast("توکن تلگرام تنظیم نشده؛ اعلان‌ها ارسال نمی‌شوند", "warning"))

    # ---------- infrastructure ----------
    def run_bg(self, work, done=None):
        """Run work() in a thread; done(result) runs on the UI thread."""
        def target():
            try:
                res, err = work(), None
            except Exception as e:  # noqa
                res, err = None, e
            self.ui_q.put((done, res, err))
        threading.Thread(target=target, daemon=True).start()

    def _pump(self):
        try:
            while True:
                done, res, err = self.ui_q.get_nowait()
                if err is not None:
                    self.toast(f"خطا: {err}", "danger")
                elif done:
                    try:
                        done(res)
                    except Exception as e:  # widget destroyed etc.
                        print("[ui]", e)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _tick(self):
        self._update_footer()
        if self.current_view == "dashboard":
            self._load_dashboard()
        elif self.current_view == "clients":
            self._load_clients()
        self.after(REFRESH_MS, self._tick)

    def _update_clock(self):
        self.time_lbl.configure(text=now_shamsi("%Y/%m/%d  •  %H:%M:%S"))
        self.after(1000, self._update_clock)

    def toast(self, msg, kind="info"):
        color = {"success": COLOR_SUCCESS, "danger": COLOR_DANGER, "warning": COLOR_WARNING}.get(kind, COLOR_ACCENT)
        if self._toast is not None:
            try:
                self._toast.destroy()
            except Exception:
                pass
        t = ctk.CTkFrame(self, fg_color=tint(color, COLOR_CARD, 0.22), border_width=1, border_color=color, corner_radius=12)
        ctk.CTkLabel(t, text=msg, font=F(12, True), text_color=COLOR_TEXT).pack(padx=20, pady=11)
        t.place(relx=0.02, rely=0.97, anchor="sw")
        t.lift()
        self._toast = t
        self.after(3500, lambda: t.destroy() if t.winfo_exists() else None)

    # ---------- layout ----------
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=260, fg_color=COLOR_SIDEBAR, corner_radius=0)
        sb.grid(row=0, column=1, sticky="nsew")
        sb.grid_propagate(False)
        self.sidebar = sb

        logo = ctk.CTkFrame(sb, fg_color="transparent")
        logo.pack(pady=(32, 26), padx=22, fill="x")
        badge = ctk.CTkFrame(logo, width=50, height=50, corner_radius=14, fg_color=tint(COLOR_ACCENT, COLOR_SIDEBAR, 0.22))
        badge.pack(side="right")
        badge.pack_propagate(False)
        ctk.CTkLabel(badge, text="⚡", font=("Segoe UI Emoji", 24), text_color=COLOR_ACCENT).pack(expand=True)
        txt = ctk.CTkFrame(logo, fg_color="transparent")
        txt.pack(side="right", padx=12)
        ctk.CTkLabel(txt, text="Luminiz", font=("Segoe UI", 21, "bold"), text_color=COLOR_TEXT).pack(anchor="e")
        ctk.CTkLabel(txt, text="Enterprise NOC", font=("Segoe UI", 10), text_color=COLOR_TEXT_DIM).pack(anchor="e")

        self.nav = {}
        for key, icon, label in (("dashboard", "📊", "داشبورد"), ("requests", "📥", "درخواست‌ها"),
                                 ("clients", "💻", "کلاینت‌ها"), ("alarms", "📜", "هشدارها")):
            self.nav[key] = self._nav(icon, label, lambda k=key: self.show_view(k))

        ctk.CTkLabel(sb, text="ابزارهای مدیریتی", font=F(10), text_color=COLOR_TEXT_DIM).pack(anchor="e", padx=26, pady=(26, 6))
        self._nav("🚨", "ارسال هشدار", self.show_alarm_dialog, COLOR_DANGER)
        self._nav("🔒", "قفل سیستم", self.show_lockdown_dialog, COLOR_WARNING)
        self._nav("📨", "ارسال گزارش سلامت", self.send_health_report_now, COLOR_INFO)

        foot = ctk.CTkFrame(sb, fg_color=COLOR_CARD, corner_radius=14, border_width=1, border_color=COLOR_BORDER)
        foot.pack(side="bottom", fill="x", padx=18, pady=22)
        self.foot_dot = ctk.CTkLabel(foot, text="●", font=F(14), text_color=COLOR_SUCCESS)
        self.foot_dot.pack(side="right", padx=(14, 4), pady=12)
        self.foot_lbl = ctk.CTkLabel(foot, text="", font=F(11), text_color=COLOR_TEXT)
        self.foot_lbl.pack(side="right")
        self._update_footer()

    def _nav(self, icon, text, cmd, color=None):
        btn = ctk.CTkButton(self.sidebar, text=f"{text}   {icon}", font=F(14, True), fg_color="transparent",
                            text_color=color or COLOR_TEXT_DIM, hover_color=COLOR_CARD, anchor="e",
                            height=44, corner_radius=12, command=cmd)
        btn.pack(fill="x", padx=14, pady=2)
        return btn

    def _update_footer(self):
        n = len(clients_snapshot())
        self.foot_lbl.configure(text=f"سرور فعال :{PORT}  •  {n} کلاینت")

    def _build_main(self):
        mc = ctk.CTkFrame(self, fg_color="transparent")
        mc.grid(row=0, column=0, sticky="nsew")
        mc.grid_rowconfigure(1, weight=1)
        mc.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(mc, fg_color="transparent", height=84)
        header.grid(row=0, column=0, sticky="ew", padx=36, pady=(24, 12))
        header.grid_propagate(False)
        ht = ctk.CTkFrame(header, fg_color="transparent")
        ht.pack(side="right", fill="y")
        self.page_title = ctk.CTkLabel(ht, text="", font=F(26, True), text_color=COLOR_TEXT)
        self.page_title.pack(anchor="e", pady=(10, 0))
        self.page_subtitle = ctk.CTkLabel(ht, text="", font=F(12), text_color=COLOR_TEXT_DIM)
        self.page_subtitle.pack(anchor="e")

        ctk.CTkButton(header, text="⟳  بروزرسانی", font=F(13, True), width=130, height=42, corner_radius=12,
                      fg_color=COLOR_CARD, hover_color=COLOR_CARD_HOVER, border_width=1, border_color=COLOR_BORDER,
                      command=lambda: self.show_view(self.current_view)).pack(side="left", pady=14)
        self.time_lbl = ctk.CTkLabel(header, text="", font=F(13, True), text_color=COLOR_ACCENT,
                                     fg_color=COLOR_CARD, corner_radius=12, height=42)
        self.time_lbl.pack(side="left", padx=12, pady=14, ipadx=14)

        self.content = ctk.CTkFrame(mc, fg_color="transparent")
        self.content.grid(row=1, column=0, sticky="nsew", padx=36, pady=(0, 24))

    def show_view(self, view):
        self.current_view = view
        for k, b in self.nav.items():
            on = k == view
            b.configure(fg_color=tint(COLOR_ACCENT, COLOR_SIDEBAR, 0.16) if on else "transparent",
                        text_color=COLOR_ACCENT if on else COLOR_TEXT_DIM)
        title, sub, builder = self.views[view]
        self.page_title.configure(text=title)
        self.page_subtitle.configure(text=sub)
        clear(self.content)
        builder()

    def _refresh_if(self, view):
        if self.current_view == view:
            self.show_view(view)

    # ============================================================
    #  Dashboard
    # ============================================================
    def build_dashboard(self):
        ins = ctk.CTkFrame(self.content, fg_color=tint(COLOR_ACCENT, COLOR_BG, 0.10), corner_radius=16,
                           border_width=1, border_color=tint(COLOR_ACCENT, COLOR_BG, 0.45))
        ins.pack(fill="x", pady=(0, 16))
        ctk.CTkLabel(ins, text="🧠  بینش هوشمند سیستم", font=F(14, True), text_color=COLOR_ACCENT).pack(anchor="e", padx=22, pady=(14, 2))
        self.insight_lbl = ctk.CTkLabel(ins, text="در حال بارگذاری…", font=F(13), text_color=COLOR_TEXT, justify="right", wraplength=900)
        self.insight_lbl.pack(anchor="e", padx=22, pady=(0, 14))

        row = ctk.CTkFrame(self.content, fg_color="transparent")
        row.pack(fill="x", pady=(0, 16))
        self.kpi = {}
        for key, title, icon, color in (("clients", "کلاینت‌های آنلاین", "💻", COLOR_ACCENT), ("pending", "در انتظار تایید", "⏳", COLOR_WARNING),
                                        ("approved", "تایید‌شده", "✅", COLOR_SUCCESS), ("alarms", "هشدارهای فعال", "🚨", COLOR_DANGER),
                                        ("locked", "وضعیت قفل", "🔒", COLOR_SUCCESS)):
            self.kpi[key] = self._kpi(row, title, icon, color)

        cols = ctk.CTkFrame(self.content, fg_color="transparent")
        cols.pack(fill="both", expand=True)
        cols.grid_columnconfigure((0, 1), weight=1, uniform="c")
        cols.grid_rowconfigure(0, weight=1)
        right, left = card(cols), card(cols)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.recent_box = self._panel(right, "📋  آخرین درخواست‌های کارتریج")
        self.attn_box = self._panel(left, "⚠️  نیازمند توجه")
        self._load_dashboard()

    def _panel(self, parent, title):
        ctk.CTkLabel(parent, text=title, font=F(15, True), text_color=COLOR_TEXT).pack(anchor="e", padx=22, pady=(18, 10))
        ctk.CTkFrame(parent, height=1, fg_color=COLOR_BORDER).pack(fill="x", padx=22)
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(fill="both", expand=True, padx=22, pady=8)
        return box

    def _kpi(self, parent, title, icon, color):
        c = card(parent)
        c.pack(side="right", fill="both", expand=True, padx=6)
        ctk.CTkFrame(c, height=4, fg_color=color, corner_radius=4).pack(fill="x", padx=28, pady=(0, 0))
        head = ctk.CTkFrame(c, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(14, 0))
        ctk.CTkLabel(head, text=icon, font=("Segoe UI Emoji", 20)).pack(side="right")
        ctk.CTkLabel(head, text=title, font=F(12), text_color=COLOR_TEXT_DIM).pack(side="right", padx=8)
        val = ctk.CTkLabel(c, text="–", font=F(30, True), text_color=color)
        val.pack(anchor="e", padx=22, pady=(4, 16))
        return val

    def _load_dashboard(self):
        self.run_bg(dashboard_data, self._apply_dashboard)

    def _apply_dashboard(self, d):
        if self.current_view != "dashboard" or not self.insight_lbl.winfo_exists():
            return
        self.insight_lbl.configure(text=d["insight"])
        for k in ("clients", "pending", "approved", "alarms"):
            self.kpi[k].configure(text=str(d[k]))
        self.kpi["locked"].configure(text="قفل" if d["locked"] else "آزاد", text_color=COLOR_DANGER if d["locked"] else COLOR_SUCCESS)

        clear(self.recent_box)
        if not d["recent"]:
            ctk.CTkLabel(self.recent_box, text="درخواستی ثبت نشده", font=F(12), text_color=COLOR_TEXT_DIM).pack(pady=24)
        for r in d["recent"]:
            row = ctk.CTkFrame(self.recent_box, fg_color="transparent")
            row.pack(fill="x", pady=5)
            ctk.CTkLabel(row, text=f"👤 {r['requester']}", font=F(13, True), text_color=COLOR_TEXT).pack(side="right")
            ctk.CTkLabel(row, text=f"🖨 {r['cartridge_model']} × {r['quantity']}", font=F(11), text_color=COLOR_ACCENT).pack(side="right", padx=14)
            txt, col = STATUSES.get(r["status"], ("نامشخص", COLOR_TEXT_DIM))
            pill(row, txt, col).pack(side="left")

        clear(self.attn_box)
        if not d["attention"]:
            ctk.CTkLabel(self.attn_box, text="✅  همه کلاینت‌ها سالم هستند", font=F(13, True), text_color=COLOR_SUCCESS).pack(pady=28)
        for host, text, col in d["attention"][:8]:
            row = ctk.CTkFrame(self.attn_box, fg_color="transparent")
            row.pack(fill="x", pady=5)
            ctk.CTkLabel(row, text=f"💻 {host}", font=F(13, True), text_color=COLOR_TEXT).pack(side="right")
            pill(row, text, col).pack(side="left")

    # ============================================================
    #  Requests
    # ============================================================
    def build_requests(self):
        bar = ctk.CTkFrame(self.content, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 12))
        seg = ctk.CTkSegmentedButton(bar, values=list(REQ_FILTERS), font=F(12, True), height=38, selected_color=COLOR_ACCENT,
                                     unselected_color=COLOR_CARD, fg_color=COLOR_CARD, command=self._on_req_filter)
        seg.set(self.req_filter_label)
        seg.pack(side="right")
        self.req_count = ctk.CTkLabel(bar, text="", font=F(12), text_color=COLOR_TEXT_DIM)
        self.req_count.pack(side="left")
        self.req_list = ctk.CTkScrollableFrame(self.content, fg_color="transparent")
        self.req_list.pack(fill="both", expand=True)
        self._load_requests()

    def _on_req_filter(self, label):
        self.req_filter_label = label
        self._load_requests()

    def _load_requests(self):
        status = REQ_FILTERS[self.req_filter_label]

        def work():
            with db() as c:
                if status:
                    rows = c.execute("SELECT * FROM purchase_requests WHERE status=? ORDER BY id DESC LIMIT 200", (status,))
                else:
                    rows = c.execute("SELECT * FROM purchase_requests ORDER BY id DESC LIMIT 200")
                return [dict(r) for r in rows]
        self.run_bg(work, self._render_requests)

    def _render_requests(self, rows):
        if self.current_view != "requests" or not self.req_list.winfo_exists():
            return
        clear(self.req_list)
        self.req_count.configure(text=f"{len(rows)} مورد")
        if not rows:
            empty_state(self.req_list, "📭", "درخواستی در این دسته وجود ندارد")
            return
        for r in rows:
            txt, col = STATUSES.get(r["status"], ("نامشخص", COLOR_TEXT_DIM))
            cd = card(self.req_list)
            cd.pack(fill="x", pady=5, padx=2)
            ctk.CTkFrame(cd, width=5, fg_color=col, corner_radius=4).pack(side="right", fill="y", padx=(0, 0), pady=10)
            if r["status"] == "pending":
                act = ctk.CTkFrame(cd, fg_color="transparent")
                act.pack(side="left", padx=18)
                ctk.CTkButton(act, text="✓  تایید", width=96, height=36, corner_radius=10, font=F(12, True), fg_color=COLOR_SUCCESS,
                              hover_color=tint(COLOR_SUCCESS, "#000000", 0.75), command=lambda i=r["id"]: self._decide(i, "approved")).pack(side="left", padx=4)
                ctk.CTkButton(act, text="✕  رد", width=96, height=36, corner_radius=10, font=F(12, True), fg_color=tint(COLOR_DANGER, COLOR_CARD, 0.25),
                              text_color=COLOR_DANGER, hover_color=tint(COLOR_DANGER, COLOR_CARD, 0.4),
                              command=lambda i=r["id"]: self._decide(i, "rejected")).pack(side="left", padx=4)
            body_ = ctk.CTkFrame(cd, fg_color="transparent")
            body_.pack(side="right", fill="x", expand=True, padx=18, pady=14)
            top = ctk.CTkFrame(body_, fg_color="transparent")
            top.pack(fill="x")
            ctk.CTkLabel(top, text=f"👤 {r['requester']}", font=F(15, True), text_color=COLOR_TEXT).pack(side="right")
            pill(top, txt, col).pack(side="right", padx=10)
            if r.get("urgency"):
                pill(top, f"فوریت: {r['urgency']}", COLOR_WARNING).pack(side="right")
            info = ctk.CTkFrame(body_, fg_color="transparent")
            info.pack(fill="x", pady=(8, 0))
            chip(info, f"🖨 {r['cartridge_model']} × {r['quantity']}", COLOR_ACCENT).pack(side="right")
            chip(info, f"🔖 {r['tracking_code'] or '—'}").pack(side="right", padx=6)
            chip(info, f"📅 {r['req_date'] or '—'}").pack(side="right")

    def _decide(self, req_id, status):
        def done(ok):
            self.toast("وضعیت درخواست بروزرسانی شد" if ok else "این درخواست قبلاً پردازش شده", "success" if ok else "warning")
            self._load_requests()
        self.run_bg(lambda: set_request_status(req_id, status), done)

    # ============================================================
    #  Clients
    # ============================================================
    def build_clients(self):
        bar = ctk.CTkFrame(self.content, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 12))
        self.client_search = ctk.StringVar()
        ctk.CTkEntry(bar, textvariable=self.client_search, placeholder_text="🔍  جستجوی نام، IP یا کاربر…", width=360, height=42,
                     justify="right", font=F(13), fg_color=COLOR_CARD, border_color=COLOR_BORDER, corner_radius=12).pack(side="right")
        self.client_search.trace_add("write", lambda *_: self._render_clients())
        self.client_count = ctk.CTkLabel(bar, text="", font=F(12), text_color=COLOR_TEXT_DIM)
        self.client_count.pack(side="left")
        self.client_list = ctk.CTkScrollableFrame(self.content, fg_color="transparent")
        self.client_list.pack(fill="both", expand=True)
        self._client_sig = None
        self._load_clients()

    def _load_clients(self):
        self.clients_cache = sorted(clients_snapshot(), key=lambda c: str(c.get("hostname", "")).lower())
        sig = tuple((c.get("ip"), c.get("hw_uptime"), c.get("hw_ram"), c.get("hw_disks"), c.get("username")) for c in self.clients_cache)
        if sig != self._client_sig:
            self._client_sig = sig
            self._render_clients()

    def _render_clients(self):
        if self.current_view != "clients" or not self.client_list.winfo_exists():
            return
        q = self.client_search.get().strip().lower()
        items = [c for c in self.clients_cache
                 if not q or q in " ".join(str(c.get(k, "")) for k in ("hostname", "ip", "username")).lower()]
        clear(self.client_list)
        self.client_count.configure(text=f"{len(items)} از {len(self.clients_cache)} کلاینت آنلاین")
        if not items:
            empty_state(self.client_list, "📡", "کلاینتی یافت نشد")
            return
        for c in items:
            self._client_card(c)

    def _client_card(self, c):
        issues = client_issues(c)
        accent = COLOR_DANGER if any(col == COLOR_DANGER for _, col in issues) else COLOR_WARNING if issues else COLOR_SUCCESS
        cd = card(self.client_list)
        cd.pack(fill="x", pady=5, padx=2)
        ctk.CTkFrame(cd, width=5, fg_color=accent, corner_radius=4).pack(side="right", fill="y", pady=10)

        act = ctk.CTkFrame(cd, fg_color="transparent")
        act.pack(side="left", padx=18, pady=12)
        ctk.CTkButton(act, text="⚙️  دستور", width=104, height=34, corner_radius=10, font=F(12, True), fg_color=COLOR_ACCENT,
                      command=lambda cl=c: self.show_command_dialog(cl)).pack(pady=3)
        ctk.CTkButton(act, text="🖨  پرینتر", width=104, height=34, corner_radius=10, font=F(12), fg_color=COLOR_CHIP, hover_color=COLOR_CARD_HOVER,
                      command=lambda cl=c: self.show_command_dialog(cl, PRINTER_CMD, True, f"🖨 پرینترهای {cl.get('hostname', '')}")).pack(pady=3)
        ctk.CTkButton(act, text="🔔  پیام", width=104, height=34, corner_radius=10, font=F(12), fg_color=COLOR_CHIP, hover_color=COLOR_CARD_HOVER,
                      command=lambda cl=c: self.show_personal_message_dialog(cl)).pack(pady=3)

        info = ctk.CTkFrame(cd, fg_color="transparent")
        info.pack(side="right", fill="x", expand=True, padx=18, pady=16)
        head = ctk.CTkFrame(info, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=f"💻 {c.get('hostname', 'Unknown')}", font=F(17, True), text_color=COLOR_TEXT).pack(side="right")
        pill(head, "● آنلاین", COLOR_SUCCESS).pack(side="right", padx=10)
        ctk.CTkLabel(info, text=f"🌐 {c.get('ip', '')}    👤 {c.get('username', '—')}", font=F(12), text_color=COLOR_TEXT_DIM).pack(anchor="e", pady=(4, 8))

        chips = ctk.CTkFrame(info, fg_color="transparent")
        chips.pack(fill="x")
        chip(chips, f"🖥 {str(c.get('hw_cpu', '—'))[:34]}", COLOR_ACCENT).pack(side="right")
        chip(chips, f"🧠 {c.get('hw_ram', '—')}").pack(side="right", padx=6)
        h = parse_uptime_hours(c.get("hw_uptime"))
        chip(chips, f"⏱ {c.get('hw_uptime', '—')}", COLOR_WARNING if h and h > UPTIME_LIMIT_H else COLOR_TEXT_DIM).pack(side="right")
        for letter, free in parse_disks(c.get("hw_disks")):
            low = letter == "C" and free < DISK_LIMIT_GB
            chip(chips, f"💾 {letter}: {free:g}GB", COLOR_DANGER if low else COLOR_TEXT_DIM).pack(side="right", padx=(6, 0))

    # ============================================================
    #  Alarms
    # ============================================================
    def build_alarms(self):
        self.alarm_list = ctk.CTkScrollableFrame(self.content, fg_color="transparent")
        self.alarm_list.pack(fill="both", expand=True)
        self.run_bg(alarm_history, self._render_alarms)

    def _render_alarms(self, alarms):
        if self.current_view != "alarms" or not self.alarm_list.winfo_exists():
            return
        if not alarms:
            empty_state(self.alarm_list, "🔕", "هیچ هشداری ثبت نشده است")
            return
        for a in alarms:
            label, col = LEVELS.get(a.get("level"), ("اطلاع", COLOR_INFO))
            cd = card(self.alarm_list)
            cd.pack(fill="x", pady=5, padx=2)
            ctk.CTkFrame(cd, width=5, fg_color=col, corner_radius=4).pack(side="right", fill="y", pady=10)
            inner = ctk.CTkFrame(cd, fg_color="transparent")
            inner.pack(side="right", fill="x", expand=True, padx=18, pady=14)
            top = ctk.CTkFrame(inner, fg_color="transparent")
            top.pack(fill="x")
            pill(top, label, col).pack(side="right")
            ctk.CTkLabel(top, text=a.get("message", ""), font=F(14, True), text_color=COLOR_TEXT, justify="right", wraplength=760).pack(side="right", padx=12)
            meta = ctk.CTkFrame(inner, fg_color="transparent")
            meta.pack(fill="x", pady=(8, 0))
            chip(meta, f"🏢 {a.get('target_dept')}").pack(side="right")
            chip(meta, f"🕒 {a.get('created_at')}").pack(side="right", padx=6)
            chip(meta, f"✓ {a.get('ack_count')} تایید", COLOR_SUCCESS if a.get("ack_count") else COLOR_TEXT_DIM).pack(side="right")
            if a.get("scheduled_at"):
                chip(meta, f"⏰ {a['scheduled_at']}", COLOR_WARNING).pack(side="right", padx=6)
            if a.get("ack_users"):
                ctk.CTkLabel(inner, text="تاییدکنندگان: " + "، ".join(a["ack_users"][:12]), font=F(11),
                             text_color=COLOR_TEXT_DIM, justify="right", wraplength=800).pack(anchor="e", pady=(6, 0))

    # ============================================================
    #  Dialogs
    # ============================================================
    def _dialog(self, title, w, h):
        d = ctk.CTkToplevel(self)
        d.title(title)
        d.configure(fg_color=COLOR_BG)
        x = self.winfo_x() + max((self.winfo_width() - w) // 2, 0)
        y = self.winfo_y() + max((self.winfo_height() - h) // 2, 0)
        d.geometry(f"{w}x{h}+{x}+{y}")
        d.transient(self)
        d.after(200, lambda: d.winfo_exists() and (d.lift(), d.focus_force()))
        return d

    def _field_label(self, parent, text):
        ctk.CTkLabel(parent, text=text, font=F(12, True), text_color=COLOR_TEXT_DIM).pack(anchor="e", padx=32, pady=(14, 4))

    def send_health_report_now(self):
        self.run_bg(generate_and_send_health_report, lambda _: self.toast("گزارش سلامت ارسال شد", "success"))

    def show_alarm_dialog(self):
        d = self._dialog("ارسال هشدار", 560, 640)
        ctk.CTkLabel(d, text="🚨  ارسال هشدار", font=F(22, True), text_color=COLOR_DANGER).pack(pady=(26, 6))
        self._field_label(d, "سطح هشدار")
        lvl = ctk.CTkSegmentedButton(d, values=[v[0] for v in LEVELS.values()], height=38, font=F(12, True),
                                     selected_color=COLOR_DANGER, unselected_color=COLOR_CARD, fg_color=COLOR_CARD)
        lvl.set("بحرانی")
        lvl.pack(fill="x", padx=32)
        self._field_label(d, "واحد هدف")
        dept = ctk.CTkOptionMenu(d, values=["all", "مدیریت", "مالی", "فنی", "فروش"], height=40, font=F(13), fg_color=COLOR_CARD,
                                 button_color=COLOR_CARD_HOVER, dropdown_font=F(12))
        dept.pack(fill="x", padx=32)
        self._field_label(d, "زمان‌بندی (خالی = فوری) — قالب: 1405/08/25 14:30")
        sch = ctk.CTkEntry(d, height=40, justify="right", font=F(13), fg_color=COLOR_CARD, border_color=COLOR_BORDER)
        sch.pack(fill="x", padx=32)
        self._field_label(d, "متن پیام")
        txt = ctk.CTkTextbox(d, height=130, font=F(13), fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        txt.pack(fill="x", padx=32)

        def send():
            msg = txt.get("1.0", "end").strip()
            when = sch.get().strip() or None
            if not msg:
                return self.toast("متن هشدار خالی است", "warning")
            if when:
                try:
                    jdatetime.datetime.strptime(when, "%Y/%m/%d %H:%M")
                except ValueError:
                    return self.toast("قالب زمان‌بندی نادرست است", "warning")

            def done(_):
                d.destroy()
                self.toast("هشدار ارسال شد", "success")
                self._refresh_if("alarms")
            self.run_bg(lambda: add_alarm(msg, LEVEL_BY_LABEL[lvl.get()], dept.get(), when), done)
        ctk.CTkButton(d, text="ارسال هشدار", font=F(14, True), height=50, corner_radius=12, fg_color=COLOR_DANGER,
                      hover_color=tint(COLOR_DANGER, "#000000", 0.75), command=send).pack(fill="x", padx=32, pady=26)

    def show_lockdown_dialog(self):
        d = self._dialog("قفل سیستم", 460, 400)
        state = get_lockdown()
        active = bool(state["is_active"])
        ctk.CTkLabel(d, text="🔒  قفل سراسری", font=F(22, True), text_color=COLOR_WARNING).pack(pady=(26, 10))
        pill(d, "● قفل فعال" if active else "● سیستم آزاد", COLOR_DANGER if active else COLOR_SUCCESS, COLOR_BG).pack(pady=4)
        self._field_label(d, "پیام نمایش‌داده‌شده روی کلاینت‌ها")
        msg = ctk.CTkEntry(d, height=42, justify="right", font=F(13), fg_color=COLOR_CARD, border_color=COLOR_BORDER)
        msg.insert(0, state.get("message") or "سیستم قفل است")
        msg.pack(fill="x", padx=32)

        def toggle():
            if not active and not messagebox.askyesno("تایید", "همه کلاینت‌ها قفل می‌شوند. ادامه می‌دهید؟", parent=d):
                return

            def done(_):
                d.destroy()
                self.toast("قفل سیستم فعال شد" if not active else "قفل سیستم برداشته شد", "success")
            self.run_bg(lambda: set_lockdown(not active, msg.get().strip() or "سیستم قفل است"), done)
        ctk.CTkButton(d, text="برداشتن قفل" if active else "فعال‌سازی قفل", font=F(14, True), height=50, corner_radius=12,
                      fg_color=COLOR_SUCCESS if active else COLOR_WARNING, text_color="#0b0b0b", command=toggle).pack(fill="x", padx=32, pady=28)

    def show_command_dialog(self, client, preset=None, auto_run=False, title=None):
        host, ip = client.get("hostname", ""), client.get("ip")
        d = self._dialog(title or f"دستور روی {host}", 780, 700)
        ctk.CTkLabel(d, text=title or f"⚙️  PowerShell روی {host}", font=F(18, True), text_color=COLOR_TEXT).pack(pady=(22, 4))
        ctk.CTkLabel(d, text=f"{ip}  •  {client.get('username', '')}", font=F(11), text_color=COLOR_TEXT_DIM).pack()

        txt = ctk.CTkTextbox(d, height=110, font=("Consolas", 12), fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        placeholder = "— دستورهای سریع —"

        def pick(v):
            if v in QUICK_COMMANDS:
                txt.delete("1.0", "end")
                txt.insert("1.0", QUICK_COMMANDS[v])
        ctk.CTkOptionMenu(d, values=[placeholder, *QUICK_COMMANDS], command=pick, height=38, font=F(12), fg_color=COLOR_CARD,
                          button_color=COLOR_CARD_HOVER, dropdown_font=F(12)).pack(fill="x", padx=32, pady=(14, 8))
        txt.pack(fill="x", padx=32)
        txt.insert("1.0", preset or "Get-Process | Select-Object -First 5 Name, CPU")

        status = ctk.CTkLabel(d, text="آماده", font=F(12, True), text_color=COLOR_TEXT_DIM)
        out = ctk.CTkTextbox(d, font=("Consolas", 12), fg_color="#05070f", border_width=1, border_color=COLOR_BORDER)
        btn = ctk.CTkButton(d, text="🚀  اجرا", font=F(14, True), height=46, corner_radius=12, fg_color=COLOR_SUCCESS, text_color="#06120a")

        def run():
            cmd = txt.get("1.0", "end").strip()
            if not cmd:
                return
            if not auto_run and not messagebox.askyesno("تایید اجرا", f"این دستور روی {host} اجرا شود؟", parent=d):
                return
            btn.configure(state="disabled")
            status.configure(text="⏳ در صف اجرا…", text_color=COLOR_WARNING)
            out.delete("1.0", "end")
            self.run_bg(lambda: queue_command(ip, host, cmd), lambda cid: self._poll(cid, out, status, btn, d, 0))
        btn.configure(command=run)
        btn.pack(fill="x", padx=32, pady=12)
        status.pack(anchor="e", padx=32)
        out.pack(fill="both", expand=True, padx=32, pady=(6, 24))
        if auto_run:
            d.after(300, run)

    def _poll(self, cmd_id, out, status, btn, d, tries):
        if not d.winfo_exists():
            return

        def done(row):
            if not d.winfo_exists():
                return
            if row and row["status"] == "completed":
                out.delete("1.0", "end")
                out.insert("1.0", row.get("output") or "(خروجی خالی)")
                status.configure(text="✅ اجرا شد", text_color=COLOR_SUCCESS)
                btn.configure(state="normal")
            elif tries >= 40:
                status.configure(text="⚠️ پاسخی از کلاینت دریافت نشد", text_color=COLOR_DANGER)
                btn.configure(state="normal")
            else:
                if row and row["status"] == "running":
                    status.configure(text="⚙️ در حال اجرا روی کلاینت…", text_color=COLOR_ACCENT)
                d.after(1500, lambda: self._poll(cmd_id, out, status, btn, d, tries + 1))
        self.run_bg(lambda: get_command(cmd_id), done)

    def show_personal_message_dialog(self, client):
        user = client.get("username", "Unknown")
        d = self._dialog("پیام شخصی", 520, 420)
        ctk.CTkLabel(d, text="🔔  پیام شخصی", font=F(20, True), text_color=COLOR_TEXT).pack(pady=(24, 2))
        ctk.CTkLabel(d, text=f"به: {user}  ({client.get('hostname', '')})", font=F(12), text_color=COLOR_TEXT_DIM).pack()
        txt = ctk.CTkTextbox(d, height=170, font=F(13), fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        txt.pack(fill="x", padx=32, pady=16)

        def send():
            m = txt.get("1.0", "end").strip()
            if not m:
                return self.toast("متن پیام خالی است", "warning")

            def done(_):
                d.destroy()
                self.toast("پیام ارسال شد", "success")
            self.run_bg(lambda: add_alarm(m, "info", user), done)
        ctk.CTkButton(d, text="ارسال پیام", font=F(14, True), height=48, corner_radius=12, fg_color=COLOR_ACCENT, command=send).pack(fill="x", padx=32)


if __name__ == "__main__":
    start_update_check()
    init_db()
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=alerts_loop, daemon=True).start()
    threading.Thread(target=daily_report_loop, daemon=True).start()
    AdminApp().mainloop()
