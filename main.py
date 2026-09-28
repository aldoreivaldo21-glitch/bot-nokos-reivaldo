import logging
import os
import json
import re
import time
import hmac
import hashlib
import sqlite3
import threading
import urllib.request
import urllib.error
import asyncio
from flask import Flask, request, jsonify
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# --- KONFIGURASI ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597")
SMScode_API_KEY = os.environ.get("SMScode_API_KEY")
SMScode_WEBHOOK_SECRET = os.environ.get("SMScode_WEBHOOK_SECRET", "")  # Set di Railway setelah registrasi webhook
LOW_BALANCE_THRESHOLD = 2000
BOT_VERSION = "2.9 Professional (Webhook)"
START_TIME = time.time()
MAX_BULK = 100
AUTO_DELETE_SECONDS = 10
BATCH_SIZE = 10
TIMEOUT_MINUTES = 25
TIMEOUT_SECONDS = TIMEOUT_MINUTES * 60
PORT = int(os.environ.get("PORT", 8080))

BASE_URL = "https://api.smscode.gg/v1"
HEADERS = {
    "Authorization": f"Bearer {SMScode_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")
pending_messages = {}
active_bulk_batches = {}
webhook_notified_orders = set()  # Anti-duplikat antara webhook & polling

# Global refs untuk komunikasi Flask <-> Telegram
telegram_loop = None
telegram_bot_instance = None

# --- FLASK WEBHOOK SERVER ---
flask_app = Flask(__name__)

