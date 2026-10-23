import os
import json
import re
import time
import sqlite3
import urllib.request
import urllib.error
import asyncio
import logging
import threading
from collections import defaultdict
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# --- KONFIGURASI ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597")
SMScode_API_KEY = os.environ.get("SMScode_API_KEY")
LOW_BALANCE_THRESHOLD = 2000
BOT_VERSION = "3.2 Production"
START_TIME = time.time()
MAX_BULK = 100
AUTO_DELETE_SECONDS = 10
BATCH_SIZE = 10
TIMEOUT_MINUTES = 25
TIMEOUT_SECONDS = TIMEOUT_MINUTES * 60
PRODUCT_CACHE_TTL = 30
SALDO_CACHE_TTL = 30
RATE_LIMIT_WINDOW = 10
RATE_LIMIT_MAX = 8
PROCESSING_LOCK_SECONDS = 10
CONFIRM_THRESHOLD = 10000        # Konfirmasi untuk beli 1 nomor >= Rp 10.000
BULK_CONFIRM_THRESHOLD = 50000   # Konfirmasi untuk bulk total >= Rp 50.000
LOG_RETENTION_DAYS = 30
LOG_MAX_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

BASE_URL = "https://api.smscode.gg/v1"
HEADERS = {
    "Authorization": f"Bearer {SMScode_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

# --- LOGGING ---
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# --- STATE ---
pending_messages = {}
_product_cache = {"data": None, "ts": 0}
_saldo_cache = {"value": -1, "ts": 0}
_rate_bucket = defaultdict(list)
_processing_users = {}
_db_conn = None
_db_lock = threading.Lock()

# --- ENV VALIDATION ---
def validate_env():
    """Fail-fast jika env wajib kosong — mencegah bot jalan tanpa bisa transaksi."""
    missing = []
    if not TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
    if not SMScode_API_KEY: missing.append("SMScode_API_KEY")
    if missing:
        msg = f"Missing env vars: {', '.join(missing)}"
        print(f"❌ FATAL: {msg}")
        try: logger.error(f"FATAL: {msg}")
        except: pass
        exit(1)
    print(f"✅ Environment OK (Admin ID: {ADMIN_ID}, API Key: {'*' * 8}{SMScode_API_KEY[-4:]})")

# --- HELPER ---
def format_rupiah(n):
    try: return f"{int(n):,}".replace(",", ".")
    except: return str(n)

def format_countdown(seconds):
    if seconds < 0: seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0: return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def progress_bar(current, total, width=12):
    if total <= 0: return "░" * width
    filled = int(width * current / total)
    return "█" * filled + "░" * (width - filled)

def timer_emoji(seconds):
    if seconds > 900: return "🟢"
    if seconds > 300: return "🟡"
    return "🔴"

def check_rate_limit(uid):
    now = time.time()
    calls = _rate_bucket[uid]
    while calls and now - calls[0] > RATE_LIMIT_WINDOW:
        calls.pop(0)
    if len(calls) >= RATE_LIMIT_MAX:
        return False
    calls.append(now)
    return True

def try_acquire_lock(uid):
    now = time.time()
    last = _processing_users.get(uid, 0)
    if now - last < PROCESSING_LOCK_SECONDS:
        return False
    _processing_users[uid] = now
    return True

def release_lock(uid):
    _processing_users.pop(uid, None)

async def send_auto_delete_message(context, chat_id, text, parse_mode='HTML', delete_after=AUTO_DELETE_SECONDS, reply_markup=None):
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        asyncio.create_task(auto_delete_task(context, chat_id, msg.message_id, delete_after))
        return msg
    except Exception as e:
        logger.error(f"send_auto_delete error: {e}")
        return None

async def auto_delete_task(context, chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try: await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except: pass

# --- DATABASE ---
def get_db():
    global _db_conn
    with _db_lock:
        if _db_conn is None:
            _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
            _db_conn.execute("PRAGMA journal_mode=WAL")
            _db_conn.execute("PRAGMA synchronous=NORMAL")
            _db_conn.execute("PRAGMA busy_timeout=5000")
        return _db_conn

def init_db():
    conn = get_db()
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
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_name TEXT, username TEXT,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        onboarding_done INTEGER DEFAULT 0
    )''')
    conn.commit()
    logger.info("Database initialized")

def db_touch_user(user_id, first_name, username=None):
    try:
        conn = get_db()
        row = conn.execute("SELECT onboarding_done FROM users WHERE user_id=?", (user_id,)).fetchone()
        is_new = row is None
        if is_new:
            conn.execute("INSERT INTO users (user_id, first_name, username) VALUES (?, ?, ?)",
                         (user_id, first_name or "", username or ""))
        else:
            conn.execute("UPDATE users SET last_seen=CURRENT_TIMESTAMP, first_name=?, username=? WHERE user_id=?",
                         (first_name or "", username or "", user_id))
        conn.commit()
        return is_new, (row[0] == 0 if row else True)
    except Exception as e:
        logger.error(f"touch_user error: {e}")
        return False, False

def db_mark_onboarded(user_id):
    try:
        conn = get_db()
        conn.execute("UPDATE users SET onboarding_done=1 WHERE user_id=?", (user_id,))
        conn.commit()
    except: pass

def db_save_log(level, message, user_id=None):
    try:
        conn = get_db()
        conn.execute("INSERT INTO logs (level, message, user_id) VALUES (?, ?, ?)", (level, message[:500], user_id))
        conn.commit()
    except: pass

def db_save_order(order_id, user_id, phone, product_id, price):
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO orders (order_id, user_id, phone, product_id, price, status) VALUES (?, ?, ?, ?, ?, 'ACTIVE')",
                     (str(order_id), user_id, phone, product_id, price))
        conn.commit()
    except Exception as e: logger.error(f"save_order: {e}")

def db_update_order(order_id, status, otp=None):
    try:
        conn = get_db()
        if otp: conn.execute("UPDATE orders SET status=?, otp=? WHERE order_id=?", (status, otp, str(order_id)))
        else: conn.execute("UPDATE orders SET status=? WHERE order_id=?", (status, str(order_id)))
        conn.commit()
    except Exception as e: logger.error(f"update_order: {e}")

def db_get_history(user_id, limit=10, filter_type="all"):
    try:
        conn = get_db()
        if filter_type == "today":
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours'), price FROM orders WHERE user_id=? AND DATE(created_at, '+7 hours') = DATE('now', '+7 hours') ORDER BY created_at DESC LIMIT ?"
        elif filter_type == "week":
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours'), price FROM orders WHERE user_id=? AND datetime(created_at, '+7 hours') >= datetime('now', '-7 days', '+7 hours') ORDER BY created_at DESC LIMIT ?"
        elif filter_type == "month":
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours'), price FROM orders WHERE user_id=? AND strftime('%Y-%m', datetime(created_at, '+7 hours')) = strftime('%Y-%m', 'now', '+7 hours') ORDER BY created_at DESC LIMIT ?"
        else:
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours'), price FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?"
        return conn.execute(q, (user_id, limit)).fetchall()
    except Exception as e:
        logger.error(f"get_history: {e}")
        return []

def db_get_last_successful(user_id):
    try:
        conn = get_db()
        return conn.execute(
            "SELECT product_id, price, phone FROM orders WHERE user_id=? AND status IN ('COMPLETED','OTP_RECEIVED') ORDER BY created_at DESC LIMIT 1",
            (user_id,)).fetchone()
    except: return None

def db_get_user_stats(user_id):
    try:
        conn = get_db()
        total = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=?", (user_id,)).fetchone()
        completed = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='COMPLETED'", (user_id,)).fetchone()[0]
        cancelled = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='CANCELLED'", (user_id,)).fetchone()[0]
        return {"total": total[0], "spent": total[1], "completed": completed, "cancelled": cancelled}
    except: return {"total": 0, "spent": 0, "completed": 0, "cancelled": 0}

def db_get_stats():
    try:
        conn = get_db()
        today = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE DATE(created_at, '+7 hours') = DATE('now', '+7 hours')").fetchone()
        month = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE strftime('%Y-%m', datetime(created_at, '+7 hours')) = strftime('%Y-%m', 'now', '+7 hours')").fetchone()
        total = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders").fetchone()
        statuses = dict(conn.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall())
        return {"today": today, "month": month, "total": total, "statuses": statuses}
    except: return None

def db_get_refund_stats(user_id=None):
    try:
        conn = get_db()
        today = conn.execute("SELECT COUNT(*), COALESCE(SUM(refund_amount),0) FROM orders WHERE refund_amount > 0 AND DATE(refunded_at, '+7 hours') = DATE('now', '+7 hours')").fetchone()
        month = conn.execute("SELECT COUNT(*), COALESCE(SUM(refund_amount),0) FROM orders WHERE refund_amount > 0 AND strftime('%Y-%m', datetime(refunded_at, '+7 hours')) = strftime('%Y-%m', 'now', '+7 hours')").fetchone()
        total = conn.execute("SELECT COUNT(*), COALESCE(SUM(refund_amount),0) FROM orders WHERE refund_amount > 0").fetchone()
        return {"today": today, "month": month, "total": total}
    except: return None

def get_template(key, default=""):
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM templates WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    except: return default

def set_template(key, value):
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO templates (key, value) VALUES (?, ?)", (key, value))
        conn.commit(); return True
    except: return False

def db_watchlist_add(user_id, product_id, price):
    try:
        conn = get_db()
        conn.execute("INSERT INTO watchlist (user_id, product_id, price) VALUES (?, ?, ?)", (user_id, product_id, price))
        conn.commit(); return True
    except: return False

def db_watchlist_remove_by_id(wid):
    try:
        conn = get_db()
        conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
        conn.commit(); return True
    except: return False

def db_watchlist_get(user_id):
    try:
        conn = get_db()
        return conn.execute("SELECT id, product_id, price FROM watchlist WHERE user_id=?", (user_id,)).fetchall()
    except: return []

# --- HTTP ---
def http_get(url, retry=1):
    for attempt in range(retry + 1):
        req = urllib.request.Request(url, headers=HEADERS, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
            except: return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            if attempt < retry:
                time.sleep(1); continue
            return {"error": str(e)}
    return {"error": "Unknown"}

def http_post(url, payload, retry=1):
    data = json.dumps(payload).encode('utf-8')
    for attempt in range(retry + 1):
        req = urllib.request.Request(url, data=data, headers=HEADERS, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
            except: return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            if attempt < retry:
                time.sleep(1); continue
            return {"error": str(e)}
    return {"error": "Unknown"}

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
    if "bad gateway" in e or "502" in e: return "🔧 Server SMScode bermasalah (502)."
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
    logger.error(f"User {user_id}: {error_msg[:400]}")
    try:
        teks = f"⚠️ <b>Error Log</b>\n\n"
        if user_id: teks += f"User: <code>{user_id}</code>\n"
        teks += f"Pesan: <code>{error_msg[:400]}</code>\nWaktu: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        await context.bot.send_message(chat_id=ADMIN_ID, text=teks, parse_mode='HTML')
    except: pass

# --- SALDO CACHE ---
def invalidate_saldo_cache():
    global _saldo_cache
    _saldo_cache["value"] = -1
    _saldo_cache["ts"] = 0

def cek_saldo_api():
    global _saldo_cache
    now = time.time()
    if _saldo_cache["value"] >= 0 and (now - _saldo_cache["ts"]) < SALDO_CACHE_TTL:
        return f"💰 Saldo Saya: <b>Rp {format_rupiah(_saldo_cache['value'])}</b>", _saldo_cache["value"]
    if not SMScode_API_KEY: return "❌ API Key belum diatur.", -1
    data = http_get(f"{BASE_URL}/balance")
    if "error" in data: return translate_error(data["error"]), -1
    if data.get("success"):
        saldo = data["data"].get("balance", 0)
        _saldo_cache["value"] = saldo
        _saldo_cache["ts"] = now
        return f"💰 Saldo Saya: <b>Rp {format_rupiah(saldo)}</b>", saldo
    return "⚠️ Gagal ambil saldo.", -1

def get_balance():
    data = http_get(f"{BASE_URL}/balance")
    if data.get("success"): return data["data"].get("balance", 0)
    return -1

# --- API SMScode ---
def get_gojek_products(force_refresh=False):
    now = time.time()
    if not force_refresh and _product_cache["data"] is not None and (now - _product_cache["ts"]) < PRODUCT_CACHE_TTL:
        return _product_cache["data"], None
    data = http_get(f"{BASE_URL}/catalog/products?country_id=7")
    if "error" in data:
        if _product_cache["data"] is not None: return _product_cache["data"], None
        return None, translate_error(data["error"])
    if not data.get("success"):
        if _product_cache["data"] is not None: return _product_cache["data"], None
        return None, "Gagal ambil data."
    products = data.get("data", [])
    if not isinstance(products, list): return None, "Format data tidak valid."
    gojek = [p for p in products if "Gojek" in p.get("name", "")]
    gojek.sort(key=lambda x: x.get('price', 999999))
    _product_cache["data"] = gojek
    _product_cache["ts"] = now
    return gojek, None

def get_active_orders():
    data = http_get(f"{BASE_URL}/orders/active")
    if "error" in data: return None, translate_error(data["error"])
    if not data.get("success"): return None, "Gagal ambil order."
    orders = data.get("data", [])
    if not isinstance(orders, list): return None, "Format data tidak valid."
    return orders, None

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
        invalidate_saldo_cache()
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
            if batch_num == 1: return None, 0, translate_error(data["error"])
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
            if len(orders) == 0: break
        else: break
        if remaining > 0: await asyncio.sleep(3)
    if not all_results: return None, 0, "⚠️ Tidak ada order yang berhasil dibuat."
    invalidate_saldo_cache()
    return all_results, total_failed, None

def batal_order_api(order_id):
    data = http_post(f"{BASE_URL}/orders/cancel", {"id": int(order_id)})
    if "error" in data: return {"success": False, "message": translate_error(data["error"]), "refund_amount": 0}
    if data.get("success"):
        refund_amount = 0
        try:
            conn = get_db()
            row = conn.execute("SELECT price FROM orders WHERE order_id=?", (str(order_id),)).fetchone()
            refund_amount = row[0] if row else 0
            conn.execute("UPDATE orders SET status='CANCELLED', refund_amount=?, refunded_at=CURRENT_TIMESTAMP WHERE order_id=?",
                         (refund_amount, str(order_id)))
            conn.commit()
        except Exception as e:
            logger.error(f"batal DB: {e}")
            db_update_order(order_id, "CANCELLED")
        invalidate_saldo_cache()
        return {"success": True, "message": "✅ Nomor berhasil dibatalkan & saldo dikembalikan.", "refund_amount": refund_amount}
    return {"success": False, "message": "⚠️ Gagal membatalkan nomor.", "refund_amount": 0}

def selesai_order_api(order_id):
    data = http_post(f"{BASE_URL}/orders/finish", {"id": int(order_id)})
    if "error" in data: return {"success": False, "message": translate_error(data["error"])}
    if data.get("success"):
        db_update_order(order_id, "COMPLETED")
        invalidate_saldo_cache()
        return {"success": True, "message": "✅ Nomor berhasil diselesaikan."}
    return {"success": False, "message": "⚠️ Gagal menyelesaikan nomor."}

# ============================================================
# === CLEANUP: LOG LAMA + ROTASI FILE LOG ===
# ============================================================
def run_housekeeping():
    """Hapus log >30 hari dari DB, rotasi bot.log jika >5MB."""
    try:
        conn = get_db()
        deleted = conn.execute(
            f"DELETE FROM logs WHERE timestamp < datetime('now', '-{LOG_RETENTION_DAYS} days')"
        ).rowcount
        conn.commit()
        if deleted > 0:
            logger.info(f"Housekeeping: {deleted} log lama dihapus")
    except Exception as e:
        logger.error(f"cleanup DB error: {e}")
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_SIZE_BYTES:
            rotated = LOG_PATH.replace(".log", f"_{int(time.time())}.log")
            os.rename(LOG_PATH, rotated)
            for h in logger.handlers[:]:
                logger.removeHandler(h)
                h.close()
            new_h = logging.FileHandler(LOG_PATH)
            new_h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
            logger.addHandler(new_h)
            logger.info(f"Log rotated → {os.path.basename(rotated)}")
    except Exception as e:
        print(f"cleanup log file error: {e}")

# ============================================================
# === BULK SUMMARY BUILDER ===
# ============================================================
def build_bulk_summary(ordered_list, received_set, price_per_unit, remaining_seconds, failed_count=0):
    total = len(ordered_list)
    jumlah_dapat = len(received_set)
    timer_str = format_countdown(remaining_seconds)
    bar = progress_bar(jumlah_dapat, total, 12)
    pct = int(100 * jumlah_dapat / total) if total else 0
    t_emoji = timer_emoji(remaining_seconds)

    text = f"🛒 <b>Berhasil Beli {total} Nomor!</b>\n"
    if failed_count > 0:
        text += f"⚠️ Gagal: <b>{failed_count}</b> nomor\n"
    text += f"\n📊 <b>Progress OTP</b>\n"
    text += f"<code>{bar}</code> {pct}%\n"
    text += f"✅ <b>{jumlah_dapat}/{total}</b> menerima OTP\n"
    text += f"{t_emoji} Sisa waktu: <b>{timer_str}</b>\n"
    text += "━━━━━━━━━━━━━━━━━━━━\n"

    received_items = [(oid, p) for oid, p in ordered_list if oid in received_set]
    pending_items = [(oid, p) for oid, p in ordered_list if oid not in received_set]

    if received_items:
        text += "✅ <b>OTP Diterima:</b>\n"
        for oid, phone in received_items[:15]:
            text += f"  🔑 <code>{phone}</code>\n"
        if len(received_items) > 15:
            text += f"  <i>… dan {len(received_items) - 15} lainnya</i>\n"
        text += "\n"
    if pending_items:
        text += "⏳ <b>Menunggu OTP:</b>\n"
        for oid, phone in pending_items[:20]:
            text += f"  📱 <code>{phone}</code>\n"
        if len(pending_items) > 20:
            text += f"  <i>… dan {len(pending_items) - 20} lainnya</i>\n"
        text += "\n"

    text += (f"━━━━━━━━━━━━━━━━━━━━\n"
             f"💰 Total: <b>Rp {format_rupiah(price_per_unit * total)}</b>\n\n"
             f"📋 <b>Cara pakai:</b>\n"
             f"1️⃣ Ketuk nomor untuk salin otomatis\n"
             f"2️⃣ Tempel ke aplikasi Gojek\n"
             f"3️⃣ OTP dikirim bot otomatis\n\n"
             f"⏱️ Timer berhenti di {TIMEOUT_MINUTES} menit.")
    return text

# ============================================================
# === SINGLE ORDER POLLING ===
# ============================================================
async def auto_poll_otp(chat_id, context, order_id, nomor_display):
    total = TIMEOUT_SECONDS
    elapsed = 0
    iteration = 0
    while elapsed < total:
        if iteration < 10: sleep_s = 2
        elif iteration < 40: sleep_s = 5
        else: sleep_s = 10
        await asyncio.sleep(sleep_s)
        elapsed += sleep_s
        iteration += 1
        remaining = total - elapsed
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
            h = remaining // 3600
            m = (remaining % 3600) // 60
            s = remaining % 60
            t_emoji = timer_emoji(remaining)
            try:
                kb = [[InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
                      [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                    text=f"⏳ <b>Status:</b> Menunggu SMS masuk\n{t_emoji} Sisa waktu: <b>{h:02d}:{m:02d}:{s:02d}</b>\n\nBot akan otomatis mengubah pesan ini menjadi kode OTP saat masuk.",
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
            except: pass
    cek = http_get(f"{BASE_URL}/orders/{order_id}")
    otp_akhir = None
    if cek.get("success"): otp_akhir = cek["data"].get("otp_code")
    msg_id = pending_messages.get(order_id)
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
# === BULK MONITOR ===
# ============================================================
async def bulk_monitor_task(context, chat_id, batch_id, ordered_list, price_per_unit, summary_msg_id, failed_count=0):
    total = len(ordered_list)
    pending = {oid: phone for oid, phone in ordered_list}
    received_set = set()
    start_time = time.time()
    timeout_seconds = TIMEOUT_SECONDS

    async def update_summary():
        try:
            elapsed = int(time.time() - start_time)
            remaining = max(0, timeout_seconds - elapsed)
            teks = build_bulk_summary(ordered_list, received_set, price_per_unit, remaining, failed_count)
            kb = [[InlineKeyboardButton("📦 Order Aktif", callback_data='aktif')],
                  [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
            await context.bot.edit_message_text(chat_id=chat_id, message_id=summary_msg_id, text=teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
        except: pass

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
                        text=(f"🔑 <b>OTP Diterima!</b>\n\n"
                              f"📱 Nomor: <code>{phone}</code>\n"
                              f"🔑 Kode: <code>{otp}</code>\n"
                              f"🆔 ID: <code>{oid}</code>\n\n"
                              f"📊 Progress: <b>{len(received_set)}/{total}</b>"),
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{oid}")]]),
                        parse_mode='HTML')
                except: pass
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
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]),
                parse_mode='HTML')
        except: pass
        return
    success = 0; failed = 0; refund_total = 0; failed_oids = []
    for o in to_cancel:
        oid = str(o.get("id"))
        hasil = batal_order_api(oid)
        if hasil["success"]:
            success += 1; refund_total += hasil.get("refund_amount", 0)
        else:
            failed += 1; failed_oids.append(oid)
        await asyncio.sleep(0.3)
    teks = (f"✅ <b>Batalkan Massal Selesai</b>\n\n"
            f"📊 Total diperiksa: <b>{len(orders)}</b>\n"
            f"🗑️ Dibatalkan: <b>{success}</b>\n"
            f"⏭️ Dilewati (sudah OTP): <b>{skipped}</b>\n"
            f"❌ Gagal: <b>{failed}</b>\n\n"
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
    """Cek restock + housekeeping (cleanup rate bucket, logs, log rotation)."""
    while True:
        await asyncio.sleep(5 * 60)
        try:
            # --- Housekeeping (logs + file rotation) ---
            run_housekeeping()

            # --- Cleanup memory ---
            now = time.time()
            for uid in list(_rate_bucket.keys()):
                _rate_bucket[uid] = [t for t in _rate_bucket[uid] if now - t < RATE_LIMIT_WINDOW]
                if not _rate_bucket[uid]: _rate_bucket.pop(uid, None)
            for uid in list(_processing_users.keys()):
                if now - _processing_users[uid] > 60: _processing_users.pop(uid, None)

            # --- Cek restock ---
            conn = get_db()
            rows = conn.execute("SELECT id, user_id, product_id, price FROM watchlist").fetchall()
            if not rows: continue
            products, err = get_gojek_products(force_refresh=True)
            if err or not products: continue
            for wid, uid, pid, price in rows:
                for p in products:
                    if p.get('id') == pid and p.get('available', 0) > 0:
                        try:
                            await context.bot.send_message(chat_id=uid,
                                text=f"🎉 <b>Stok Kembali Tersedia!</b>\n\n📦 Gojek\n💵 Harga: Rp {format_rupiah(price)}\n📊 Stok: {p.get('available')}\n\nSegera beli sebelum habis!",
                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Beli Sekarang", callback_data='beli_nomor')]]),
                                parse_mode='HTML')
                        except: pass
                        db_watchlist_remove_by_id(wid)
                        break
        except Exception as e:
            logger.error(f"Restock task error: {e}")

async def recover_pending_orders(application):
    """Recovery order aktif saat bot restart."""
    await asyncio.sleep(3)
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT order_id, user_id, phone FROM orders "
            "WHERE status='ACTIVE' AND datetime(created_at, '+7 hours') >= datetime('now', '-25 minutes', '+7 hours')"
        ).fetchall()
        if not rows:
            logger.info("Recovery: tidak ada order tertunda.")
            return
        logger.info(f"Recovery: {len(rows)} order aktif ditemukan.")
        for oid, uid, phone in rows:
            try:
                cek = http_get(f"{BASE_URL}/orders/{oid}")
                if not cek.get("success"): continue
                od = cek["data"]; status = od.get("status"); otp = od.get("otp_code")
                nomor_display = format_phone(phone)
                if status == "ACTIVE":
                    await application.bot.send_message(
                        chat_id=uid,
                        text=(f"🔄 <b>Bot Recovery</b>\n\n"
                              f"Order <code>{oid}</code> (<code>{nomor_display}</code>) masih aktif.\n"
                              f"Bot kembali memantau OTP untuk Anda otomatis."),
                        parse_mode='HTML')
                    asyncio.create_task(auto_poll_otp(uid, application, oid, nomor_display))
                elif status == "OTP_RECEIVED" and otp:
                    db_update_order(oid, "OTP_RECEIVED", otp)
                    await application.bot.send_message(
                        chat_id=uid,
                        text=(f"🔄 <b>Recovery OTP</b>\n\n"
                              f"📱 Nomor: <code>{nomor_display}</code>\n"
                              f"🔑 OTP: <code>{otp}</code>"),
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{oid}")]]))
                elif status in ["CANCELLED", "EXPIRED", "COMPLETED"]:
                    db_update_order(oid, status)
            except Exception as e:
                logger.error(f"Recovery order {oid}: {e}")
    except Exception as e:
        logger.error(f"recover_pending_orders: {e}")

# ============================================================
# === MENU KEYBOARDS ===
# ============================================================
def kb_menu_utama(user_id=None):
    rows = []
    if user_id:
        last = db_get_last_successful(user_id)
        if last:
            pid, price, _ = last
            rows.append([InlineKeyboardButton(f"🔁 Beli Lagi Rp {format_rupiah(price)}", callback_data=f"buy_{pid}_{price}")])
    rows += [
        [InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')],
        [InlineKeyboardButton("💼 Akun Saya", callback_data='akun_saya'),
         InlineKeyboardButton("⭐ Favorit", callback_data='favorit')],
        [InlineKeyboardButton("📜 Riwayat", callback_data='riwayat_menu'),
         InlineKeyboardButton("ℹ️ Info & Bantuan", callback_data='info_bantuan')]
    ]
    return rows

def kb_akun():
    return [
        [InlineKeyboardButton("💰 Saldo", callback_data='cek_saldo'),
         InlineKeyboardButton("📊 Statistik Saya", callback_data='stats_saya')],
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
        [InlineKeyboardButton("📅 Hari Ini", callback_data='riwayat_today'),
         InlineKeyboardButton("📆 7 Hari", callback_data='riwayat_week')],
        [InlineKeyboardButton("🗓️ Bulan Ini", callback_data='riwayat_month'),
         InlineKeyboardButton("📋 Semua", callback_data='riwayat_all')],
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
        [InlineKeyboardButton("📦 5 Nomor", callback_data=f"bulkqty_{pid}_{harga}_5"),
         InlineKeyboardButton("📦 10 Nomor", callback_data=f"bulkqty_{pid}_{harga}_10")],
        [InlineKeyboardButton("📦 20 Nomor", callback_data=f"bulkqty_{pid}_{harga}_20"),
         InlineKeyboardButton("📦 30 Nomor", callback_data=f"bulkqty_{pid}_{harga}_30")],
        [InlineKeyboardButton("📦 40 Nomor", callback_data=f"bulkqty_{pid}_{harga}_40"),
         InlineKeyboardButton("📦 50 Nomor", callback_data=f"bulkqty_{pid}_{harga}_50")],
        [InlineKeyboardButton("📦 100 Nomor", callback_data=f"bulkqty_{pid}_{harga}_100")],
        [InlineKeyboardButton("🔙 Kembali", callback_data='bulk_menu')]
    ]

# ============================================================
# === BUY EXECUTORS (dipisahkan agar reusable dengan konfirmasi) ===
# ============================================================
async def _execute_buy(query, context, user_id, pid, harga):
    """Eksekusi beli 1 nomor (dipanggil langsung atau via konfirmasi)."""
    await query.edit_message_text("⏳ <b>Sedang membeli nomor…</b>\n\n<i>Mohon tunggu sebentar.</i>", parse_mode='HTML')
    order_id, nomor_raw = beli_nomor_api(pid, user_id, harga)
    if order_id:
        nomor_display = format_phone(nomor_raw)
        kb = [[InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
              [InlineKeyboardButton("🔄 Cek OTP Sekarang", callback_data=f"checkotp_{order_id}")],
              [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
        teks = (f"🛒 <b>Nomor berhasil dibeli!</b>\n\n"
                f"📱 Nomor: <code>{nomor_display}</code>\n"
                f"🆔 ID: <code>{order_id}</code>\n\n"
                f"📋 <b>Langkah selanjutnya:</b>\n"
                f"1️⃣ <i>Ketuk nomor untuk salin otomatis</i>\n"
                f"2️⃣ Tempel ke aplikasi Gojek\n"
                f"3️⃣ Bot akan kirim OTP otomatis saat masuk\n\n"
                f"⚠️ OTP tidak masuk dalam {TIMEOUT_MINUTES} menit? Nomor auto-cancel + refund.")
        await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display))
    else:
        kb = [[InlineKeyboardButton("🔄 Coba Lagi", callback_data='beli_nomor')],
              [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
        await query.edit_message_text(f"❌ {nomor_raw}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

async def _execute_bulk(query, context, user_id, pid, harga, qty):
    """Eksekusi beli banyak (dipanggil langsung atau via konfirmasi)."""
    total = harga * qty
    total_batches = (qty + BATCH_SIZE - 1) // BATCH_SIZE
    await query.edit_message_text(
        f"⏳ <b>Membeli {qty} nomor…</b>\n\n"
        f"💵 Total: <b>Rp {format_rupiah(total)}</b>\n"
        f"📦 Diproses dalam <b>{total_batches} batch</b> × 10 nomor\n\n"
        f"<i>Mohon tunggu 15–60 detik. Progress akan muncul di pesan berikutnya.</i>", parse_mode='HTML')
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
        kb = [[InlineKeyboardButton("🔄 Coba Lagi", callback_data='bulk_menu')],
              [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
        await query.edit_message_text(f"❌ {err}", reply_markup=InlineKeyboardMarkup(kb))

# ============================================================
# === TAMPILAN MENU ===
# ============================================================
async def send_menu_utama(query, context, user_id=None):
    welcome = get_template("welcome", "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>")
    user = query.from_user.first_name or "Bos"
    uid = user_id or query.from_user.id
    _, s = cek_saldo_api()
    stats = db_get_user_stats(uid)
    teks = (f"{welcome}\n\n👋 Halo, <b>{user}</b>!\n\n━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Saldo:</b> Rp {format_rupiah(s) if s >= 0 else '-'}\n"
            f"📊 <b>Total Order:</b> {stats['total']} | ✅ {stats['completed']} selesai\n"
            f"⏱️ <b>Uptime:</b> {get_uptime()}\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💡 <i>Bot membatalkan & refund otomatis jika OTP tidak masuk dalam {TIMEOUT_MINUTES} menit.</i>")
    await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_menu_utama(uid)), parse_mode='HTML')

async def send_products_menu(chat_id, context, query=None, force_refresh=False):
    products, err = get_gojek_products(force_refresh=force_refresh)
    if err:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Coba Lagi", callback_data='beli_nomor')],
                                    [InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]])
        if query: await query.edit_message_text(f"❌ {err}", reply_markup=kb)
        else: await context.bot.send_message(chat_id, f"❌ {err}", reply_markup=kb)
        return
    if not products:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data='beli_nomor')],
                                    [InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]])
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
            keyboard.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(harga)}  •  📦 {stok} stok", callback_data=f"buy_{pid}_{harga}")])
        else:
            keyboard.append([InlineKeyboardButton(f"❌ Rp {format_rupiah(harga)} (Habis) — 🔔 Pantau", callback_data=f"watch_{pid}_{harga}")])
        if len(keyboard) >= 8: break
    keyboard.append([InlineKeyboardButton("📦 Beli Banyak (Bulk)", callback_data='bulk_menu'),
                     InlineKeyboardButton("🔄 Refresh", callback_data='refresh_beli')])
    keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')])
    text = ("🛒 <b>Pilih Harga Nomor Gojek</b>\n\n"
            "⚡ <i>Ketuk harga untuk beli 1 nomor.</i>\n"
            "🔔 <i>Ketuk saat habis untuk notifikasi restock.</i>\n"
            "📦 <i>Tombol Bulk untuk beli banyak sekaligus.</i>")
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
            keyboard.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(harga)}  •  📦 {stok}", callback_data=f"bulkprice_{pid}_{harga}")])
        if len(keyboard) >= 8: break
    keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')])
    text = ("📦 <b>Beli Banyak (Bulk)</b>\n\n"
            "Pilih harga → pilih jumlah: <b>5 / 10 / 20 / 30 / 40 / 50 / 100</b>\n\n"
            f"⚙️ <b>Yang bot lakukan otomatis:</b>\n"
            f"• Memecah pembelian jadi batch 10 nomor\n"
            f"• Menampilkan progress bar live\n"
            f"• Mengirim OTP seketika saat masuk\n"
            f"• Auto-cancel + refund jika {TIMEOUT_MINUTES} menit tanpa OTP\n\n"
            f"💡 <i>Total ≥ Rp {format_rupiah(BULK_CONFIRM_THRESHOLD)} akan muncul konfirmasi dulu.</i>")
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
    text = f"{judul}\n\nTotal: <b>{len(orders)}</b> nomor aktif"
    if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else: await context.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# ============================================================
# === HANDLER UTAMA ===
# ============================================================
async def start(update, context):
    user = update.effective_user
    is_new, needs_onboarding = db_touch_user(user.id, user.first_name, user.username)
    _, s = cek_saldo_api()
    stats = db_get_user_stats(user.id)
    welcome = get_template("welcome", "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>")

    if is_new or needs_onboarding:
        teks = (f"{welcome}\n\n"
                f"👋 Halo <b>{user.first_name or 'Bos'}</b>, selamat bergabung!\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>Ini cara pakai bot ini (3 langkah):</b>\n\n"
                f"1️⃣ Ketuk <b>🛒 Beli Nomor</b>\n"
                f"2️⃣ Pilih harga Gojek yang kamu mau\n"
                f"3️⃣ Ketuk nomornya untuk salin → tempel di aplikasi Gojek\n\n"
                f"🔑 <b>OTP dikirim otomatis oleh bot</b> — tidak perlu tanya lagi.\n"
                f"⏱️ Jika OTP tidak masuk dalam {TIMEOUT_MINUTES} menit, nomor dibatalkan & <b>saldo kembali 100%</b>.\n"
                f"━━━━━━━━━━━━━━━━━━━━")
        await update.message.reply_text(teks, reply_markup=InlineKeyboardMarkup(kb_menu_utama(user.id)), parse_mode='HTML')
        db_mark_onboarded(user.id)
        if s >= 0 and s < LOW_BALANCE_THRESHOLD:
            await update.message.reply_text(f"⚠️ <b>Saldo rendah!</b> Saldo: Rp {format_rupiah(s)}. Segera top-up ke admin.", parse_mode='HTML')
        return

    teks = (f"{welcome}\n\n👋 Halo, <b>{user.first_name or 'Bos'}</b>!\n\n━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Saldo:</b> Rp {format_rupiah(s) if s >= 0 else '-'}\n"
            f"📊 <b>Total Order:</b> {stats['total']} | ✅ {stats['completed']} selesai\n"
            f"⏱️ <b>Uptime:</b> {get_uptime()}\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💡 <i>Bot auto-refund jika OTP tidak masuk dalam {TIMEOUT_MINUTES} menit.</i>")
    await update.message.reply_text(teks, reply_markup=InlineKeyboardMarkup(kb_menu_utama(user.id)), parse_mode='HTML')
    if s >= 0 and s < LOW_BALANCE_THRESHOLD:
        await update.message.reply_text(f"⚠️ <b>Peringatan Saldo Rendah!</b>\n\nSaldo: <b>Rp {format_rupiah(s)}</b>\nSegera top-up ke admin.", parse_mode='HTML')

async def help_command(update, context):
    await update.message.reply_text(
        "❓ <b>Bantuan</b>\n\n"
        "Gunakan menu <b>ℹ️ Info & Bantuan</b> di bot untuk FAQ lengkap dan kontak admin.\n\n"
        "🚀 /start — Menu utama\n"
        "🛒 /beli — Beli nomor Gojek\n"
        "💰 /saldo — Cek saldo",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def button_handler(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    if not is_admin(user_id) and not check_rate_limit(user_id):
        await query.answer("⏱️ Terlalu cepat. Tunggu sebentar.", show_alert=True)
        return

    await query.answer()
    if not is_admin(user_id):
        await query.edit_message_text("🚫 Akses ditolak."); return

    data = query.data

    # Anti double-click untuk operasi beli (termasuk konfirmasi)
    is_purchase = (
        data.startswith('buy_') or data.startswith('bulkqty_') or
        data.startswith('buyconfirm_') or data.startswith('bulkconfirm_')
    )
    if is_purchase:
        if not try_acquire_lock(user_id):
            await query.answer("⏳ Masih memproses transaksi sebelumnya, tunggu sebentar.", show_alert=True)
            return

    try:
        if data == 'menu_utama': await send_menu_utama(query, context)
        elif data == 'refresh_beli': await send_products_menu(query.message.chat_id, context, query=query, force_refresh=True)

        elif data == 'akun_saya':
            _, s = cek_saldo_api()
            stats = db_get_user_stats(user_id)
            teks = (f"💼 <b>Akun Saya</b>\n\n👤 Nama: {query.from_user.first_name or '-'}\n🆔 ID: <code>{user_id}</code>\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n💰 <b>Saldo:</b> Rp {format_rupiah(s) if s >= 0 else '-'}\n"
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
            await query.edit_message_text("📦 <b>Order Aktif</b>\n\nSilakan pilih aksi:",
                reply_markup=InlineKeyboardMarkup(kb_aktif_menu()), parse_mode='HTML')
        elif data == 'aktif_cancel': await send_active_orders_menu(query.message.chat_id, context, 'cancel', query=query)
        elif data == 'aktif_finish': await send_active_orders_menu(query.message.chat_id, context, 'finish', query=query)

        elif data == 'aktif_cancel_all':
            orders, err = get_active_orders()
            if err:
                await query.edit_message_text(f"❌ {err}", reply_markup=InlineKeyboardMarkup(kb_aktif_menu())); return
            to_cancel = [o for o in orders if not o.get("otp_code")]
            skipped = len(orders) - len(to_cancel)
            if not to_cancel:
                await query.edit_message_text(
                    f"✅ <b>Tidak Ada yang Perlu Dibatalkan</b>\n\nSemua <b>{skipped}</b> nomor aktif sudah menerima OTP.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='aktif')]]),
                    parse_mode='HTML')
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
            await query.edit_message_text(
                "⏳ <b>Sedang membatalkan semua nomor...</b>\n\nMohon tunggu 1–3 menit. Bot akan kirim pesan setelah selesai.",
                parse_mode='HTML')
            asyncio.create_task(batch_cancel_task(context, query.message.chat_id, user_id))

        elif data == 'pengaturan':
            await query.edit_message_text("⚙️ <b>Pengaturan</b>\n\nSilakan pilih:", reply_markup=InlineKeyboardMarkup(kb_pengaturan()), parse_mode='HTML')

        elif data == 'notif_menu':
            watchlist = db_watchlist_get(user_id)
            if not watchlist:
                teks = "🔔 <b>Notifikasi Restock</b>\n\nBelum ada produk yang dipantau.\n\n💡 <i>Cara pakai: saat stok habis, klik 🔔 Pantau di menu Beli Nomor.</i>"
            else:
                teks = f"🔔 <b>Notifikasi Restock</b>\n\nAnda memantau {len(watchlist)} produk:\n\n"
                for wid, pid, price in watchlist: teks += f"🔔 Gojek Rp {format_rupiah(price)}\n"
                teks += "\nBot akan kirim notif saat stok kembali."
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='pengaturan')]]), parse_mode='HTML')

        elif data == 'template_menu':
            current_w = get_template("welcome", "(default)")
            current_o = get_template("otp_found", "(default)")
            teks = (f"💬 <b>Template Pesan</b>\n\n<b>1. Sambutan:</b>\n<i>{current_w[:100]}</i>\n"
                    f"Ubah: <code>/setwelcome Teks baru</code>\n\n<b>2. Template OTP:</b>\n<i>{current_o[:100]}</i>\n"
                    f"Ubah: <code>/setotp Teks dengan {{otp}}</code>\n\n"
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
            teks = (f"📊 <b>Status Bot</b>\n\n🤖 <b>Nama:</b> ReivaldoNokos\n📦 <b>Versi:</b> {BOT_VERSION}\n"
                    f"🟢 <b>Status:</b> Online\n⏱️ <b>Uptime:</b> {get_uptime()}\n\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"📡 <b>Koneksi SMScode:</b> {sms_status}\n💾 <b>Database:</b> {db_status}\n🌐 <b>API:</b> api.smscode.gg\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n📊 Total Order: {stats['total'][0] if stats else 0}\n"
                    f"💸 Pengeluaran: Rp {format_rupiah(stats['total'][1]) if stats else 0}")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='info_bantuan')]]), parse_mode='HTML')

        elif data == 'info_faq':
            teks = (f"📖 <b>FAQ</b>\n\n<b>❓ Kenapa nomor tidak muncul OTP?</b>\n• Cek koneksi internet\n• Pastikan nomor benar\n"
                    f"• Tunggu 5–10 menit\n• Jika lewat {TIMEOUT_MINUTES} menit, nomor otomatis dibatalkan & saldo dikembalikan\n\n"
                    f"<b>❓ Kenapa stok habis?</b>\n• Stok dari provider fluktuatif\n• Klik 🔔 Pantau untuk notif saat restock\n\n"
                    f"<b>❓ Bagaimana cara top-up saldo?</b>\n• Hubungi admin untuk top-up\n\n"
                    f"<b>❓ Apakah saldo bisa hangus?</b>\n• Tidak. Saldo dikembalikan otomatis jika order dibatalkan/expired")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='info_bantuan')]]), parse_mode='HTML')

        elif data == 'info_kontak':
            teks = (f"📞 <b>Kontak Admin</b>\n\n👤 <b>Telegram:</b> @mreivaldoo\n💬 <b>Jam Operasional:</b> 24/7\n\n"
                    f"Silakan hubungi jika:\n• Ada kendala teknis\n• Ingin top-up saldo\n• Ada pertanyaan lain")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='info_bantuan')]]), parse_mode='HTML')

        elif data == 'favorit':
            watchlist = db_watchlist_get(user_id)
            if not watchlist:
                teks = "⭐ <b>Favorit</b>\n\nBelum ada produk favorit.\n\n💡 Tambah dari menu <b>🛒 Beli Nomor</b> (klik 🔔 saat stok habis)."
            else:
                teks = f"⭐ <b>Favorit</b>\n\nAnda memantau {len(watchlist)} produk:\n\n"
                for wid, pid, price in watchlist: teks += f"⭐ Gojek Rp {format_rupiah(price)}\n"
                teks += "\nBot akan kirim notif saat stok kembali."
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_favorit()), parse_mode='HTML')

        elif data == 'riwayat_menu':
            await query.edit_message_text("📜 <b>Riwayat Order</b>\n\nPilih rentang waktu:", reply_markup=InlineKeyboardMarkup(kb_riwayat()), parse_mode='HTML')

        elif data.startswith('riwayat_'):
            filter_type = data.replace('riwayat_', '')
            label = {"today": "Hari Ini", "week": "7 Hari Terakhir", "month": "Bulan Ini", "all": "Semua"}.get(filter_type, "Semua")
            rows = db_get_history(user_id, 20, filter_type)
            if not rows:
                teks = f"📜 <b>Riwayat ({label})</b>\n\nBelum ada transaksi."
            else:
                teks = f"📜 <b>Riwayat ({label})</b>\n\nMenampilkan {len(rows)} transaksi:\n\n"
                for row in rows:
                    oid, phone, status, otp, created, price = row
                    emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else ("❌" if status == "CANCELLED" else "⏳"))
                    teks += f"{emoji} <code>{format_phone(phone)}</code> — Rp {format_rupiah(price)}\n   {status} | OTP: {otp or '-'}\n   {created}\n\n"
            await query.edit_message_text(teks[:4000], reply_markup=InlineKeyboardMarkup(kb_riwayat()), parse_mode='HTML')

        elif data == 'beli_nomor': await send_products_menu(query.message.chat_id, context, query=query)
        elif data == 'bulk_menu': await send_bulk_menu(query.message.chat_id, context, query=query)

        elif data.startswith('bulkprice_'):
            parts = data[len('bulkprice_'):].split('_')
            pid = int(parts[0]); harga = int(parts[1])
            _, saldo = cek_saldo_api()
            teks = (f"📦 <b>Beli Banyak — Rp {format_rupiah(harga)}</b>\n\n"
                    f"💰 Saldo Anda: <b>Rp {format_rupiah(saldo) if saldo >= 0 else '?'}</b>\n"
                    f"💵 Harga satuan: <b>Rp {format_rupiah(harga)}</b>\n\n"
                    f"<b>Perkiraan total:</b>\n"
                    f"• 5 = Rp {format_rupiah(harga * 5)}\n"
                    f"• 10 = Rp {format_rupiah(harga * 10)}\n"
                    f"• 20 = Rp {format_rupiah(harga * 20)}\n"
                    f"• 30 = Rp {format_rupiah(harga * 30)}\n"
                    f"• 40 = Rp {format_rupiah(harga * 40)}\n"
                    f"• 50 = Rp {format_rupiah(harga * 50)}\n"
                    f"• 100 = Rp {format_rupiah(harga * 100)}")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_bulk_qty(pid, harga)), parse_mode='HTML')

        # === BULK: konfirmasi jika total >= BULK_CONFIRM_THRESHOLD ===
        elif data.startswith('bulkqty_'):
            parts = data[len('bulkqty_'):].split('_')
            pid = int(parts[0]); harga = int(parts[1]); qty = int(parts[2])
            total = harga * qty
            if total >= BULK_CONFIRM_THRESHOLD:
                await query.edit_message_text(
                    f"⚠️ <b>Konfirmasi Pembelian Bulk</b>\n\n"
                    f"📦 Jumlah: <b>{qty} nomor Gojek</b>\n"
                    f"💵 Harga satuan: <b>Rp {format_rupiah(harga)}</b>\n"
                    f"💰 Total: <b>Rp {format_rupiah(total)}</b>\n\n"
                    f"Saldo akan dipotong setelah Anda konfirmasi.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"✅ Ya, Beli {qty} Nomor", callback_data=f"bulkconfirm_{pid}_{harga}_{qty}")],
                        [InlineKeyboardButton("❌ Batal", callback_data='bulk_menu')]
                    ]), parse_mode='HTML')
                return
            await _execute_bulk(query, context, user_id, pid, harga, qty)

        elif data.startswith('bulkconfirm_'):
            parts = data[len('bulkconfirm_'):].split('_')
            pid = int(parts[0]); harga = int(parts[1]); qty = int(parts[2])
            await _execute_bulk(query, context, user_id, pid, harga, qty)

        elif data.startswith('watch_'):
            parts = data[len('watch_'):].split('_')
            pid = int(parts[0]); price = int(parts[1])
            if db_watchlist_add(user_id, pid, price):
                await query.edit_message_text(
                    f"🔔 <b>Dipantau!</b>\n\nProduk Gojek Rp {format_rupiah(price)} akan dipantau. Anda akan dapat notif saat stok kembali.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]]), parse_mode='HTML')
            else: await query.answer("❌ Gagal tambah", show_alert=True)

        elif data == 'stok_habis':
            await query.answer("❌ Stok habis. Pilih harga lain.", show_alert=True)

        # === SINGLE BUY: konfirmasi jika harga >= CONFIRM_THRESHOLD ===
        elif data.startswith('buy_'):
            parts = data[len('buy_'):].split('_')
            pid = int(parts[0]); harga = int(parts[1])
            if harga >= CONFIRM_THRESHOLD:
                await query.edit_message_text(
                    f"⚠️ <b>Konfirmasi Pembelian</b>\n\n"
                    f"💵 Harga: <b>Rp {format_rupiah(harga)}</b>\n"
                    f"📦 1 nomor Gojek\n\n"
                    f"Saldo akan dipotong setelah Anda konfirmasi.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Ya, Beli Sekarang", callback_data=f"buyconfirm_{pid}_{harga}")],
                        [InlineKeyboardButton("❌ Batal", callback_data='beli_nomor')]
                    ]), parse_mode='HTML')
                return
            await _execute_buy(query, context, user_id, pid, harga)

        elif data.startswith('buyconfirm_'):
            parts = data[len('buyconfirm_'):].split('_')
            pid = int(parts[0]); harga = int(parts[1])
            await _execute_buy(query, context, user_id, pid, harga)

        elif data.startswith('checkotp_'):
            order_id = data[len('checkotp_'):]
            cek = http_get(f"{BASE_URL}/orders/{order_id}")
            otp_s = None; status_s = None
            if cek.get("success"):
                otp_s = cek["data"].get("otp_code"); status_s = cek["data"].get("status")
            if otp_s:
                tmpl = get_template("otp_found", "🔑 <b>Kode OTP Ditemukan!</b>\n\n🔑 Kode: <code>{otp}</code>")
                teks = tmpl.replace("{otp}", otp_s)
                await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")], [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]), parse_mode='HTML')
            elif status_s in ["CANCELLED", "EXPIRED", "COMPLETED"]:
                try: await query.message.delete()
                except: pass
            else:
                kb = [[InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")], [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                teks = f"⏳ <b>Status:</b> Menunggu SMS masuk\n⏱️ Sisa waktu: <b>{TIMEOUT_MINUTES}:00</b>\n\nBot akan otomatis mengubah pesan ini menjadi kode OTP."
                await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                pending_messages[order_id] = query.message.message_id

        elif data.startswith('cancel_'):
            order_id = data[len('cancel_'):]
            try: await query.message.delete()
            except: pass
            hasil = batal_order_api(order_id)
            pending_messages.pop(order_id, None)
            if hasil["success"]:
                teks = (f"✅ <b>Berhasil Dibatalkan</b>\n\n"
                        f"Nomor dengan ID <code>{order_id}</code> telah dibatalkan.\n"
                        f"💰 Saldo dikembalikan: <b>Rp {format_rupiah(hasil.get('refund_amount', 0))}</b>\n\n"
                        f"💬 <i>Pesan ini hilang dalam 10 detik.</i>")
            else:
                teks = (f"❌ <b>Gagal Membatalkan</b>\n\nID: <code>{order_id}</code>\nAlasan: {hasil['message']}\n\n"
                        f"💬 <i>Pesan ini hilang dalam 10 detik.</i>")
            await send_auto_delete_message(context, query.message.chat_id, teks)

        elif data.startswith('finish_'):
            order_id = data[len('finish_'):]
            try: await query.message.delete()
            except: pass
            hasil = selesai_order_api(order_id)
            pending_messages.pop(order_id, None)
            if hasil["success"]:
                teks = (f"✅ <b>Berhasil Diselesaikan</b>\n\n"
                        f"Nomor dengan ID <code>{order_id}</code> telah dilepas.\n"
                        f"Nomor bisa langsung dipakai di aplikasi Gojek.\n\n"
                        f"💬 <i>Pesan ini hilang dalam 10 detik.</i>")
            else:
                teks = (f"❌ <b>Gagal Menyelesaikan</b>\n\nID: <code>{order_id}</code>\nAlasan: {hasil['message']}\n\n"
                        f"💬 <i>Pesan ini hilang dalam 10 detik.</i>")
            await send_auto_delete_message(context, query.message.chat_id, teks)

    except Exception as e:
        await log_error(context, f"Button error: {str(e)[:200]}", user_id)
        try:
            await query.edit_message_text(
                "⚠️ Terjadi kesalahan teknis.\n\n<i>Coba lagi atau kembali ke menu utama.</i>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Coba Lagi", callback_data=data)],
                                                    [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]),
                parse_mode='HTML')
        except: pass
    finally:
        if is_purchase:
            release_lock(user_id)

# --- COMMAND HANDLERS ---
async def saldo_command(update, context):
    teks, saldo = cek_saldo_api()
    await update.message.reply_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
    if saldo >= 0 and saldo < LOW_BALANCE_THRESHOLD:
        await update.message.reply_text(f"⚠️ <b>Saldo Rendah!</b>\n\nSaldo: <b>Rp {format_rupiah(saldo)}</b>", parse_mode='HTML')

async def beli_command(update, context): await send_products_menu(update.effective_chat.id, context)

async def riwayat_command(update, context):
    rows = db_get_history(update.effective_user.id, 10, "all")
    if not rows:
        await update.message.reply_text("📜 Belum ada riwayat.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]])); return
    teks = "📜 <b>10 Transaksi Terakhir:</b>\n\n"
    for row in rows:
        oid, phone, status, otp, created, price = row
        emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else "⏳")
        teks += f"{emoji} <code>{format_phone(phone)}</code> — Rp {format_rupiah(price)}\n   {status} | OTP: {otp or '-'}\n   {created}\n\n"
    await update.message.reply_text(teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def stats_command(update, context):
    s = db_get_stats()
    if not s:
        await update.message.reply_text("⚠️ Gagal ambil statistik."); return
    teks = (f"📊 <b>Statistik Global Bot</b>\n\n"
            f"📅 <b>Hari Ini:</b> {s['today'][0]} order | Rp {format_rupiah(s['today'][1])}\n"
            f"📆 <b>Bulan Ini:</b> {s['month'][0]} order | Rp {format_rupiah(s['month'][1])}\n"
            f"📈 <b>Total:</b> {s['total'][0]} order | Rp {format_rupiah(s['total'][1])}\n\n"
            f"📋 <b>Status:</b>\n")
    for st, c in s['statuses'].items(): teks += f"   • {st}: {c}\n"
    await update.message.reply_text(teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def refund_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Akses ditolak."); return
    r = db_get_refund_stats()
    if not r:
        await update.message.reply_text("⚠️ Gagal ambil data refund."); return
    teks = (f"💸 <b>Statistik Refund</b>\n\n"
            f"📅 <b>Hari Ini:</b>\n   • Jumlah: {r['today'][0]} order\n   • Total: Rp {format_rupiah(r['today'][1])}\n\n"
            f"📆 <b>Bulan Ini:</b>\n   • Jumlah: {r['month'][0]} order\n   • Total: Rp {format_rupiah(r['month'][1])}\n\n"
            f"📈 <b>Total Keseluruhan:</b>\n   • Jumlah: {r['total'][0]} order\n   • Total: Rp {format_rupiah(r['total'][1])}")
    await update.message.reply_text(teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def logs_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Akses ditolak."); return
    try:
        conn = get_db()
        rows = conn.execute("SELECT timestamp, level, message, user_id FROM logs ORDER BY id DESC LIMIT 20").fetchall()
    except: rows = []
    if not rows:
        await update.message.reply_text("📜 Belum ada log error.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]])); return
    teks = "📜 <b>20 Log Error Terakhir:</b>\n\n"
    for ts, lvl, msg, uid in rows:
        emoji = "🔴" if lvl == "ERROR" else "🟡"
        teks += f"{emoji} <code>{ts}</code>\n{msg[:200]}\n"
        if uid: teks += f"User: <code>{uid}</code>\n"
        teks += "\n"
    await update.message.reply_text(teks[:4000], parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

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
    commands = [
        BotCommand("start", "🚀 Menu utama"),
        BotCommand("beli", "🛒 Beli nomor Gojek"),
        BotCommand("saldo", "💰 Cek saldo"),
        BotCommand("help", "❓ Bantuan"),
    ]
    try: await application.bot.set_my_commands(commands)
    except Exception as e: logger.error(f"Set commands error: {e}")

    asyncio.create_task(restock_checker_task(application))
    asyncio.create_task(recover_pending_orders(application))
    logger.info(f"Bot v{BOT_VERSION} started")

if __name__ == '__main__':
    validate_env()          # Fail-fast jika env kosong
    init_db()
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    essential = [
        ("start", start), ("help", help_command), ("saldo", saldo_command),
        ("beli", beli_command),
        ("riwayat", riwayat_command), ("stats", stats_command),
        ("refund", refund_command), ("logs", logs_command),
        ("setwelcome", setwelcome_command), ("setotp", setotp_command), ("backup", backup_command),
    ]
    for cmd, fn in essential:
        application.add_handler(CommandHandler(cmd, fn))

    application.add_handler(CallbackQueryHandler(button_handler))
    logger.info("Starting bot polling...")
    print(f"🤖 Bot v{BOT_VERSION} berjalan...")
    application.run_polling()
