$serverCode = @'
# server_only.py - نسخه فقط سرور برای Render (بدون tkinter)
import sqlite3
import requests
import jdatetime
import time
import json
from flask import Flask, request, jsonify, send_from_directory
import os

BOT_TOKEN = "8802678520:AAFkzn7sYULh76Sb_1G1I1IjP06LuNNhTjA"
ADMIN_CHAT_ID = "5913106839"
DB_PATH = "/tmp/printers.db"  # روی Render از /tmp استفاده می‌کنیم

app = Flask(__name__)
active_clients = {}

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS purchase_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, requester TEXT, cartridge_model TEXT, quantity INTEGER, status TEXT, req_date TEXT, notes TEXT, urgency TEXT, tracking_code TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS alarms (id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT, level TEXT, target_dept TEXT, scheduled_at TEXT, created_at TEXT, status TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS alarm_acks (alarm_id INTEGER, user_id TEXT, ack_time TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS lockdown (id INTEGER PRIMARY KEY, is_active INTEGER, message TEXT)")
        conn.execute("INSERT OR IGNORE INTO lockdown (id, is_active) VALUES (1, 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS remote_commands (id INTEGER PRIMARY KEY AUTOINCREMENT, client_ip TEXT, client_hostname TEXT, command TEXT, status TEXT DEFAULT 'pending', output TEXT, created_at TEXT, executed_at TEXT)")

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
        now = jdatetime.datetime.now().strftime("%Y/%m/%d %H:%M")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO purchase_requests (requester, cartridge_model, quantity, status, req_date, notes, urgency, tracking_code) VALUES (?, ?, ?, 'pending', ?, 'از کلاینت', ?, ?)",
                       (data.get("name"), data.get("model"), data.get("qty"), now, data.get("urgency"), data.get("tracking_code")))
            conn.commit()
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                         json={"chat_id": ADMIN_CHAT_ID, "text": f"🔔 درخواست جدید: {data.get('name')} - {data.get('model')}", "parse_mode": "HTML"}, timeout=5)
        except: pass
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
    for ip in [ip for ip, c in active_clients.items() if now - c['timestamp'] > 8]:
        del active_clients[ip]
    return jsonify(list(active_clients.values()))

@app.route('/send_alarm', methods=['POST'])
def send_alarm():
    data = request.json
    now = jdatetime.datetime.now().strftime("%Y/%m/%d %H:%M")
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
    now = jdatetime.datetime.now().strftime("%Y/%m/%d %H:%M")
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
    now = jdatetime.datetime.now().strftime("%Y/%m/%d %H:%M")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO remote_commands (client_ip, client_hostname, command, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                    (data.get("ip"), data.get("hostname", ""), data.get("command"), now))
        conn.commit()
    return jsonify({"status": "success"})

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
        conn.execute("UPDATE remote_commands SET output=?, status='completed' WHERE id=?", (data.get("output"), data.get("cmd_id")))
        conn.commit()
    return jsonify({"status": "success"})

@app.route('/client_updates/<path:filename>')
def serve_client_update(filename):
    return send_from_directory("/tmp/client_updates", filename)

@app.route('/updates/<path:filename>')
def serve_update(filename):
    return send_from_directory("/tmp/updates", filename)

if __name__ == "__main__":
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=False)
'@
Set-Content -Path "C:\TelegramForwarder\server_only.py" -Value $serverCode -Encoding UTF8
Write-Host "[✓] server_only.py ساخته شد." -ForegroundColor Green