def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Verifikasi HMAC-SHA256 signature dari SMScode."""
    if not SMScode_WEBHOOK_SECRET:
        # Kalau secret belum di-set, skip verifikasi (untuk testing)
        return True
    expected = "sha256=" + hmac.new(
        SMScode_WEBHOOK_SECRET.encode(),
        raw_body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature_header, expected)

@flask_app.route('/webhook/smscode', methods=['POST'])
def smscode_webhook():
    try:
        raw_body = request.get_data()
        signature = request.headers.get('X-Webhook-Signature', '')

        if not verify_webhook_signature(raw_body, signature):
            print("[Webhook] Invalid signature")
            return jsonify({"error": "invalid signature"}), 401

        data = json.loads(raw_body.decode())
        event = data.get("event", "")
        payload = data.get("data", {})

        print(f"[Webhook] Event: {event}")

        if event == "order.otp_received":
            order_id = str(payload.get("order_id", ""))
            otp_code = payload.get("otp_code")
            otp_message = payload.get("otp_message")
            phone = payload.get("phone_number", "")

            if telegram_loop and telegram_bot_instance:
                asyncio.run_coroutine_threadsafe(
                    handle_webhook_otp(order_id, otp_code, otp_message, phone),
                    telegram_loop
                )
            return jsonify({"success": True}), 200

        elif event in ["order.cancelled", "order.expired", "order.completed"]:
            order_id = str(payload.get("order_id", ""))
            if telegram_loop and telegram_bot_instance:
                asyncio.run_coroutine_threadsafe(
                    handle_webhook_status_change(order_id, event),
                    telegram_loop
                )
            return jsonify({"success": True}), 200

        return jsonify({"success": True, "ignored": event}), 200

    except Exception as e:
        print(f"[Webhook] Error: {e}")
        return jsonify({"error": str(e)}), 500

@flask_app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "uptime": int(time.time() - START_TIME)}), 200

@flask_app.route('/', methods=['GET'])
def root():
    return jsonify({"status": "Bot Reivaldo Nokos aktif", "webhook": "/webhook/smscode"}), 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# --- HANDLER WEBHOOK (Dijalankan di event loop Telegram) ---
async def handle_webhook_otp(order_id, otp_code, otp_message, phone):
    """Dipanggil dari Flask thread saat OTP diterima via webhook."""
    global webhook_notified_orders
    try:
        if not order_id:
            return
        if order_id in webhook_notified_orders:
            return  # Sudah pernah diberitahu
        webhook_notified_orders.add(order_id)

        phone_display = format_phone(phone)
        # Update DB
        if otp_code:
            db_update_order(order_id, "OTP_RECEIVED", otp_code)

        # Kirim pesan ke admin
        try:
            if otp_code:
                teks = (f"⚡ <b>OTP Diterima (INSTAN via Webhook)</b>\n\n"
                        f"📱 Nomor: <code>{phone_display}</code>\n"
                        f"🔑 Kode: <code>{otp_code}</code>\n"
                        f"🆔 ID: <code>{order_id}</code>")
            else:
                teks = (f"⚡ <b>SMS Diterima (Webhook)</b>\n\n"
                        f"📱 Nomor: <code>{phone_display}</code>\n"
                        f"📩 Isi: <code>{(otp_message or '')[:200]}</code>\n"
                        f"🆔 ID: <code>{order_id}</code>")
            await telegram_bot_instance.bot.send_message(
                chat_id=ADMIN_ID,
                text=teks,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{order_id}")]]),
                parse_mode='HTML')
        except Exception as e:
            print(f"[Webhook] Gagal kirim pesan: {e}")
    except Exception as e:
        print(f"[Webhook] handle_webhook_otp error: {e}")

async def handle_webhook_status_change(order_id, event):
    """Handle order cancelled/expired/completed dari webhook."""
    global webhook_notified_orders
    try:
        if not order_id:
            return
        if event == "order.cancelled":
            db_update_order(order_id, "CANCELLED")
        elif event == "order.expired":
            db_update_order(order_id, "EXPIRED")
        elif event == "order.completed":
            db_update_order(order_id, "COMPLETED")
        webhook_notified_orders.add(order_id)
    except Exception as e:
        print(f"[Webhook] status change error: {e}")

# --- HELPER ---
def format_rupiah(n):
    try: return f"{int(n):,}".replace(",", ".")
    except: return str(n)

def format_countdown(seconds):
    if seconds < 0: seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

async def send_auto_delete_message(context, chat_id, text, parse_mode='HTML', delete_after=AUTO_DELETE_SECONDS, reply_markup=None):
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        asyncio.create_task(auto_delete_task(context, chat_id, msg.message_id, delete_after))
        return msg
    except Exception as e:
        print(f"send_auto_delete error: {e}")
        return None

async def auto_delete_task(context, chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

# --- DATABASE ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY, user_id INTEGER, phone TEXT, product_id INTEGER,
        price INTEGER, status TEXT, otp TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    for col_sql in [
        "ALTER TABLE orders ADD COLUMN refund_amount INTEGER DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN refunded_at TIMESTAMP"
    ]:
        try: c.execute(col_sql)
        except: pass
    c.execute('''CREATE TABLE IF NOT EXISTS templates (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER,
        price INTEGER, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        level TEXT, message TEXT, user_id INTEGER
    )''')
    conn.commit(); conn.close()

def db_save_log(level, message, user_id=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO logs (level, message, user_id) VALUES (?, ?, ?)", (level, message[:500], user_id))
        conn.commit(); conn.close()
    except: pass

def db_save_order(order_id, user_id, phone, product_id, price):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO orders (order_id, user_id, phone, product_id, price, status) VALUES (?, ?, ?, ?, ?, 'ACTIVE')",
                     (str(order_id), user_id, phone, product_id, price))
        conn.commit(); conn.close()
    except Exception as e: print(f"DB Error: {e}")

def db_update_order(order_id, status, otp=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        if otp: conn.execute("UPDATE orders SET status=?, otp=? WHERE order_id=?", (status, otp, str(order_id)))
        else: conn.execute("UPDATE orders SET status=? WHERE order_id=?", (status, str(order_id)))
        conn.commit(); conn.close()
    except Exception as e: print(f"DB Error: {e}")

def db_get_history(user_id, limit=10, filter_type="all"):
    try:
        conn = sqlite3.connect(DB_PATH)
        if filter_type == "today":
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? AND DATE(created_at, '+7 hours') = DATE('now', '+7 hours') ORDER BY created_at DESC LIMIT ?"
        elif filter_type == "week":
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? AND datetime(created_at, '+7 hours') >= datetime('now', '-7 days', '+7 hours') ORDER BY created_at DESC LIMIT ?"
        elif filter_type == "month":
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? AND strftime('%Y-%m', datetime(created_at, '+7 hours')) = strftime('%Y-%m', 'now', '+7 hours') ORDER BY created_at DESC LIMIT ?"
        else:
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?"
        rows = conn.execute(q, (user_id, limit)).fetchall()
        conn.close(); return rows
    except Exception as e: print(f"DB Error: {e}"); return []

def db_get_user_stats(user_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=?", (user_id,)).fetchone()
        completed = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='COMPLETED'", (user_id,)).fetchone()[0]
        cancelled = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='CANCELLED'", (user_id,)).fetchone()[0]
        conn.close()
        return {"total": total[0], "spent": total[1], "completed": completed, "cancelled": cancelled}
    except: return {"total": 0, "spent": 0, "completed": 0, "cancelled": 0}

def db_get_stats():
    try:
        conn = sqlite3.connect(DB_PATH)
        today = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE DATE(created_at, '+7 hours') = DATE('now', '+7 hours')").fetchone()
        month = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE strftime('%Y-%m', datetime(created_at, '+7 hours')) = strftime('%Y-%m', 'now', '+7 hours')").fetchone()
        total = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders").fetchone()
        statuses = dict(conn.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall())
        conn.close()
        return {"today": today, "month": month, "total": total, "statuses": statuses}
    except: return None

def db_get_refund_stats(user_id=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        today = conn.execute("SELECT COUNT(*), COALESCE(SUM(refund_amount),0) FROM orders WHERE refund_amount > 0 AND DATE(refunded_at, '+7 hours') = DATE('now', '+7 hours')").fetchone()
        month = conn.execute("SELECT COUNT(*), COALESCE(SUM(refund_amount),0) FROM orders WHERE refund_amount > 0 AND strftime('%Y-%m', datetime(refunded_at, '+7 hours')) = strftime('%Y-%m', 'now', '+7 hours')").fetchone()
        total = conn.execute("SELECT COUNT(*), COALESCE(SUM(refund_amount),0) FROM orders WHERE refund_amount > 0").fetchone()
        conn.close()
        return {"today": today, "month": month, "total": total}
    except: return None

def get_template(key, default=""):
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM templates WHERE key=?", (key,)).fetchone()
        conn.close(); return row[0] if row else default
    except: return default

def set_template(key, value):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO templates (key, value) VALUES (?, ?)", (key, value))
        conn.commit(); conn.close(); return True
    except: return False

def db_watchlist_add(user_id, product_id, price):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO watchlist (user_id, product_id, price) VALUES (?, ?, ?)", (user_id, product_id, price))
        conn.commit(); conn.close(); return True
    except: return False

def db_watchlist_remove_by_id(wid):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
        conn.commit(); conn.close(); return True
    except: return False

def db_watchlist_get(user_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT id, product_id, price FROM watchlist WHERE user_id=?", (user_id,)).fetchall()
        conn.close(); return rows
    except: return []

# --- HTTP ---
def http_get(url):
    req = urllib.request.Request(url, headers=HEADERS, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
        except: return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e: return {"error": str(e)}

def http_post(url, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=HEADERS, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
        except: return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e: return {"error": str(e)}

def http_patch(url, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=HEADERS, method='PATCH')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
        except: return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e: return {"error": str(e)}

def translate_error(err_msg):
    e = err_msg.lower()
    if "unauthorized" in e: return "🔐 API Key salah atau tidak valid."
    if "forbidden" in e: return "🚫 Akses ditolak oleh server SMScode."
    if "not_found" in e or "not found" in e: return "❓ Data tidak ditemukan."
    if "conflict" in e:
        if "cannot cancel order" in e and "otp_received" in e: return "⚠️ Order sudah menerima OTP."
        if "cannot cancel order" in e and "completed" in e: return "ℹ️ Order sudah selesai."
        if "cannot cancel order" in e: return "❌ Order tidak bisa dibatalkan."
        if "idempotency_key_reused" in e: return "⚠️ Order duplikat."
        if "request_in_progress" in e: return "⏳ Permintaan masih diproses."
        return "⚠️ Konflik status order."
    if "cancel_too_early" in e:
        match = re.search(r'wait (\d+) more seconds', err_msg)
        if match: return f"⏳ Tunggu {match.group(1)} detik lagi."
        return "⏳ Tunggu sebentar lagi."
    if "validation_error" in e: return "❌ Data tidak valid."
    if "no numbers available" in e or "no_numbers" in e: return "📭 Stok habis."
    if "no available offer" in e: return "📭 Tidak ada penawaran."
    if "insufficient_balance" in e or "insufficient balance" in e: return "💸 Saldo tidak cukup."
    if "rate limit" in e or "too many requests" in e or "429" in e: return "⏱️ Terlalu banyak permintaan."
    if "service unavailable" in e or "503" in e: return "🔧 Layanan SMScode sedang down."
    if "bad gateway" in e or "502" in e: return "🔧 Server SMScode sedang bermasalah (502)."
    if "provider_error" in e: return "❌ Provider error."
    if "fx_rate_unavailable" in e: return "💱 Kurs tidak tersedia."
    if "otp_timeout" in e: return "⏰ Waktu OTP habis."
    if "expired" in e: return "⌛ Order kadaluarsa."
    if "cancelled" in e: return "❌ Order sudah dibatalkan."
    if "completed" in e: return "✅ Order sudah selesai."
    if "bad request" in e or "400" in e: return "❌ Permintaan tidak valid."
    if "internal server error" in e or "500" in e: return "⚠️ Server bermasalah."
    if "timeout" in e or "timed out" in e: return "🌐 Koneksi timeout. Coba lagi."
    if "connection" in e and ("error" in e or "refused" in e): return "🌐 Gagal terhubung."
    if "network" in e: return "🌐 Masalah jaringan."
    if "bad_key" in e or "invalid api key" in e: return "🔐 API Key tidak valid."
    if "missing api token" in e or "missing token" in e: return "🔐 API Key belum diatur."
    match = re.search(r'"message"\s*:\s*"([^"]+)"', err_msg)
    if match: return f"⚠️ {match.group(1)[:180]}"
    return f"⚠️ {err_msg[:180]}"

def is_admin(uid): return str(uid) == str(ADMIN_ID)
def format_phone(p):
    if p and p.startswith("62"): return p[2:]
    return p

def get_uptime():
    secs = int(time.time() - START_TIME)
    days = secs // 86400; hours = (secs % 86400) // 3600; mins = (secs % 3600) // 60
    if days > 0: return f"{days}h {hours}j {mins}m"
    elif hours > 0: return f"{hours}j {mins}m"
    return f"{mins}m"

async def log_error(context, error_msg, user_id=None):
    db_save_log("ERROR", error_msg, user_id)
    try:
        teks = f"⚠️ <b>Error Log</b>\n\n"
        if user_id: teks += f"User: <code>{user_id}</code>\n"
        teks += f"Pesan: <code>{error_msg[:400]}</code>\nWaktu: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        await context.bot.send_message(chat_id=ADMIN_ID, text=teks, parse_mode='HTML')
    except: pass

def get_balance():
    data = http_get(f"{BASE_URL}/balance")
    if data.get("success"): return data["data"].get("balance", 0)
    return -1

# --- API SMScode ---
def get_gojek_products():
    data = http_get(f"{BASE_URL}/catalog/products?country_id=7")
    if "error" in data: return None, translate_error(data["error"])
    if not data.get("success"): return None, "Gagal ambil data."
    products = data.get("data", [])
    if not isinstance(products, list): return None, "Format data tidak valid."
    gojek = [p for p in products if "Gojek" in p.get("name", "")]
    gojek.sort(key=lambda x: x.get('price', 999999))
    return gojek, None

def get_active_orders():
    data = http_get(f"{BASE_URL}/orders/active")
    if "error" in data: return None, translate_error(data["error"])
    if not data.get("success"): return None, "Gagal ambil order."
    orders = data.get("data", [])
    if not isinstance(orders, list): return None, "Format data tidak valid."
    return orders, None

def cek_saldo_api():
    if not SMScode_API_KEY: return "❌ API Key belum diatur.", -1
    data = http_get(f"{BASE_URL}/balance")
    if "error" in data: return translate_error(data["error"]), -1
    if data.get("success"):
        saldo = data["data"].get("balance", 0)
        return f"💰 Saldo Saya: <b>Rp {format_rupiah(saldo)}</b>", saldo
    return "⚠️ Gagal ambil saldo.", -1

def beli_nomor_api(product_id, user_id, price):
    if not SMScode_API_KEY: return None, "❌ API Key belum diatur."
    saldo = get_balance()
    if saldo >= 0 and saldo < price:
        return None, f"❌ Saldo tidak cukup.\nSaldo: Rp {format_rupiah(saldo)}\nButuh: Rp {format_rupiah(price)}"
    data = http_post(f"{BASE_URL}/orders/create", {"product_id": product_id})
    if "error" in data: return None, translate_error(data["error"])
    if data.get("success"):
        od = data["data"]["orders"][0]
        oid = str(od["id"]); nomor = od["phone_number"]
        db_save_order(oid, user_id, nomor, product_id, price)
        return oid, nomor
    return None, "⚠️ Gagal membeli nomor."

async def beli_banyak_api_async(product_id, user_id, price, quantity):
    if not SMScode_API_KEY: return None, 0, "❌ API Key belum diatur."
    if quantity > MAX_BULK: return None, 0, f"❌ Maksimal {MAX_BULK} nomor."
    total_cost = price * quantity
    saldo = get_balance()
    if saldo >= 0 and saldo < total_cost:
        return None, 0, (f"❌ Saldo tidak cukup.\nSaldo: Rp {format_rupiah(saldo)}\n"
                         f"Butuh: Rp {format_rupiah(total_cost)} ({quantity}×Rp {format_rupiah(price)})")
    all_results = []
    total_failed = 0
    remaining = quantity
    batch_num = 0
    while remaining > 0:
        batch_num += 1
        this_batch = min(BATCH_SIZE, remaining)
        data = http_post(f"{BASE_URL}/orders/create", {"product_id": product_id, "quantity": this_batch})
        if "error" in data:
            if batch_num == 1:
                return None, 0, translate_error(data["error"])
            break
        if data.get("success"):
            orders = data["data"].get("orders", [])
            failed = data["data"].get("failed_count", 0)
            for od in orders:
                oid = str(od["id"]); nomor = od["phone_number"]
                db_save_order(oid, user_id, nomor, product_id, price)
                all_results.append((oid, nomor))
            total_failed += failed
            remaining -= len(orders) + failed
            if len(orders) == 0:
                break
        else:
            break
        if remaining > 0:
            await asyncio.sleep(3)
    if not all_results:
        return None, 0, "⚠️ Tidak ada order yang berhasil dibuat."
    return all_results, total_failed, None

def batal_order_api(order_id):
    data = http_post(f"{BASE_URL}/orders/cancel", {"id": int(order_id)})
    if "error" in data: return {"success": False, "message": translate_error(data["error"]), "refund_amount": 0}
    if data.get("success"):
        refund_amount = 0
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT price FROM orders WHERE order_id=?", (str(order_id),)).fetchone()
            refund_amount = row[0] if row else 0
            conn.execute("UPDATE orders SET status='CANCELLED', refund_amount=?, refunded_at=CURRENT_TIMESTAMP WHERE order_id=?",
                         (refund_amount, str(order_id)))
            conn.commit(); conn.close()
        except Exception as e:
            print(f"DB Error: {e}")
            db_update_order(order_id, "CANCELLED")
        return {"success": True, "message": "✅ Nomor berhasil dibatalkan & saldo dikembalikan.", "refund_amount": refund_amount}
    return {"success": False, "message": "⚠️ Gagal membatalkan nomor.", "refund_amount": 0}

def selesai_order_api(order_id):
    data = http_post(f"{BASE_URL}/orders/finish", {"id": int(order_id)})
    if "error" in data: return {"success": False, "message": translate_error(data["error"])}
    if data.get("success"):
        db_update_order(order_id, "COMPLETED")
        return {"success": True, "message": "✅ Nomor berhasil diselesaikan."}
    return {"success": False, "message": "⚠️ Gagal menyelesaikan nomor."}

# ============================================================
# === REGISTER WEBHOOK URL KE SMScode ===
# ============================================================
async def register_webhook_to_smscode():
    """Daftarkan URL webhook ke SMScode saat startup."""
    try:
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        if not domain:
            print("[Webhook] RAILWAY_PUBLIC_DOMAIN tidak ada, skip registrasi.")
            return
        webhook_url = f"https://{domain}/webhook/smscode"
        print(f"[Webhook] Mendaftarkan URL: {webhook_url}")
        data = http_patch(f"{BASE_URL}/webhook", {"webhook_url": webhook_url})
        if data.get("success"):
            new_secret = data.get("data", {}).get("webhook_secret")
            if new_secret:
                print(f"[Webhook] Secret baru dari SMScode: {new_secret[:8]}...")
                print(f"[Webhook] ⚠️ SIMPAN SECRET INI DI RAILWAY VARIABLE 'SMScode_WEBHOOK_SECRET'")
            print(f"[Webhook] URL berhasil didaftarkan!")
        else:
            print(f"[Webhook] Gagal daftar: {data}")
    except Exception as e:
        print(f"[Webhook] register error: {e}")

# ============================================================
# === BULK SUMMARY BUILDER ===
# ============================================================
def build_bulk_summary(ordered_list, received_set, price_per_unit, remaining_seconds, failed_count=0):
    total = len(ordered_list)
    jumlah_dapat = len(received_set)
    timer_str = format_countdown(remaining_seconds)
    text = f"🛒 <b>Berhasil Beli {total} Nomor!</b>\n"
    if failed_count > 0:
        text += f"⚠️ Gagal: <b>{failed_count}</b> nomor\n"
    text += f"📊 Progress: <b>{jumlah_dapat}/{total}</b> menerima OTP\n"
    text += f"⏱️ Sisa waktu: <b>{timer_str}</b>\n\n"
    for i, (oid, phone) in enumerate(ordered_list, 1):
        check = " ✅" if oid in received_set else ""
        text += f"{i}.<code>{phone}</code>{check}\n\n"
    text += (f"━━━━━━━━━━━━━━━━━━━━\n"
             f"💰 Total: Rp {format_rupiah(price_per_unit * total)}\n"
             f"📋 <b>Cara pakai:</b>\n"
             f"1️⃣ Masukkan nomor ke aplikasi Gojek satu per satu\n"
             f"2️⃣ Bot akan kirim OTP otomatis dengan nomor\n"
             f"3️⃣ Klik ✅ Selesai setelah OTP dipakai\n\n"
             f"⏱️ Timer berhenti saat waktu habis ({TIMEOUT_MINUTES} menit).")
    return text

# ============================================================
# === SINGLE ORDER POLLING (FALLBACK) ===
# ============================================================
async def auto_poll_otp(chat_id, context, order_id, nomor_display):
    total = TIMEOUT_SECONDS
    for i in range(total // 5):
        await asyncio.sleep(5)
        # Skip kalau webhook sudah notif duluan
        if order_id in webhook_notified_orders:
            return
        elapsed = (i + 1) * 5; remaining = total - elapsed
        data = http_get(f"{BASE_URL}/orders/{order_id}")
        if not data.get("success"): continue
        od = data["data"]; otp = od.get("otp_code"); status = od.get("status")
        msg_id = pending_messages.get(order_id)
        if otp:
            db_update_order(order_id, "OTP_RECEIVED", otp)
            tmpl = get_template("otp_found", "🔑 <b>Kode OTP Ditemukan!</b>\n\n📱 Nomor: <code>{nomor}</code>\n🔑 Kode: <code>{otp}</code>")
            teks = tmpl.replace("{otp}", otp).replace("{nomor}", nomor_display).replace("{order_id}", order_id)
            kb = [[InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")],
                  [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
            if msg_id:
                try: await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML'); pending_messages.pop(order_id, None)
                except: pass
            else:
                try: await context.bot.send_message(chat_id=chat_id, text=teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                except: pass
            return
        elif status in ["CANCELLED", "EXPIRED", "COMPLETED"]:
            db_update_order(order_id, status)
            if msg_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=msg_id); pending_messages.pop(order_id, None)
                except: pass
            return
        elif status == "ACTIVE" and remaining > 0 and msg_id:
            h = remaining // 3600; m = (remaining % 3600) // 60; s = remaining % 60
            try:
                kb = [[InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
                      [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                    text=f"⏳ <b>Status:</b> Menunggu SMS masuk\n⏱️ Sisa waktu: <b>{h:02d}:{m:02d}:{s:02d}</b>\n\nBot akan otomatis mengubah pesan ini menjadi kode OTP.",
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
            except: pass
    cek = http_get(f"{BASE_URL}/orders/{order_id}")
    otp_akhir = None
    if cek.get("success"): otp_akhir = cek["data"].get("otp_code")
    if otp_akhir:
        db_update_order(order_id, "OTP_RECEIVED", otp_akhir)
        tmpl = get_template("otp_found", "🔑 <b>Kode OTP Ditemukan!</b>\n\n📱 Nomor: <code>{nomor}</code>\n🔑 Kode: <code>{otp}</code>")
        pesan = tmpl.replace("{otp}", otp_akhir).replace("{nomor}", nomor_display).replace("{order_id}", order_id)
        if msg_id:
            try: await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=pesan, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{order_id}")]]), parse_mode='HTML'); pending_messages.pop(order_id, None)
            except: pass
        return
    batal_order_api(order_id)
    if msg_id:
        try: await context.bot.delete_message(chat_id=chat_id, message_id=msg_id); pending_messages.pop(order_id, None)
        except: pass

# ============================================================
# === BULK MONITOR (FALLBACK) ===
# ============================================================
async def bulk_monitor_task(context, chat_id, batch_id, ordered_list, price_per_unit, summary_msg_id, failed_count=0):
    total = len(ordered_list)
    pending = {oid: phone for oid, phone in ordered_list}
    received_set = set()
    start_time = time.time()
    timeout_seconds = TIMEOUT_SECONDS
    active_bulk_batches[batch_id] = {"orders": ordered_list, "received": received_set, "summary_msg_id": summary_msg_id, "pending": pending}
    async def update_summary():
        try:
            elapsed = int(time.time() - start_time)
            remaining = timeout_seconds - elapsed
            if remaining < 0: remaining = 0
            teks = build_bulk_summary(ordered_list, received_set, price_per_unit, remaining, failed_count)
            kb = [[InlineKeyboardButton("📦 Order Aktif", callback_data='aktif')],
                  [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
            await context.bot.edit_message_text(chat_id=chat_id, message_id=summary_msg_id, text=teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
        except Exception: pass
    await update_summary()
    while pending and (time.time() - start_time) < timeout_seconds:
        await asyncio.sleep(5)
        if not pending: break
        data = http_get(f"{BASE_URL}/orders/active")
        active_map = {}
        if data.get("success"):
            for o in data.get("data", []):
                active_map[str(o.get("id"))] = o
        to_remove = []
        for oid, phone in list(pending.items()):
            # Skip kalau webhook sudah handle
            if oid in webhook_notified_orders:
                received_set.add(oid)
                to_remove.append(oid)
                continue
            o = active_map.get(oid)
            if o is None:
                cek = http_get(f"{BASE_URL}/orders/{oid}")
                if cek.get("success"): o = cek["data"]
                else: continue
            otp = o.get("otp_code"); status = o.get("status")
            if otp and oid not in received_set:
                received_set.add(oid)
                db_update_order(oid, "OTP_RECEIVED", otp)
                try:
                    await context.bot.send_message(chat_id=chat_id,
                        text=(f"🔑 <b>OTP Diterima!</b>\n\n📱 Nomor: <code>{phone}</code>\n🔑 Kode: <code>{otp}</code>\n🆔 ID: <code>{oid}</code>\n\n📊 Progress: <b>{len(received_set)}/{total}</b>"),
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{oid}")]]),
                        parse_mode='HTML')
                except Exception: pass
                to_remove.append(oid)
            elif status in ["CANCELLED", "EXPIRED", "COMPLETED"]:
                db_update_order(oid, status); to_remove.append(oid)
        for oid in to_remove: pending.pop(oid, None)
        await update_summary()
    await update_summary()
    if pending:
        for oid, phone in list(pending.items()):
            batal_order_api(oid)
        await update_summary()
    active_bulk_batches.pop(batch_id, None)

# ============================================================
# === BATCH CANCEL TASK ===
# ============================================================
async def batch_cancel_task(context, chat_id, user_id):
    orders, err = get_active_orders()
    if err:
        try: await context.bot.send_message(chat_id, f"❌ {err}")
        except: pass
        return
    to_cancel = [o for o in orders if not o.get("otp_code")]
    skipped = len(orders) - len(to_cancel)
    total = len(to_cancel)
    if total == 0:
        try:
            await context.bot.send_message(chat_id,
                f"✅ Tidak ada nomor yang perlu dibatalkan.\n\n📭 Semua {skipped} nomor aktif sudah menerima OTP.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]), parse_mode='HTML')
        except: pass
        return
    success = 0; failed = 0; refund_total = 0; failed_oids = []
    for o in to_cancel:
        oid = str(o.get("id"))
        hasil = batal_order_api(oid)
        if hasil["success"]: success += 1; refund_total += hasil.get("refund_amount", 0)
        else: failed += 1; failed_oids.append(oid)
        await asyncio.sleep(0.3)
    teks = (f"✅ <b>Batalkan Massal Selesai</b>\n\n"
            f"📊 Total diperiksa: <b>{len(orders)}</b>\n🗑️ Dibatalkan: <b>{success}</b>\n"
            f"⏭️ Dilewati (sudah OTP): <b>{skipped}</b>\n❌ Gagal: <b>{failed}</b>\n\n"
            f"💰 <b>Total Refund: Rp {format_rupiah(refund_total)}</b>")
    if failed_oids:
        teks += f"\n\n⚠️ ID gagal: {', '.join(failed_oids[:5])}"
        if len(failed_oids) > 5: teks += f" dan {len(failed_oids) - 5} lainnya"
    try:
        await context.bot.send_message(chat_id, teks, parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
    except: pass

# ============================================================
# === BACKGROUND TASKS ===
# ============================================================
async def restock_checker_task(context):
    while True:
        await asyncio.sleep(5 * 60)
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute("SELECT id, user_id, product_id, price FROM watchlist").fetchall()
            conn.close()
            if not rows: continue
            products, err = get_gojek_products()
            if err or not products: continue
            for wid, uid, pid, price in rows:
                for p in products:
                    if p.get('id') == pid and p.get('available', 0) > 0:
                        try:
                            await context.bot.send_message(chat_id=uid,
                                text=f"🎉 <b>Stok Kembali Tersedia!</b>\n\n📦 Gojek\n💵 Harga: Rp {format_rupiah(price)}\n📊 Stok: {p.get('available')}\n\nSegera beli!",
                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Beli Sekarang", callback_data='beli_nomor')]]), parse_mode='HTML')
                        except: pass
                        db_watchlist_remove_by_id(wid)
                        break
        except Exception as e: print(f"Restock error: {e}")

async def auto_backup_task(context):
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            if os.path.exists(DB_PATH):
                with open(DB_PATH, 'rb') as f:
                    await context.bot.send_document(chat_id=ADMIN_ID, document=f,
                        filename=f"backup_{time.strftime('%Y%m%d_%H%M')}.db",
                        caption=f"🗄️ <b>Backup Otomatis</b>\n{time.strftime('%Y-%m-%d %H:%M:%S')} WIB", parse_mode='HTML')
        except Exception as e: print(f"Backup error: {e}")

# ============================================================
# === MENU KEYBOARDS ===
# ============================================================
def kb_menu_utama():
    return [
        [InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')],
        [InlineKeyboardButton("💼 Akun Saya", callback_data='akun_saya'), InlineKeyboardButton("⭐ Favorit", callback_data='favorit')],
        [InlineKeyboardButton("📜 Riwayat", callback_data='riwayat_menu'), InlineKeyboardButton("ℹ️ Info & Bantuan", callback_data='info_bantuan')]
    ]

def kb_akun():
    return [
        [InlineKeyboardButton("💰 Saldo", callback_data='cek_saldo'), InlineKeyboardButton("📊 Statistik Saya", callback_data='stats_saya')],
        [InlineKeyboardButton("📦 Order Aktif", callback_data='aktif')],
        [InlineKeyboardButton("⚙️ Pengaturan", callback_data='pengaturan')],
        [InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]
    ]

def kb_aktif_menu():
    return [
        [InlineKeyboardButton("❌ Batalkan 1 Nomor", callback_data='aktif_cancel')],
        [InlineKeyboardButton("✅ Selesaikan 1 Nomor", callback_data='aktif_finish')],
        [InlineKeyboardButton("🗑️ Batalkan Semua (Tanpa OTP)", callback_data='aktif_cancel_all')],
        [InlineKeyboardButton("🔙 Kembali", callback_data='akun_saya')]
    ]

def kb_pengaturan():
    return [
        [InlineKeyboardButton("🔔 Notifikasi Restock", callback_data='notif_menu')],
        [InlineKeyboardButton("💬 Template Pesan", callback_data='template_menu')],
        [InlineKeyboardButton("🗄️ Backup Database", callback_data='backup_now')],
        [InlineKeyboardButton("🔙 Kembali", callback_data='akun_saya')]
    ]

def kb_info_bantuan():
    return [
        [InlineKeyboardButton("📊 Status Bot", callback_data='info_status')],
        [InlineKeyboardButton("📖 FAQ", callback_data='info_faq')],
        [InlineKeyboardButton("📞 Kontak Admin", callback_data='info_kontak')],
        [InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]
    ]

def kb_riwayat():
    return [
        [InlineKeyboardButton("📅 Hari Ini", callback_data='riwayat_today'), InlineKeyboardButton("📆 7 Hari", callback_data='riwayat_week')],
        [InlineKeyboardButton("🗓️ Bulan Ini", callback_data='riwayat_month'), InlineKeyboardButton("📋 Semua", callback_data='riwayat_all')],
        [InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]
    ]

def kb_favorit():
    return [
        [InlineKeyboardButton("➕ Tambah dari Menu Beli", callback_data='beli_nomor')],
        [InlineKeyboardButton("🔄 Refresh", callback_data='favorit')],
        [InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]
    ]

def kb_bulk_qty(pid, harga):
    return [
        [InlineKeyboardButton("📦 5 Nomor", callback_data=f"bulkqty_{pid}_{harga}_5"), InlineKeyboardButton("📦 10 Nomor", callback_data=f"bulkqty_{pid}_{harga}_10")],
        [InlineKeyboardButton("📦 20 Nomor", callback_data=f"bulkqty_{pid}_{harga}_20"), InlineKeyboardButton("📦 30 Nomor", callback_data=f"bulkqty_{pid}_{harga}_30")],
        [InlineKeyboardButton("📦 40 Nomor", callback_data=f"bulkqty_{pid}_{harga}_40"), InlineKeyboardButton("📦 50 Nomor", callback_data=f"bulkqty_{pid}_{harga}_50")],
        [InlineKeyboardButton("📦 100 Nomor", callback_data=f"bulkqty_{pid}_{harga}_100")],
        [InlineKeyboardButton("🔙 Kembali", callback_data='bulk_menu')]
    ]

# ============================================================
# === TAMPILAN MENU ===
# ============================================================
async def send_menu_utama(query, context):
    welcome = get_template("welcome", "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>")
    user = query.from_user.first_name or "Bos"
    _, s = cek_saldo_api()
    stats = db_get_user_stats(query.from_user.id)
    teks = (f"{welcome}\n\n👋 Halo, <b>{user}</b>!\n\n━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Saldo Saya:</b> Rp {format_rupiah(s) if s >= 0 else '-'}\n"
            f"📊 <b>Total Order:</b> {stats['total']} | ✅ {stats['completed']} selesai\n"
            f"⏱️ <b>Server Uptime:</b> {get_uptime()}\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💡 <i>Bot otomatis membatalkan nomor jika OTP tidak masuk dalam {TIMEOUT_MINUTES} menit.</i>")
    await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_menu_utama()), parse_mode='HTML')

async def send_products_menu(chat_id, context, query=None):
    products, err = get_gojek_products()
    if err:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]])
        if query: await query.edit_message_text(f"❌ {err}", reply_markup=kb)
        else: await context.bot.send_message(chat_id, f"❌ {err}", reply_markup=kb)
        return
    if not products:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]])
        text = "❌ Tidak ada produk Gojek tersedia saat ini."
        if query: await query.edit_message_text(text, reply_markup=kb)
        else: await context.bot.send_message(chat_id, text, reply_markup=kb)
        return
    keyboard = []; seen = set()
    for p in products:
        harga = p.get('price', '?'); pid = p.get('id'); stok = p.get('available', 0)
        if harga in seen: continue
        seen.add(harga)
        if stok and stok > 0:
            keyboard.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(harga)} | 📦 {stok}", callback_data=f"buy_{pid}_{harga}")])
        else:
            keyboard.append([InlineKeyboardButton(f"❌ Rp {format_rupiah(harga)} (Habis) | 🔔 Pantau", callback_data=f"watch_{pid}_{harga}")])
        if len(keyboard) >= 8: break
    keyboard.append([InlineKeyboardButton("📦 Beli Banyak (Bulk)", callback_data='bulk_menu')])
    keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')])
    text = ("🛒 <b>Pilih Harga Nomor Gojek</b>\n\n"
            "📦 = stok | 🔔 = pantau saat habis\n"
            "⚡ Klik harga untuk beli 1 nomor.\n"
            "📦 Klik <b>Beli Banyak</b> untuk beli sekaligus (maks 100).")
    if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else: await context.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def send_bulk_menu(chat_id, context, query=None):
    products, err = get_gojek_products()
    if err:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]])
        if query: await query.edit_message_text(f"❌ {err}", reply_markup=kb)
        else: await context.bot.send_message(chat_id, f"❌ {err}", reply_markup=kb)
        return
    if not products:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]])
        text = "❌ Tidak ada produk Gojek tersedia saat ini."
        if query: await query.edit_message_text(text, reply_markup=kb)
        else: await context.bot.send_message(chat_id, text, reply_markup=kb)
        return
    keyboard = []; seen = set()
    for p in products:
        harga = p.get('price', '?'); pid = p.get('id'); stok = p.get('available', 0)
        if harga in seen: continue
        seen.add(harga)
        if stok and stok > 0:
            keyboard.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(harga)} | 📦 {stok}", callback_data=f"bulkprice_{pid}_{harga}")])
        if len(keyboard) >= 8: break
    keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')])
    text = ("📦 <b>Beli Banyak (Bulk)</b>\n\n"
            f"Pilih harga, lalu pilih jumlah: 5, 10, 20, 30, 40, 50, atau 100.\n\n"
            f"⚙️ <b>Sistem Aman:</b>\n"
            f"• <b>Webhook aktif</b> — OTP terdeteksi instan\n"
            f"• Pembelian dipecah jadi batch 10 nomor\n"
            f"• Timer live {TIMEOUT_MINUTES} menit\n"
            f"• Daftar nomor bernomor urut + centang ✅ otomatis")
    if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else: await context.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def send_active_orders_menu(chat_id, context, action, query=None):
    orders, err = get_active_orders()
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='aktif')]])
    if err:
        if query: await query.edit_message_text(f"❌ {err}", reply_markup=kb_back)
        else: await context.bot.send_message(chat_id, f"❌ {err}")
        return
    if not orders:
        text = "📭 Tidak ada nomor aktif saat ini."
        if query: await query.edit_message_text(text, reply_markup=kb_back)
        else: await context.bot.send_message(chat_id, text, reply_markup=kb_back)
        return
    prefix = "cancel_" if action == 'cancel' else "finish_"
    judul = "❌ <b>Pilih Nomor untuk DIBATALKAN</b>" if action == 'cancel' else "✅ <b>Pilih Nomor untuk DISELESAIKAN</b>"
    keyboard = []
    for o in orders[:50]:
        oid = o.get('id'); nomor = format_phone(o.get('phone_number', '?'))
        status = o.get('status', '?'); otp = o.get('otp_code')
        label = f"📱 {nomor}"
        if otp: label += f" | 🔑 {otp}"
        elif status == 'ACTIVE': label += " | ⏳"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"{prefix}{oid}")])
    keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data='aktif')])
    text = f"{judul}\n\nTotal: {len(orders)} nomor"
    if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else: await context.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# ============================================================
# === HANDLER UTAMA ===
# ============================================================
async def start(update, context):
    welcome = get_template("welcome", "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>")
    user = update.effective_user.first_name or "Bos"
    _, s = cek_saldo_api()
    stats = db_get_user_stats(update.effective_user.id)
    teks = (f"{welcome}\n\n👋 Halo, <b>{user}</b>!\n\n━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Saldo Saya:</b> Rp {format_rupiah(s) if s >= 0 else '-'}\n"
            f"📊 <b>Total Order:</b> {stats['total']} | ✅ {stats['completed']} selesai\n"
            f"⏱️ <b>Server Uptime:</b> {get_uptime()}\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💡 <i>Bot otomatis membatalkan nomor jika OTP tidak masuk dalam {TIMEOUT_MINUTES} menit.</i>")
    await update.message.reply_text(teks, reply_markup=InlineKeyboardMarkup(kb_menu_utama()), parse_mode='HTML')
    if s >= 0 and s < LOW_BALANCE_THRESHOLD:
        await update.message.reply_text(f"⚠️ <b>Peringatan Saldo Rendah!</b>\n\nSaldo: <b>Rp {format_rupiah(s)}</b>\nSegera top-up!", parse_mode='HTML')

async def help_command(update, context):
    await update.message.reply_text(
        "❓ <b>Daftar Perintah:</b>\n\n🚀 /start - Menu utama\n🛒 /beli - Beli nomor\n💰 /saldo - Cek saldo\n"
        "📦 /aktif - Nomor aktif\n📜 /riwayat - Riwayat order\n📊 /stats - Statistik\n"
        "💸 /refund - Statistik refund\n"
        "❌ /batal - Batalkan\n✅ /selesai - Selesaikan\n\n"
        "👑 <b>Admin:</b>\n/setwelcome [teks]\n/setotp [teks]\n/backup\n/logs - Log error\n/webhook_status - Cek status webhook",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("🚫 Akses ditolak."); return
    data = query.data
    try:
        if data == 'menu_utama': await send_menu_utama(query, context)
        elif data == 'akun_saya':
            _, s = cek_saldo_api()
            stats = db_get_user_stats(user_id)
            teks = (f"💼 <b>Akun Saya</b>\n\n👤 Nama: {query.from_user.first_name or '-'}\n🆔 ID: <code>{user_id}</code>\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n💰 <b>Saldo Saya:</b> Rp {format_rupiah(s) if s >= 0 else '-'}\n"
                    f"📦 <b>Total Order:</b> {stats['total']}\n✅ <b>Selesai:</b> {stats['completed']}\n"
                    f"❌ <b>Dibatalkan:</b> {stats['cancelled']}\n💸 <b>Total Belanja:</b> Rp {format_rupiah(stats['spent'])}\n"
                    f"━━━━━━━━━━━━━━━━━━━━")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_akun()), parse_mode='HTML')

        elif data == 'cek_saldo':
            teks, saldo = cek_saldo_api()
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_akun()), parse_mode='HTML')
            if saldo >= 0 and saldo < LOW_BALANCE_THRESHOLD:
                await context.bot.send_message(query.message.chat_id, f"⚠️ <b>Saldo Rendah!</b>\n\nSaldo: <b>Rp {format_rupiah(saldo)}</b>", parse_mode='HTML')

        elif data == 'stats_saya':
            stats = db_get_user_stats(user_id)
            s = db_get_stats()
            teks = (f"📊 <b>Statistik Saya</b>\n\n📦 Total Order: <b>{stats['total']}</b>\n✅ Selesai: <b>{stats['completed']}</b>\n"
                    f"❌ Dibatalkan: <b>{stats['cancelled']}</b>\n💸 Total Belanja: <b>Rp {format_rupiah(stats['spent'])}</b>\n\n")
            if s:
                teks += (f"━━━━━━━━━━━━━━━━━━━━\n🌐 <b>Global:</b>\n📅 Hari ini: {s['today'][0]} order (Rp {format_rupiah(s['today'][1])})\n"
                         f"📆 Bulan ini: {s['month'][0]} order (Rp {format_rupiah(s['month'][1])})\n"
                         f"📈 Total: {s['total'][0]} order (Rp {format_rupiah(s['total'][1])})")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_akun()), parse_mode='HTML')

        elif data == 'aktif':
            await query.edit_message_text("📦 <b>Order Aktif</b>\n\nSilakan pilih aksi:", reply_markup=InlineKeyboardMarkup(kb_aktif_menu()), parse_mode='HTML')
        elif data == 'aktif_cancel': await send_active_orders_menu(query.message.chat_id, context, 'cancel', query=query)
        elif data == 'aktif_finish': await send_active_orders_menu(query.message.chat_id, context, 'finish', query=query)

        elif data == 'aktif_cancel_all':
            orders, err = get_active_orders()
            if err:
                await query.edit_message_text(f"❌ {err}", reply_markup=InlineKeyboardMarkup(kb_aktif_menu()))
                return
            to_cancel = [o for o in orders if not o.get("otp_code")]
            skipped = len(orders) - len(to_cancel)
            if not to_cancel:
                await query.edit_message_text(
                    f"✅ <b>Tidak Ada yang Perlu Dibatalkan</b>\n\nSemua <b>{skipped}</b> nomor aktif sudah menerima OTP.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='aktif')]]), parse_mode='HTML')
                return
            await query.edit_message_text(
                f"⚠️ <b>Konfirmasi Batalkan Massal</b>\n\n"
                f"🗑️ Akan dibatalkan: <b>{len(to_cancel)}</b> nomor (belum dapat OTP)\n"
                f"⏭️ Dilewati: <b>{skipped}</b> nomor (sudah dapat OTP)\n\n"
                f"💰 Saldo akan dikembalikan ke akun Anda.\n\nLanjutkan?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Ya, Batalkan Semua", callback_data='aktif_cancel_all_confirm')],
                    [InlineKeyboardButton("❌ Batal", callback_data='aktif')]
                ]), parse_mode='HTML')

        elif data == 'aktif_cancel_all_confirm':
            await query.edit_message_text("⏳ <b>Sedang membatalkan semua nomor...</b>\n\nMohon tunggu 1-3 menit.", parse_mode='HTML')
            asyncio.create_task(batch_cancel_task(context, query.message.chat_id, user_id))

        elif data == 'pengaturan':
            await query.edit_message_text("⚙️ <b>Pengaturan</b>\n\nSilakan pilih:", reply_markup=InlineKeyboardMarkup(kb_pengaturan()), parse_mode='HTML')

        elif data == 'notif_menu':
            watchlist = db_watchlist_get(user_id)
            if not watchlist:
                teks = "🔔 <b>Notifikasi Restock</b>\n\nBelum ada produk yang dipantau."
            else:
                teks = f"🔔 <b>Notifikasi Restock</b>\n\nAnda memantau {len(watchlist)} produk:\n\n"
                for wid, pid, price in watchlist: teks += f"🔔 Gojek Rp {format_rupiah(price)}\n"
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='pengaturan')]]), parse_mode='HTML')

        elif data == 'template_menu':
            current_w = get_template("welcome", "(default)")
            current_o = get_template("otp_found", "(default)")
            teks = (f"💬 <b>Template Pesan</b>\n\n<b>1. Sambutan:</b>\n<i>{current_w[:100]}</i>\nUbah: <code>/setwelcome Teks baru</code>\n\n"
                    f"<b>2. Template OTP:</b>\n<i>{current_o[:100]}</i>\nUbah: <code>/setotp Teks dengan {{otp}}</code>\n\n"
                    f"💡 Placeholder: <code>{{otp}}</code>, <code>{{nomor}}</code>, <code>{{order_id}}</code>")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='pengaturan')]]), parse_mode='HTML')

        elif data == 'backup_now':
            try:
                if os.path.exists(DB_PATH):
                    with open(DB_PATH, 'rb') as f:
                        await context.bot.send_document(chat_id=query.message.chat_id, document=f,
                            filename=f"backup_manual_{time.strftime('%Y%m%d_%H%M')}.db", caption="🗄️ Backup Manual")
                    await query.answer("✅ Backup terkirim!", show_alert=True)
            except Exception as e: await query.answer(f"❌ Gagal: {str(e)[:50]}", show_alert=True)

        elif data == 'info_bantuan':
            await query.edit_message_text("ℹ️ <b>Info & Bantuan</b>\n\nPilih menu:", reply_markup=InlineKeyboardMarkup(kb_info_bantuan()), parse_mode='HTML')

        elif data == 'info_status':
            cek = http_get(f"{BASE_URL}/balance")
            sms_status = "🟢 Terhubung" if cek.get("success") else "🔴 Bermasalah"
            db_status = "🟢 OK" if os.path.exists(DB_PATH) else "🔴 Error"
            stats = db_get_stats()
            webhook_status = "🟢 Aktif" if SMScode_WEBHOOK_SECRET else "🟡 Menunggu Secret"
            teks = (f"📊 <b>Status Bot</b>\n\n🤖 <b>Nama:</b> ReivaldoNokos\n📦 <b>Versi:</b> {BOT_VERSION}\n"
                    f"🟢 <b>Status:</b> Online\n⏱️ <b>Uptime:</b> {get_uptime()}\n\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"📡 <b>Koneksi SMScode:</b> {sms_status}\n💾 <b>Database:</b> {db_status}\n"
                    f"🔔 <b>Webhook:</b> {webhook_status}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n📊 Total Order: {stats['total'][0] if stats else 0}\n"
                    f"💸 Pengeluaran: Rp {format_rupiah(stats['total'][1]) if stats else 0}")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='info_bantuan')]]), parse_mode='HTML')

        elif data == 'info_faq':
            teks = (f"📖 <b>FAQ</b>\n\n<b>❓ Kenapa nomor tidak muncul OTP?</b>\n• Cek koneksi internet\n• Tunggu 5-10 menit\n"
                    f"• Jika lewat {TIMEOUT_MINUTES} menit, nomor otomatis dibatalkan & saldo dikembalikan")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='info_bantuan')]]), parse_mode='HTML')

        elif data == 'info_kontak':
            teks = (f"📞 <b>Kontak Admin</b>\n\n👤 <b>Telegram:</b> @mreivaldoo\n💬 <b>Jam Operasional:</b> 24/7")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='info_bantuan')]]), parse_mode='HTML')

        elif data == 'favorit':
            watchlist = db_watchlist_get(user_id)
            if not watchlist: teks = "⭐ <b>Favorit</b>\n\nBelum ada produk favorit."
            else:
                teks = f"⭐ <b>Favorit</b>\n\nAnda memantau {len(watchlist)} produk:\n\n"
                for wid, pid, price in watchlist: teks += f"⭐ Gojek Rp {format_rupiah(price)}\n"
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_favorit()), parse_mode='HTML')

        elif data == 'riwayat_menu':
            await query.edit_message_text("📜 <b>Riwayat Order</b>\n\nPilih rentang waktu:", reply_markup=InlineKeyboardMarkup(kb_riwayat()), parse_mode='HTML')

        elif data.startswith('riwayat_'):
            filter_type = data.replace('riwayat_', '')
            label = {"today": "Hari Ini", "week": "7 Hari Terakhir", "month": "Bulan Ini", "all": "Semua"}.get(filter_type, "Semua")
            rows = db_get_history(user_id, 20, filter_type)
            if not rows: teks = f"📜 <b>Riwayat ({label})</b>\n\nBelum ada transaksi."
            else:
                teks = f"📜 <b>Riwayat ({label})</b>\n\nMenampilkan {len(rows)} transaksi:\n\n"
                for oid, phone, status, otp, created in rows:
                    emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else ("❌" if status == "CANCELLED" else "⏳"))
                    teks += f"{emoji} <code>{format_phone(phone)}</code>\n   {status} | OTP: {otp or '-'}\n   {created}\n\n"
            await query.edit_message_text(teks[:4000], reply_markup=InlineKeyboardMarkup(kb_riwayat()), parse_mode='HTML')

        elif data == 'beli_nomor': await send_products_menu(query.message.chat_id, context, query=query)
        elif data == 'bulk_menu': await send_bulk_menu(query.message.chat_id, context, query=query)

        elif data.startswith('bulkprice_'):
            parts = data.replace('bulkprice_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1])
            _, saldo = cek_saldo_api()
            teks = (f"📦 <b>Beli Banyak — Rp {format_rupiah(harga)}</b>\n\n💰 Saldo: <b>Rp {format_rupiah(saldo) if saldo >= 0 else '?'}</b>\n\n"
                    f"• 5 = Rp {format_rupiah(harga * 5)}\n• 10 = Rp {format_rupiah(harga * 10)}\n• 20 = Rp {format_rupiah(harga * 20)}\n"
                    f"• 30 = Rp {format_rupiah(harga * 30)}\n• 40 = Rp {format_rupiah(harga * 40)}\n• 50 = Rp {format_rupiah(harga * 50)}\n"
                    f"• 100 = Rp {format_rupiah(harga * 100)}")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_bulk_qty(pid, harga)), parse_mode='HTML')

        elif data.startswith('bulkqty_'):
            parts = data.replace('bulkqty_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1]); qty = int(parts[2])
            total = harga * qty
            total_batches = (qty + BATCH_SIZE - 1) // BATCH_SIZE
            await query.edit_message_text(
                f"⏳ Membeli <b>{qty} nomor</b>...\n💵 Total: Rp {format_rupiah(total)}\n📦 {total_batches} batch × 10 nomor\n\n"
                f"<i>Mohon tunggu 15-60 detik.</i>", parse_mode='HTML')
            results, failed_count, err = await beli_banyak_api_async(pid, user_id, harga, qty)
            if results:
                ordered_list = [(oid, format_phone(n)) for oid, n in results]
                summary_teks = build_bulk_summary(ordered_list, set(), harga, TIMEOUT_SECONDS, failed_count)
                kb = [[InlineKeyboardButton("📦 Order Aktif", callback_data='aktif')],
                      [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                msg = await query.edit_message_text(summary_teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                batch_id = f"{user_id}_{int(time.time())}"
                asyncio.create_task(bulk_monitor_task(context, query.message.chat_id, batch_id, ordered_list, harga, msg.message_id, failed_count))
            else:
                kb = [[InlineKeyboardButton("🔙 Kembali", callback_data='bulk_menu')],
                      [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                await query.edit_message_text(f"❌ {err}", reply_markup=InlineKeyboardMarkup(kb))

        elif data.startswith('watch_'):
            parts = data.replace('watch_', '').split('_')
            pid = int(parts[0]); price = int(parts[1])
            if db_watchlist_add(user_id, pid, price):
                await query.edit_message_text(f"🔔 <b>Dipantau!</b>\n\nProduk Gojek Rp {format_rupiah(price)} akan dipantau.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]]), parse_mode='HTML')
            else: await query.answer("❌ Gagal tambah", show_alert=True)

        elif data == 'stok_habis':
            await query.answer("❌ Stok habis. Pilih harga lain.", show_alert=True)

        elif data.startswith('buy_'):
            parts = data.replace('buy_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1])
            await query.edit_message_text("⏳ Sedang membeli nomor...")
            order_id, nomor_raw = beli_nomor_api(pid, user_id, harga)
            if order_id:
                nomor_display = format_phone(nomor_raw)
                kb = [[InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
                      [InlineKeyboardButton("🔄 Cek OTP Sekarang", callback_data=f"checkotp_{order_id}")],
                      [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                teks = (f"🛒 <b>Nomor berhasil dibeli!</b>\n\n📱 Nomor: <code>{nomor_display}</code>\n🆔 ID: <code>{order_id}</code>\n\n"
                        f"📋 <b>Langkah selanjutnya:</b>\n1️⃣ Ketuk nomor untuk salin\n2️⃣ Masukkan ke aplikasi Gojek\n3️⃣ Ketuk <b>Cek OTP Sekarang</b>\n\n"
                        f"🔔 <i>OTP akan masuk instan via Webhook.</i>")
                await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display))
            else:
                await query.edit_message_text(f"❌ {nomor_raw}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')], [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

        elif data.startswith('checkotp_'):
            order_id = data.replace('checkotp_', '')
            cek = http_get(f"{BASE_URL}/orders/{order_id}")
            otp_s = None; status_s = None
            if cek.get("success"): otp_s = cek["data"].get("otp_code"); status_s = cek["data"].get("status")
            if otp_s:
                tmpl = get_template("otp_found", "🔑 <b>Kode OTP Ditemukan!</b>\n\n🔑 Kode: <code>{otp}</code>")
                teks = tmpl.replace("{otp}", otp_s)
                await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")], [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]), parse_mode='HTML')
            elif status_s in ["CANCELLED", "EXPIRED", "COMPLETED"]:
                try: await query.message.delete()
                except: pass
            else:
                kb = [[InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")], [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                teks = f"⏳ <b>Status:</b> Menunggu SMS masuk\n⏱️ Sisa waktu: <b>{TIMEOUT_MINUTES}:00</b>\n\n🔔 Bot akan otomatis mengubah pesan ini saat OTP masuk (via Webhook)."
                await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                pending_messages[order_id] = query.message.message_id

        elif data.startswith('cancel_'):
            order_id = data.replace('cancel_', '')
            try: await query.message.delete()
            except: pass
            hasil = batal_order_api(order_id)
            pending_messages.pop(order_id, None)
            if hasil["success"]:
                teks = (f"✅ <b>Berhasil Dibatalkan</b>\n\nNomor ID <code>{order_id}</code> telah dibatalkan.\n"
                        f"💰 Saldo dikembalikan: <b>Rp {format_rupiah(hasil.get('refund_amount', 0))}</b>\n\n"
                        f"💬 <i>Pesan ini akan hilang dalam 10 detik.</i>")
            else:
                teks = (f"❌ <b>Gagal Membatalkan</b>\n\nID: <code>{order_id}</code>\nAlasan: {hasil['message']}\n\n"
                        f"💬 <i>Pesan ini akan hilang dalam 10 detik.</i>")
            await send_auto_delete_message(context, query.message.chat_id, teks)

        elif data.startswith('finish_'):
            order_id = data.replace('finish_', '')
            try: await query.message.delete()
            except: pass
            hasil = selesai_order_api(order_id)
            pending_messages.pop(order_id, None)
            if hasil["success"]:
                teks = (f"✅ <b>Berhasil Diselesaikan</b>\n\nNomor ID <code>{order_id}</code> telah dilepas.\n\n"
                        f"💬 <i>Pesan ini akan hilang dalam 10 detik.</i>")
            else:
                teks = (f"❌ <b>Gagal Menyelesaikan</b>\n\nID: <code>{order_id}</code>\nAlasan: {hasil['message']}\n\n"
                        f"💬 <i>Pesan ini akan hilang dalam 10 detik.</i>")
            await send_auto_delete_message(context, query.message.chat_id, teks)

    except Exception as e:
        await log_error(context, f"Button error: {str(e)[:200]}", user_id)
        try: await query.edit_message_text("⚠️ Terjadi kesalahan. Coba lagi.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
        except: pass

# --- COMMAND HANDLERS ---
async def saldo_command(update, context):
    teks, saldo = cek_saldo_api()
    await update.message.reply_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
    if saldo >= 0 and saldo < LOW_BALANCE_THRESHOLD:
        await update.message.reply_text(f"⚠️ <b>Saldo Rendah!</b>\n\nSaldo: <b>Rp {format_rupiah(saldo)}</b>", parse_mode='HTML')

async def beli_command(update, context): await send_products_menu(update.effective_chat.id, context)
async def batal_command(update, context): await send_active_orders_menu(update.effective_chat.id, context, 'cancel')
async def selesai_command(update, context): await send_active_orders_menu(update.effective_chat.id, context, 'finish')
async def aktif_command(update, context): await send_active_orders_menu(update.effective_chat.id, context, 'cancel')

async def riwayat_command(update, context):
    rows = db_get_history(update.effective_user.id, 10, "all")
    if not rows: await update.message.reply_text("📜 Belum ada riwayat.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]])); return
    teks = "📜 <b>10 Transaksi Terakhir:</b>\n\n"
    for oid, phone, status, otp, created in rows:
        emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else "⏳")
        teks += f"{emoji} <code>{format_phone(phone)}</code>\n   {status} | OTP: {otp or '-'}\n   {created}\n\n"
    await update.message.reply_text(teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def stats_command(update, context):
    s = db_get_stats()
    if not s: await update.message.reply_text("⚠️ Gagal ambil statistik."); return
    teks = (f"📊 <b>Statistik Global Bot</b>\n\n📅 <b>Hari Ini:</b> {s['today'][0]} order | Rp {format_rupiah(s['today'][1])}\n"
            f"📆 <b>Bulan Ini:</b> {s['month'][0]} order | Rp {format_rupiah(s['month'][1])}\n"
            f"📈 <b>Total:</b> {s['total'][0]} order | Rp {format_rupiah(s['total'][1])}\n\n📋 <b>Status:</b>\n")
    for st, c in s['statuses'].items(): teks += f"   • {st}: {c}\n"
    await update.message.reply_text(teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def refund_command(update, context):
    if not is_admin(update.effective_user.id): await update.message.reply_text("🚫 Akses ditolak."); return
    r = db_get_refund_stats()
    if not r: await update.message.reply_text("⚠️ Gagal ambil data refund."); return
    teks = (f"💸 <b>Statistik Refund</b>\n\n📅 <b>Hari Ini:</b> {r['today'][0]} order | Rp {format_rupiah(r['today'][1])}\n"
            f"📆 <b>Bulan Ini:</b> {r['month'][0]} order | Rp {format_rupiah(r['month'][1])}\n"
            f"📈 <b>Total:</b> {r['total'][0]} order | Rp {format_rupiah(r['total'][1])}")
    await update.message.reply_text(teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def logs_command(update, context):
    if not is_admin(update.effective_user.id): await update.message.reply_text("🚫 Akses ditolak."); return
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT timestamp, level, message, user_id FROM logs ORDER BY id DESC LIMIT 20").fetchall()
        conn.close()
    except: rows = []
    if not rows:
        await update.message.reply_text("📜 Belum ada log error.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
        return
    teks = "📜 <b>20 Log Error Terakhir:</b>\n\n"
    for ts, lvl, msg, uid in rows:
        emoji = "🔴" if lvl == "ERROR" else "🟡"
        teks += f"{emoji} <code>{ts}</code>\n{msg[:200]}\n"
        if uid: teks += f"User: <code>{uid}</code>\n"
        teks += "\n"
    await update.message.reply_text(teks[:4000], parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def webhook_status_command(update, context):
    if not is_admin(update.effective_user.id): await update.message.reply_text("🚫 Akses ditolak."); return
    # Cek webhook info dari SMScode
    data = http_get(f"{BASE_URL}/webhook")
    if "error" in data:
        await update.message.reply_text(f"❌ {data['error']}"); return
    wh_url = data.get("data", {}).get("webhook_url", "-")
    has_secret = bool(SMScode_WEBHOOK_SECRET)
    teks = (f"🔔 <b>Status Webhook</b>\n\n"
            f"🌐 <b>URL Terdaftar:</b>\n<code>{wh_url or '(belum terdaftar)'}</code>\n\n"
            f"🔑 <b>Secret di Railway:</b> {'✅ Ada' if has_secret else '❌ Belum ada'}\n\n"
            f"📡 <b>Endpoint Lokal:</b>\n<code>/webhook/smscode</code>\n\n"
            f"💡 <i>Jika secret belum ada, copy dari log Railway saat startup lalu set di Variables Railway.</i>")
    await update.message.reply_text(teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def setwelcome_command(update, context):
    if not is_admin(update.effective_user.id): await update.message.reply_text("🚫 Akses ditolak."); return
    if not context.args: await update.message.reply_text("⚠️ Format: /setwelcome Teks baru"); return
    teks = " ".join(context.args)
    if set_template("welcome", teks): await update.message.reply_text(f"✅ Sambutan diubah:\n\n{teks}")
    else: await update.message.reply_text("⚠️ Gagal simpan.")

async def setotp_command(update, context):
    if not is_admin(update.effective_user.id): await update.message.reply_text("🚫 Akses ditolak."); return
    if not context.args: await update.message.reply_text("⚠️ Format: /setotp Teks dengan {otp}"); return
    teks = " ".join(context.args)
    if set_template("otp_found", teks): await update.message.reply_text(f"✅ Template OTP diubah.\nPlaceholder: {{otp}}, {{nomor}}, {{order_id}}")
    else: await update.message.reply_text("⚠️ Gagal simpan.")

async def backup_command(update, context):
    if not is_admin(update.effective_user.id): await update.message.reply_text("🚫 Akses ditolak."); return
    try:
        if os.path.exists(DB_PATH):
            with open(DB_PATH, 'rb') as f:
                await update.message.reply_document(document=f, filename=f"backup_{time.strftime('%Y%m%d_%H%M')}.db", caption="🗄️ Backup Manual Database")
    except Exception as e: await update.message.reply_text(f"⚠️ Gagal: {str(e)[:100]}")

# --- MAIN ---
async def post_init(application):
    global telegram_loop, telegram_bot_instance
    telegram_loop = asyncio.get_running_loop()
    telegram_bot_instance = application
    commands = [
        BotCommand("start", "🚀 Menu utama"), BotCommand("beli", "🛒 Beli nomor Gojek"),
        BotCommand("saldo", "💰 Cek saldo"), BotCommand("aktif", "📦 Nomor aktif"),
        BotCommand("riwayat", "📜 Riwayat transaksi"), BotCommand("stats", "📊 Statistik"),
        BotCommand("refund", "💸 Statistik refund"),
        BotCommand("batal", "❌ Batalkan nomor"), BotCommand("selesai", "✅ Selesaikan nomor"),
        BotCommand("help", "❓ Bantuan"),
        BotCommand("logs", "📜 Log error (admin)"),
        BotCommand("webhook_status", "🔔 Status webhook"),
    ]
    try: await application.bot.set_my_commands(commands)
    except Exception as e: print(f"Set commands error: {e}")
    asyncio.create_task(auto_backup_task(application))
    asyncio.create_task(restock_checker_task(application))
    # Registrasi webhook URL ke SMScode
    asyncio.create_task(register_webhook_to_smscode())

if __name__ == '__main__':
    init_db()
    # Start Flask (webhook receiver) di thread terpisah
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"[Flask] Webhook server listening on port {PORT}")
    print(f"[Flask] Endpoint: /webhook/smscode")

    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    for cmd, fn in [("start", start), ("help", help_command), ("saldo", saldo_command),
                    ("beli", beli_command), ("batal", batal_command), ("selesai", selesai_command),
                    ("aktif", aktif_command), ("riwayat", riwayat_command), ("stats", stats_command),
                    ("refund", refund_command), ("logs", logs_command),
                    ("webhook_status", webhook_status_command),
                    ("setwelcome", setwelcome_command), ("setotp", setotp_command), ("backup", backup_command)]:
        application.add_handler(CommandHandler(cmd, fn))
    application.add_handler(CallbackQueryHandler(button_handler))
    print("Bot berjalan...")
    application.run_polling()
