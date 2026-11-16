# bot_runner.py - فقط ربات تلگرام (جدا از Flask)
import os
import sqlite3
import time
import jdatetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8802678520:AAFkzn7sYULh76Sb_1G1I1IjP06LuNNhTjA")
DB_PATH = "/tmp/printers.db"

active_clients = {}

async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 🤖\nربات Luminiz NOC آنلاین است.\n\n"
        "دستورات:\n/start — شروع\n/status — وضعیت\n/clients — کلاینت‌ها\n/lock — قفل\n/requests — درخواست‌ها"
    )

async def tg_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
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
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")

async def tg_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not active_clients:
        await update.message.reply_text("📡 هیچ کلاینتی آنلاین نیست.")
        return
    lines = ["💻 <b>کلاینت‌های آنلاین:</b>"]
    for c in active_clients.values():
        lines.append(f"• <code>{c.get('hostname','?')}</code> — {c.get('ip','?')}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def tg_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT is_active FROM lockdown WHERE id=1").fetchone()
        active = bool(row[0]) if row else False
        await update.message.reply_text(f"🔒 وضعیت قفل: {'فعال' if active else 'غیرفعال'}")
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")

async def tg_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("SELECT requester, cartridge_model, quantity, tracking_code FROM purchase_requests WHERE status='pending' ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            await update.message.reply_text("📭 درخواست در انتظاری نیست.")
            return
        lines = ["📥 <b>آخرین درخواست‌های در انتظار:</b>"]
        for r in rows:
            lines.append(f"• {r[0]} — {r[1]} × {r[2]} (<code>{r[3]}</code>)")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")

def main():
    if not BOT_TOKEN:
        print("[!] BOT_TOKEN تنظیم نشده")
        return
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", tg_start))
    application.add_handler(CommandHandler("status", tg_status))
    application.add_handler(CommandHandler("clients", tg_clients))
    application.add_handler(CommandHandler("lock", tg_lock))
    application.add_handler(CommandHandler("requests", tg_requests))
    print("[✓] ربات تلگرام در حال اجراست...")
    application.run_polling()

if __name__ == "__main__":
    main()
