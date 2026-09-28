import logging
import os
import json
import re
import time
import sqlite3
import urllib.request
import urllib.error
import asyncio
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# --- KONFIGURASI ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = os.environ.get("ALLOWED_TELEGRAM_IDS", "7854456597")
ALLOWED_USERS = [x.strip() for x in ALLOWED_USERS.split(",") if x.strip()]
ADMIN_ID = ALLOWED_USERS[0] if ALLOWED_USERS else "7854456597"
SMScode_API_KEY = os.environ.get("SMScode_API_KEY")

# VALIDASI CONFIG - Cegah bot start dengan config tidak lengkap
if not TOKEN:
    print("❌ ERROR: TELEGRAM_BOT_TOKEN tidak diatur di environment variables!")
    print("Bot tidak bisa jalan tanpa token Telegram.")
    raise SystemExit(1)

if not SMScode_API_KEY:
    print("⚠️ WARNING: SMScode_API_KEY belum diatur. Bot akan jalan tapi fitur beli nomor tidak aktif.")

LOW_BALANCE_THRESHOLD = 2000
BOT_VERSION = "3.0 Final Stable"
START_TIME = time.time()
MAX_BULK = 100
AUTO_DELETE_SECONDS = 10
BATCH_SIZE = 10
TIMEOUT_MINUTES = 25
TIMEOUT_SECONDS = TIMEOUT_MINUTES * 60

# Concurrency (akan diinit di post_init agar binding ke event loop yang benar)
BULK_SEMAPHORE = None
API_RATE_LOCK = None
LAST_API_CALL = 0.0
MIN_API_INTERVAL = 0.5

# Message Queue
MESSAGE_QUEUE = None
MESSAGE_WORKERS = []
MESSAGE_QUEUE_MAXSIZE = 500

# Cleanup tracking
LAST_CLEANUP = 0
CLEANUP_INTERVAL = 600  # 10 menit

# Bulk buy lock per user (untuk cegah spam)
USER_BULK_LOCKS = {}

BASE_URL = "https://api.smscode.gg/v1"
HEADERS = {
    "Authorization": f"Bearer {SMScode_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")
pending_messages = {}       # order_id -> message_id
pending_messages_ts = {}    # order_id -> timestamp untuk TTL
active_bulk_batches = {}
active_bulk_batches_ts = {}

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

def safe_int(val, default=0):
    try: return int(val)
    except: return default

def safe_parse_callback(data, prefix, expected_parts):
    """Parse callback data dengan aman."""
    try:
        raw = data.replace(prefix, '')
        parts = raw.split('_')
        if len(parts) < expected_parts:
            return None
        return [safe_int(p) for p in parts[:expected_parts]]
    except:
        return None

def get_db_connection():
    """Membuka koneksi SQLite dengan WAL mode & busy_timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn

async def safe_send(context, chat_id, text, reply_markup=None, parse_mode='HTML', max_retry=3):
    """Kirim pesan dengan retry & handling flood control."""
    for attempt in range(max_retry):
        try:
            return await context.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup,
                parse_mode=parse_mode)
        except RetryAfter as e:
            wait = int(e.retry_after) + 1
            print(f"⏳ Flood control, tunggu {wait}s")
            await asyncio.sleep(wait)
        except TimedOut:
            await asyncio.sleep(2)
        except NetworkError as e:
            print(f"⚠️ Network error: {e}")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"❌ Send error: {e}")
            return None
    return None

async def safe_edit(query, text, reply_markup=None, parse_mode='HTML', max_retry=2):
    """Edit pesan dengan retry."""
    for attempt in range(max_retry):
        try:
            return await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
        except Exception as e:
            if attempt == max_retry - 1:
                print(f"❌ Edit error: {e}")
            await asyncio.sleep(1)
    return None

async def send_auto_delete_message(context, chat_id, text, parse_mode='HTML', delete_after=AUTO_DELETE_SECONDS, reply_markup=None):
    try:
        msg = await safe_send(context, chat_id, text, reply_markup, parse_mode)
        if msg:
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

async def enqueue_message(chat_id, text, reply_markup=None, parse_mode='HTML'):
    """Masukkan pesan ke queue agar tidak kena flood control."""
    if MESSAGE_QUEUE is not None:
        try:
            MESSAGE_QUEUE.put_nowait((chat_id, text, reply_markup, parse_mode))
        except asyncio.QueueFull:
            print(f"⚠️ Message queue penuh, drop pesan ke {chat_id}")
    else:
        print(f"⚠️ Queue belum siap, drop pesan ke {chat_id}")

async def message_worker(context):
    """Worker mengirim pesan dari queue."""
    global MESSAGE_QUEUE
    while True:
        try:
            item = await MESSAGE_QUEUE.get()
            chat_id, text, reply_markup, parse_mode = item
            await safe_send(context, chat_id, text, reply_markup, parse_mode)
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Worker loop error: {e}")
            await asyncio.sleep(1)

async def cleanup_task():
    """Task untuk membersihkan data yang expired."""
    global LAST_CLEANUP
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            # Cleanup pending_messages (TTL 1 jam)
            expired = [oid for oid, ts in pending_messages_ts.items() if now - ts > 3600]
            for oid in expired:
                pending_messages.pop(oid, None)
                pending_messages_ts.pop(oid, None)
            # Cleanup active_bulk_batches (TTL 2 jam)
            expired_b = [bid for bid, ts in active_bulk_batches_ts.items() if now - ts > 7200]
            for bid in expired_b:
                active_bulk_batches.pop(bid, None)
                active_bulk_batches_ts.pop(bid, None)
            # Cleanup user bulk locks
            expired_l = [uid for uid, ts in USER_BULK_LOCKS.items() if now - ts > 3600]
            for uid in expired_l:
                USER_BULK_LOCKS.pop(uid, None)
        except Exception as e:
            print(f"Cleanup error: {e}")

# --- DATABASE ---
def init_db():
    conn = get_db_connection()
    try:
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
            level TEXT,
            message TEXT,
            user_id INTEGER
        )''')
        conn.commit()
    finally:
        conn.close()

    # Load user yang tersimpan
    global ALLOWED_USERS
    saved = get_template("allowed_users", "")
    if saved:
        extra = [x.strip() for x in saved.split(",") if x.strip()]
        for uid in extra:
            if uid not in ALLOWED_USERS:
                ALLOWED_USERS.append(uid)

def db_save_log(level, message, user_id=None):
    for attempt in range(3):
        conn = None
        try:
            conn = get_db_connection()
            conn.execute("INSERT INTO logs (level, message, user_id) VALUES (?, ?, ?)", (level, message[:500], user_id))
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                time.sleep(0.5); continue
            return
        except: return
        finally:
            if conn: conn.close()

def db_save_order(order_id, user_id, phone, product_id, price):
    for attempt in range(3):
        conn = None
        try:
            conn = get_db_connection()
            # Cegah overwrite order yang sudah ada
            existing = conn.execute("SELECT order_id FROM orders WHERE order_id=?", (str(order_id),)).fetchone()
            if existing:
                return
            conn.execute("INSERT INTO orders (order_id, user_id, phone, product_id, price, status) VALUES (?, ?, ?, ?, ?, 'ACTIVE')",
                         (str(order_id), user_id, phone, product_id, price))
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                time.sleep(0.5); continue
            print(f"DB Error: {e}"); return
        except Exception as e:
            print(f"DB Error: {e}"); return
        finally:
            if conn: conn.close()

def db_update_order(order_id, status, otp=None):
    for attempt in range(3):
        conn = None
        try:
            conn = get_db_connection()
            if otp:
                # Hanya update jika OTP belum ada (idempotency)
                conn.execute("UPDATE orders SET status=?, otp=? WHERE order_id=? AND (otp IS NULL OR otp='')",
                             (status, otp, str(order_id)))
            else:
                conn.execute("UPDATE orders SET status=? WHERE order_id=?", (status, str(order_id)))
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                time.sleep(0.5); continue
            print(f"DB Error: {e}"); return
        except Exception as e:
            print(f"DB Error: {e}"); return
        finally:
            if conn: conn.close()

def db_order_has_otp(order_id):
    conn = None
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT otp FROM orders WHERE order_id=?", (str(order_id),)).fetchone()
        return bool(row and row[0])
    except: return False
    finally:
        if conn: conn.close()

def db_get_history(user_id, limit=10, filter_type="all"):
    conn = None
    try:
        conn = get_db_connection()
        if filter_type == "today":
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? AND DATE(created_at, '+7 hours') = DATE('now', '+7 hours') ORDER BY created_at DESC LIMIT ?"
        elif filter_type == "week":
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? AND datetime(created_at, '+7 hours') >= datetime('now', '-7 days', '+7 hours') ORDER BY created_at DESC LIMIT ?"
        elif filter_type == "month":
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? AND strftime('%Y-%m', datetime(created_at, '+7 hours')) = strftime('%Y-%m', 'now', '+7 hours') ORDER BY created_at DESC LIMIT ?"
        else:
            q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?"
        return conn.execute(q, (user_id, limit)).fetchall()
    except Exception as e:
        print(f"DB Error: {e}"); return []
    finally:
        if conn: conn.close()

def db_get_user_stats(user_id):
    conn = None
    try:
        conn = get_db_connection()
        total = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=?", (user_id,)).fetchone()
        completed = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='COMPLETED'", (user_id,)).fetchone()[0]
        cancelled = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='CANCELLED'", (user_id,)).fetchone()[0]
        return {"total": total[0], "spent": total[1], "completed": completed, "cancelled": cancelled}
    except: return {"total": 0, "spent": 0, "completed": 0, "cancelled": 0}
    finally:
        if conn: conn.close()

def db_get_stats():
    conn = None
    try:
        conn = get_db_connection()
        today = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE DATE(created_at, '+7 hours') = DATE('now', '+7 hours')").fetchone()
        month = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE strftime('%Y-%m', datetime(created_at, '+7 hours')) = strftime('%Y-%m', 'now', '+7 hours')").fetchone()
        total = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders").fetchone()
        statuses = dict(conn.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall())
        return {"today": today, "month": month, "total": total, "statuses": statuses}
    except: return None
    finally:
        if conn: conn.close()

def db_get_refund_stats(user_id=None):
    conn = None
    try:
        conn = get_db_connection()
        today = conn.execute("SELECT COUNT(*), COALESCE(SUM(refund_amount),0) FROM orders WHERE refund_amount > 0 AND DATE(refunded_at, '+7 hours') = DATE('now', '+7 hours')").fetchone()
        month = conn.execute("SELECT COUNT(*), COALESCE(SUM(refund_amount),0) FROM orders WHERE refund_amount > 0 AND strftime('%Y-%m', datetime(refunded_at, '+7 hours')) = strftime('%Y-%m', 'now', '+7 hours')").fetchone()
        total = conn.execute("SELECT COUNT(*), COALESCE(SUM(refund_amount),0) FROM orders WHERE refund_amount > 0").fetchone()
        return {"today": today, "month": month, "total": total}
    except: return None
    finally:
        if conn: conn.close()

def get_template(key, default=""):
    conn = None
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT value FROM templates WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    except: return default
    finally:
        if conn: conn.close()

def set_template(key, value):
    for attempt in range(3):
        conn = None
        try:
            conn = get_db_connection()
            conn.execute("INSERT OR REPLACE INTO templates (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
            return True
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                time.sleep(0.5); continue
            return False
        except: return False
        finally:
            if conn: conn.close()

def db_watchlist_add(user_id, product_id, price):
    conn = None
    try:
        conn = get_db_connection()
        conn.execute("INSERT INTO watchlist (user_id, product_id, price) VALUES (?, ?, ?)", (user_id, product_id, price))
        conn.commit()
        return True
    except: return False
    finally:
        if conn: conn.close()

def db_watchlist_remove_by_id(wid):
    conn = None
    try:
        conn = get_db_connection()
        conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
        conn.commit()
        return True
    except: return False
    finally:
        if conn: conn.close()

def db_watchlist_get(user_id):
    conn = None
    try:
        conn = get_db_connection()
        return conn.execute("SELECT id, product_id, price FROM watchlist WHERE user_id=?", (user_id,)).fetchall()
    except: return []
    finally:
        if conn: conn.close()

# --- HTTP SYNC (untuk thread) ---
def _http_get_sync(url):
    req = urllib.request.Request(url, headers=HEADERS, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
        except: return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e: return {"error": str(e)}

def _http_post_sync(url, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=HEADERS, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
        except: return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e: return {"error": str(e)}

# --- HTTP ASYNC dengan RETRY ---
async def _rate_limit_wait():
    global LAST_API_CALL
    async with API_RATE_LOCK:
        now = time.time()
        wait = MIN_API_INTERVAL - (now - LAST_API_CALL)
        if wait > 0:
            await asyncio.sleep(wait)
        LAST_API_CALL = time.time()

async def http_get_async(url, max_retry=3):
    for attempt in range(max_retry):
        await _rate_limit_wait()
        result = await asyncio.to_thread(_http_get_sync, url)
        # Retry jika bukan error client (4xx selain 429)
        err = result.get("error", "")
        if not err:
            return result
        if "429" in err or "503" in err or "502" in err or "timeout" in err.lower():
            if attempt < max_retry - 1:
                await asyncio.sleep(2 ** attempt)
                continue
        return result
    return result

async def http_post_async(url, payload, max_retry=3):
    for attempt in range(max_retry):
        await _rate_limit_wait()
        result = await asyncio.to_thread(_http_post_sync, url, payload)
        err = result.get("error", "")
        if not err:
            return result
        if "429" in err or "503" in err or "502" in err or "timeout" in err.lower():
            if attempt < max_retry - 1:
                await asyncio.sleep(2 ** attempt)
                continue
        return result
    return result

def translate_error(err_msg):
    if not err_msg: return "⚠️ Terjadi kesalahan."
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
    if "rate limit" in e or "too many requests" in e or "429" in e: return "⏱️ Terlalu banyak permintaan. Coba lagi."
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

def is_allowed(uid): return str(uid) in [str(x) for x in ALLOWED_USERS]
def is_admin(uid): return str(uid) == str(ADMIN_ID)
def format_phone(p):
    if p and p.startswith("62"): return p[2:]
    return p or "?"

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
        await enqueue_message(ADMIN_ID, teks)
    except: pass

async def get_balance():
    data = await http_get_async(f"{BASE_URL}/balance")
    if data.get("success"): return data["data"].get("balance", 0)
    return -1

# --- API SMScode (ASYNC) ---
async def get_gojek_products():
    data = await http_get_async(f"{BASE_URL}/catalog/products?country_id=7")
    if "error" in data: return None, translate_error(data["error"])
    if not data.get("success"): return None, "Gagal ambil data."
    products = data.get("data", [])
    if not isinstance(products, list): return None, "Format data tidak valid."
    gojek = [p for p in products if "Gojek" in p.get("name", "")]
    gojek.sort(key=lambda x: x.get('price', 999999))
    return gojek, None

async def get_active_orders():
    data = await http_get_async(f"{BASE_URL}/orders/active")
    if "error" in data: return None, translate_error(data["error"])
    if not data.get("success"): return None, "Gagal ambil order."
    orders = data.get("data", [])
    if not isinstance(orders, list): return None, "Format data tidak valid."
    return orders, None

async def cek_saldo_api():
    if not SMScode_API_KEY: return "❌ API Key belum diatur.", -1
    data = await http_get_async(f"{BASE_URL}/balance")
    if "error" in data: return translate_error(data["error"]), -1
    if data.get("success"):
        saldo = data["data"].get("balance", 0)
        return f"💰 Saldo Saya: <b>Rp {format_rupiah(saldo)}</b>", saldo
    return "⚠️ Gagal ambil saldo.", -1

async def beli_nomor_api(product_id, user_id, price):
    if not SMScode_API_KEY: return None, "❌ API Key belum diatur."
    saldo = await get_balance()
    if saldo >= 0 and saldo < price:
        return None, f"❌ Saldo tidak cukup.\nSaldo: Rp {format_rupiah(saldo)}\nButuh: Rp {format_rupiah(price)}"
    data = await http_post_async(f"{BASE_URL}/orders/create", {"product_id": product_id})
    if "error" in data: return None, translate_error(data["error"])
    if data.get("success"):
        try:
            od = data["data"]["orders"][0]
            oid = str(od["id"]); nomor = od.get("phone_number", "")
            db_save_order(oid, user_id, nomor, product_id, price)
            return oid, nomor
        except (KeyError, IndexError) as e:
            return None, f"⚠️ Format respons tidak valid: {e}"
    return None, "⚠️ Gagal membeli nomor."

async def beli_banyak_api_async(product_id, user_id, price, quantity):
    if not SMScode_API_KEY: return None, 0, "❌ API Key belum diatur."
    if quantity > MAX_BULK: return None, 0, f"❌ Maksimal {MAX_BULK} nomor."

    async with BULK_SEMAPHORE:
        total_cost = price * quantity
        saldo = await get_balance()
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
            data = await http_post_async(f"{BASE_URL}/orders/create", {"product_id": product_id, "quantity": this_batch})
            if "error" in data:
                if batch_num == 1:
                    return None, 0, translate_error(data["error"])
                break
            if data.get("success"):
                orders = data["data"].get("orders", [])
                failed = data["data"].get("failed_count", 0)
                for od in orders:
                    try:
                        oid = str(od["id"]); nomor = od.get("phone_number", "")
                        db_save_order(oid, user_id, nomor, product_id, price)
                        all_results.append((oid, nomor))
                    except: pass
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

async def batal_order_api(order_id):
    data = await http_post_async(f"{BASE_URL}/orders/cancel", {"id": safe_int(order_id)})
    if "error" in data: return {"success": False, "message": translate_error(data["error"]), "refund_amount": 0}
    if data.get("success"):
        refund_amount = 0
        conn = None
        try:
            conn = get_db_connection()
            row = conn.execute("SELECT price FROM orders WHERE order_id=?", (str(order_id),)).fetchone()
            refund_amount = row[0] if row else 0
            conn.execute("UPDATE orders SET status='CANCELLED', refund_amount=?, refunded_at=CURRENT_TIMESTAMP WHERE order_id=?",
                         (refund_amount, str(order_id)))
            conn.commit()
        except Exception as e:
            print(f"DB Error: {e}")
            db_update_order(order_id, "CANCELLED")
        finally:
            if conn: conn.close()
        return {"success": True, "message": "✅ Nomor berhasil dibatalkan & saldo dikembalikan.", "refund_amount": refund_amount}
    return {"success": False, "message": "⚠️ Gagal membatalkan nomor.", "refund_amount": 0}

async def selesai_order_api(order_id):
    data = await http_post_async(f"{BASE_URL}/orders/finish", {"id": safe_int(order_id)})
    if "error" in data: return {"success": False, "message": translate_error(data["error"])}
    if data.get("success"):
        db_update_order(order_id, "COMPLETED")
        return {"success": True, "message": "✅ Nomor berhasil diselesaikan."}
    return {"success": False, "message": "⚠️ Gagal menyelesaikan nomor."}

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
# === SINGLE ORDER POLLING ===
# ============================================================
async def auto_poll_otp(chat_id, context, order_id, nomor_display):
    total = TIMEOUT_SECONDS
    for i in range(total // 5):
        await asyncio.sleep(5)
        elapsed = (i + 1) * 5; remaining = total - elapsed
        # Skip jika OTP sudah ada di DB (webhook mungkin sudah kirim)
        if db_order_has_otp(order_id):
            pending_messages.pop(order_id, None)
            pending_messages_ts.pop(order_id, None)
            return
        data = await http_get_async(f"{BASE_URL}/orders/{order_id}")
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
                try: await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                except: pass
            else:
                await enqueue_message(chat_id, teks, InlineKeyboardMarkup(kb))
            pending_messages.pop(order_id, None)
            pending_messages_ts.pop(order_id, None)
            return
        elif status in ["CANCELLED", "EXPIRED", "COMPLETED"]:
            db_update_order(order_id, status)
            if msg_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except: pass
            pending_messages.pop(order_id, None)
            pending_messages_ts.pop(order_id, None)
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
    cek = await http_get_async(f"{BASE_URL}/orders/{order_id}")
    otp_akhir = None
    if cek.get("success"): otp_akhir = cek["data"].get("otp_code")
    if otp_akhir and not db_order_has_otp(order_id):
        db_update_order(order_id, "OTP_RECEIVED", otp_akhir)
        tmpl = get_template("otp_found", "🔑 <b>Kode OTP Ditemukan!</b>\n\n📱 Nomor: <code>{nomor}</code>\n🔑 Kode: <code>{otp}</code>")
        pesan = tmpl.replace("{otp}", otp_akhir).replace("{nomor}", nomor_display).replace("{order_id}", order_id)
        msg_id = pending_messages.get(order_id)
        if msg_id:
            try: await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=pesan, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{order_id}")]]), parse_mode='HTML')
            except: pass
        pending_messages.pop(order_id, None)
        pending_messages_ts.pop(order_id, None)
        return
    await batal_order_api(order_id)
    if msg_id:
        try: await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except: pass
    pending_messages.pop(order_id, None)
    pending_messages_ts.pop(order_id, None)

# ============================================================
# === BULK MONITOR ===
# ============================================================
async def bulk_monitor_task(context, chat_id, batch_id, ordered_list, price_per_unit, summary_msg_id, failed_count=0):
    total = len(ordered_list)
    pending = {oid: phone for oid, phone in ordered_list}
    received_set = set()
    start_time = time.time()
    timeout_seconds = TIMEOUT_SECONDS
    active_bulk_batches[batch_id] = {
        "orders": ordered_list, "received": received_set,
        "summary_msg_id": summary_msg_id, "pending": pending
    }
    active_bulk_batches_ts[batch_id] = time.time()
    async def update_summary():
        try:
            elapsed = int(time.time() - start_time)
            remaining = timeout_seconds - elapsed
            if remaining < 0: remaining = 0
            teks = build_bulk_summary(ordered_list, received_set, price_per_unit, remaining, failed_count)
            kb = [[InlineKeyboardButton("📦 Order Aktif", callback_data='aktif')],
                  [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=summary_msg_id,
                text=teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
        except Exception: pass
    await update_summary()
    while pending and (time.time() - start_time) < timeout_seconds:
        await asyncio.sleep(5)
        if not pending: break
        data = await http_get_async(f"{BASE_URL}/orders/active")
        active_map = {}
        if data.get("success"):
            for o in data.get("data", []):
                active_map[str(o.get("id"))] = o
        to_remove = []
        for oid, phone in list(pending.items()):
            # Skip jika OTP sudah ada di DB
            if db_order_has_otp(oid):
                received_set.add(oid); to_remove.append(oid); continue
            o = active_map.get(oid)
            if o is None:
                cek = await http_get_async(f"{BASE_URL}/orders/{oid}")
                if cek.get("success"): o = cek["data"]
                else: continue
            otp = o.get("otp_code"); status = o.get("status")
            if otp and oid not in received_set:
                received_set.add(oid)
                db_update_order(oid, "OTP_RECEIVED", otp)
                await enqueue_message(chat_id,
                    f"🔑 <b>OTP Diterima!</b>\n\n📱 Nomor: <code>{phone}</code>\n🔑 Kode: <code>{otp}</code>\n🆔 ID: <code>{oid}</code>\n\n📊 Progress: <b>{len(received_set)}/{total}</b>",
                    InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{oid}")]]))
                to_remove.append(oid)
            elif status in ["CANCELLED", "EXPIRED", "COMPLETED"]:
                db_update_order(oid, status); to_remove.append(oid)
        for oid in to_remove: pending.pop(oid, None)
        await update_summary()
    await update_summary()
    if pending:
        for oid, phone in list(pending.items()):
            await batal_order_api(oid)
        await update_summary()
    active_bulk_batches.pop(batch_id, None)
    active_bulk_batches_ts.pop(batch_id, None)

# ============================================================
# === BATCH CANCEL TASK ===
# ============================================================
async def batch_cancel_task(context, chat_id, user_id):
    orders, err = await get_active_orders()
    if err:
        try: await safe_send(context, chat_id, f"❌ {err}")
        except: pass
        return
    to_cancel = [o for o in orders if not o.get("otp_code")]
    skipped = len(orders) - len(to_cancel)
    total = len(to_cancel)
    if total == 0:
        try:
            await safe_send(context, chat_id,
                f"✅ Tidak ada nomor yang perlu dibatalkan.\n\n📭 Semua {skipped} nomor aktif sudah menerima OTP.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
        except: pass
        return
    success = 0; failed = 0; refund_total = 0; failed_oids = []
    for o in to_cancel:
        oid = str(o.get("id"))
        hasil = await batal_order_api(oid)
        if hasil["success"]:
            success += 1
            refund_total += hasil.get("refund_amount", 0)
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
        await safe_send(context, chat_id, teks, InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
    except: pass

# ============================================================
# === BACKGROUND TASKS ===
# ============================================================
async def restock_checker_task(context):
    while True:
        await asyncio.sleep(5 * 60)
        try:
            conn = get_db_connection()
            try:
                rows = conn.execute("SELECT id, user_id, product_id, price FROM watchlist").fetchall()
            finally:
                conn.close()
            if not rows: continue
            products, err = await get_gojek_products()
            if err or not products: continue
            for wid, uid, pid, price in rows:
                for p in products:
                    if p.get('id') == pid and p.get('available', 0) > 0:
                        await enqueue_message(uid,
                            f"🎉 <b>Stok Kembali Tersedia!</b>\n\n📦 Gojek\n💵 Harga: Rp {format_rupiah(price)}\n📊 Stok: {p.get('available')}\n\nSegera beli!",
                            InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Beli Sekarang", callback_data='beli_nomor')]]))
                        db_watchlist_remove_by_id(wid)
                        break
        except Exception as e: print(f"Restock error: {e}")

async def auto_backup_task(context):
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            if os.path.exists(DB_PATH):
                # Checkpoint WAL dulu sebelum backup
                conn = get_db_connection()
                try:
                    conn.execute("PRAGMA wal_checkpoint(FULL)")
                finally:
                    conn.close()
                with open(DB_PATH, 'rb') as f:
                    await context.bot.send_document(chat_id=ADMIN_ID, document=f,
                        filename=f"backup_{time.strftime('%Y%m%d_%H%M')}.db",
                        caption=f"🗄️ <b>Backup Otomatis</b>\n{time.strftime('%Y-%m-%d %H:%M:%S')} WIB",
                        parse_mode='HTML')
        except Exception as e: print(f"Backup error: {e}")

# ============================================================
# === MENU KEYBOARDS ===
# ============================================================
def kb_menu_utama():
    return [
        [InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')],
        [InlineKeyboardButton("💼 Akun Saya", callback_data='akun_saya'),
         InlineKeyboardButton("⭐ Favorit", callback_data='favorit')],
        [InlineKeyboardButton("📜 Riwayat", callback_data='riwayat_menu'),
         InlineKeyboardButton("ℹ️ Info & Bantuan", callback_data='info_bantuan')]
    ]

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
# === TAMPILAN MENU ===
# ============================================================
async def send_menu_utama(query, context):
    welcome = get_template("welcome", "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>")
    user = query.from_user.first_name or "Bos"
    _, s = await cek_saldo_api()
    stats = db_get_user_stats(query.from_user.id)
    teks = (f"{welcome}\n\n👋 Halo, <b>{user}</b>!\n\n━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Saldo Saya:</b> Rp {format_rupiah(s) if s >= 0 else '-'}\n"
            f"📊 <b>Total Order:</b> {stats['total']} | ✅ {stats['completed']} selesai\n"
            f"⏱️ <b>Server Uptime:</b> {get_uptime()}\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💡 <i>Bot otomatis membatalkan nomor jika OTP tidak masuk dalam {TIMEOUT_MINUTES} menit.</i>")
    await safe_edit(query, teks, InlineKeyboardMarkup(kb_menu_utama()))

async def send_products_menu(chat_id, context, query=None):
    products, err = await get_gojek_products()
    if err:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]])
        if query: await safe_edit(query, f"❌ {err}", kb)
        else: await safe_send(context, chat_id, f"❌ {err}", kb)
        return
    if not products:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]])
        text = "❌ Tidak ada produk Gojek tersedia saat ini."
        if query: await safe_edit(query, text, kb)
        else: await safe_send(context, chat_id, text, kb)
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
    if query: await safe_edit(query, text, InlineKeyboardMarkup(keyboard))
    else: await safe_send(context, chat_id, text, InlineKeyboardMarkup(keyboard))

async def send_bulk_menu(chat_id, context, query=None):
    products, err = await get_gojek_products()
    if err:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]])
        if query: await safe_edit(query, f"❌ {err}", kb)
        else: await safe_send(context, chat_id, f"❌ {err}", kb)
        return
    if not products:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]])
        text = "❌ Tidak ada produk Gojek tersedia saat ini."
        if query: await safe_edit(query, text, kb)
        else: await safe_send(context, chat_id, text, kb)
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
            f"• Pembelian dipecah jadi batch 10 nomor\n"
            f"• Timer live {TIMEOUT_MINUTES} menit\n"
            f"• Auto-cancel jika tidak ada OTP\n\n"
            f"⏳ <i>Jika ada user lain sedang bulk buy, Anda masuk antrean.</i>")
    if query: await safe_edit(query, text, InlineKeyboardMarkup(keyboard))
    else: await safe_send(context, chat_id, text, InlineKeyboardMarkup(keyboard))

async def send_active_orders_menu(chat_id, context, action, query=None):
    orders, err = await get_active_orders()
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='aktif')]])
    if err:
        if query: await safe_edit(query, f"❌ {err}", kb_back)
        else: await safe_send(context, chat_id, f"❌ {err}")
        return
    if not orders:
        text = "📭 Tidak ada nomor aktif saat ini."
        if query: await safe_edit(query, text, kb_back)
        else: await safe_send(context, chat_id, text, kb_back)
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
    if query: await safe_edit(query, text, InlineKeyboardMarkup(keyboard))
    else: await safe_send(context, chat_id, text, InlineKeyboardMarkup(keyboard))

# ============================================================
# === HANDLER UTAMA ===
# ============================================================
async def start(update, context):
    welcome = get_template("welcome", "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>")
    user = update.effective_user.first_name or "Bos"
    _, s = await cek_saldo_api()
    stats = db_get_user_stats(update.effective_user.id)
    teks = (f"{welcome}\n\n👋 Halo, <b>{user}</b>!\n\n━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Saldo Saya:</b> Rp {format_rupiah(s) if s >= 0 else '-'}\n"
            f"📊 <b>Total Order:</b> {stats['total']} | ✅ {stats['completed']} selesai\n"
            f"⏱️ <b>Server Uptime:</b> {get_uptime()}\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💡 <i>Bot otomatis membatalkan nomor jika OTP tidak masuk dalam {TIMEOUT_MINUTES} menit.</i>")
    await safe_send(context, update.effective_chat.id, teks, InlineKeyboardMarkup(kb_menu_utama()))
    if s >= 0 and s < LOW_BALANCE_THRESHOLD:
        await safe_send(context, update.effective_chat.id, f"⚠️ <b>Peringatan Saldo Rendah!</b>\n\nSaldo: <b>Rp {format_rupiah(s)}</b>\nSegera top-up!")

async def help_command(update, context):
    await safe_send(context, update.effective_chat.id,
        "❓ <b>Daftar Perintah:</b>\n\n🚀 /start - Menu utama\n🛒 /beli - Beli nomor\n💰 /saldo - Cek saldo\n"
        "📦 /aktif - Nomor aktif\n📜 /riwayat - Riwayat order\n📊 /stats - Statistik\n"
        "💸 /refund - Statistik refund\n"
        "❌ /batal - Batalkan\n✅ /selesai - Selesaikan\n\n"
        "👑 <b>Admin:</b>\n/setwelcome [teks]\n/setotp [teks]\n/backup\n/logs\n/adduser [ID]\n/removeuser [ID]\n/listuser",
        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def button_handler(update, context):
    query = update.callback_query
    try:
        await query.answer()
    except: pass
    user_id = query.from_user.id
    if not is_allowed(user_id):
        try: await query.edit_message_text("🚫 Akses ditolak.")
        except: pass
        return
    data = query.data
    try:
        if data == 'menu_utama': await send_menu_utama(query, context)
        elif data == 'akun_saya':
            _, s = await cek_saldo_api()
            stats = db_get_user_stats(user_id)
            teks = (f"💼 <b>Akun Saya</b>\n\n👤 Nama: {query.from_user.first_name or '-'}\n🆔 ID: <code>{user_id}</code>\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n💰 <b>Saldo Saya:</b> Rp {format_rupiah(s) if s >= 0 else '-'}\n"
                    f"📦 <b>Total Order:</b> {stats['total']}\n✅ <b>Selesai:</b> {stats['completed']}\n"
                    f"❌ <b>Dibatalkan:</b> {stats['cancelled']}\n💸 <b>Total Belanja:</b> Rp {format_rupiah(stats['spent'])}\n"
                    f"━━━━━━━━━━━━━━━━━━━━")
            await safe_edit(query, teks, InlineKeyboardMarkup(kb_akun()))

        elif data == 'cek_saldo':
            teks, saldo = await cek_saldo_api()
            await safe_edit(query, teks, InlineKeyboardMarkup(kb_akun()))
            if saldo >= 0 and saldo < LOW_BALANCE_THRESHOLD:
                await safe_send(context, query.message.chat_id, f"⚠️ <b>Saldo Rendah!</b>\n\nSaldo: <b>Rp {format_rupiah(saldo)}</b>")

        elif data == 'stats_saya':
            stats = db_get_user_stats(user_id)
            s = db_get_stats()
            teks = (f"📊 <b>Statistik Saya</b>\n\n📦 Total Order: <b>{stats['total']}</b>\n✅ Selesai: <b>{stats['completed']}</b>\n"
                    f"❌ Dibatalkan: <b>{stats['cancelled']}</b>\n💸 Total Belanja: <b>Rp {format_rupiah(stats['spent'])}</b>\n\n")
            if s:
                teks += (f"━━━━━━━━━━━━━━━━━━━━\n🌐 <b>Global:</b>\n📅 Hari ini: {s['today'][0]} order (Rp {format_rupiah(s['today'][1])})\n"
                         f"📆 Bulan ini: {s['month'][0]} order (Rp {format_rupiah(s['month'][1])})\n"
                         f"📈 Total: {s['total'][0]} order (Rp {format_rupiah(s['total'][1])})")
            await safe_edit(query, teks, InlineKeyboardMarkup(kb_akun()))

        elif data == 'aktif':
            await safe_edit(query, "📦 <b>Order Aktif</b>\n\nSilakan pilih aksi:", InlineKeyboardMarkup(kb_aktif_menu()))
        elif data == 'aktif_cancel': await send_active_orders_menu(query.message.chat_id, context, 'cancel', query=query)
        elif data == 'aktif_finish': await send_active_orders_menu(query.message.chat_id, context, 'finish', query=query)

        elif data == 'aktif_cancel_all':
            orders, err = await get_active_orders()
            if err:
                await safe_edit(query, f"❌ {err}", InlineKeyboardMarkup(kb_aktif_menu()))
                return
            to_cancel = [o for o in orders if not o.get("otp_code")]
            skipped = len(orders) - len(to_cancel)
            if not to_cancel:
                await safe_edit(query,
                    f"✅ <b>Tidak Ada yang Perlu Dibatalkan</b>\n\nSemua <b>{skipped}</b> nomor aktif sudah menerima OTP.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='aktif')]]))
                return
            await safe_edit(query,
                f"⚠️ <b>Konfirmasi Batalkan Massal</b>\n\n"
                f"🗑️ Akan dibatalkan: <b>{len(to_cancel)}</b> nomor (belum dapat OTP)\n"
                f"⏭️ Dilewati: <b>{skipped}</b> nomor (sudah dapat OTP)\n\n"
                f"💰 Saldo akan dikembalikan.\n\nLanjutkan?",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Ya, Batalkan Semua", callback_data='aktif_cancel_all_confirm')],
                    [InlineKeyboardButton("❌ Batal", callback_data='aktif')]
                ]))

        elif data == 'aktif_cancel_all_confirm':
            await safe_edit(query, "⏳ <b>Sedang membatalkan semua nomor...</b>\n\nMohon tunggu 1-3 menit.")
            asyncio.create_task(batch_cancel_task(context, query.message.chat_id, user_id))

        elif data == 'pengaturan':
            await safe_edit(query, "⚙️ <b>Pengaturan</b>\n\nSilakan pilih:", InlineKeyboardMarkup(kb_pengaturan()))

        elif data == 'notif_menu':
            watchlist = db_watchlist_get(user_id)
            if not watchlist:
                teks = "🔔 <b>Notifikasi Restock</b>\n\nBelum ada produk yang dipantau.\n\n💡 <i>Cara pakai: Saat stok habis, klik 🔔 Pantau di menu Beli Nomor.</i>"
            else:
                teks = f"🔔 <b>Notifikasi Restock</b>\n\nAnda memantau {len(watchlist)} produk:\n\n"
                for wid, pid, price in watchlist: teks += f"🔔 Gojek Rp {format_rupiah(price)}\n"
                teks += "\nBot akan kirim notif saat stok kembali."
            await safe_edit(query, teks, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='pengaturan')]]))

        elif data == 'template_menu':
            current_w = get_template("welcome", "(default)")
            current_o = get_template("otp_found", "(default)")
            teks = (f"💬 <b>Template Pesan</b>\n\n<b>1. Sambutan:</b>\n<i>{current_w[:100]}</i>\n"
                    f"Ubah: <code>/setwelcome Teks baru</code>\n\n<b>2. Template OTP:</b>\n<i>{current_o[:100]}</i>\n"
                    f"Ubah: <code>/setotp Teks dengan {{otp}}</code>")
            await safe_edit(query, teks, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='pengaturan')]]))

        elif data == 'backup_now':
            try:
                if os.path.exists(DB_PATH):
                    conn = get_db_connection()
                    try:
                        conn.execute("PRAGMA wal_checkpoint(FULL)")
                    finally:
                        conn.close()
                    with open(DB_PATH, 'rb') as f:
                        await context.bot.send_document(chat_id=query.message.chat_id, document=f,
                            filename=f"backup_manual_{time.strftime('%Y%m%d_%H%M')}.db", caption="🗄️ Backup Manual")
                    await query.answer("✅ Backup terkirim!", show_alert=True)
            except Exception as e: await query.answer(f"❌ Gagal: {str(e)[:50]}", show_alert=True)

        elif data == 'info_bantuan':
            await safe_edit(query, "ℹ️ <b>Info & Bantuan</b>\n\nPilih menu:", InlineKeyboardMarkup(kb_info_bantuan()))

        elif data == 'info_status':
            cek = await http_get_async(f"{BASE_URL}/balance")
            sms_status = "🟢 Terhubung" if cek.get("success") else "🔴 Bermasalah"
            db_status = "🟢 OK" if os.path.exists(DB_PATH) else "🔴 Error"
            stats = db_get_stats()
            teks = (f"📊 <b>Status Bot</b>\n\n🤖 <b>Nama:</b> ReivaldoNokos\n📦 <b>Versi:</b> {BOT_VERSION}\n"
                    f"🟢 <b>Status:</b> Online\n⏱️ <b>Uptime:</b> {get_uptime()}\n\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"📡 <b>Koneksi SMScode:</b> {sms_status}\n💾 <b>Database:</b> {db_status}\n🌐 <b>API:</b> api.smscode.gg\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n📊 Total Order: {stats['total'][0] if stats else 0}\n"
                    f"💸 Pengeluaran: Rp {format_rupiah(stats['total'][1]) if stats else 0}")
            await safe_edit(query, teks, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='info_bantuan')]]))

        elif data == 'info_faq':
            teks = (f"📖 <b>FAQ</b>\n\n<b>❓ Kenapa nomor tidak muncul OTP?</b>\n• Cek koneksi internet\n• Pastikan nomor benar\n"
                    f"• Tunggu 5-10 menit\n• Jika lewat {TIMEOUT_MINUTES} menit, nomor otomatis dibatalkan\n\n"
                    f"<b>❓ Kenapa stok habis?</b>\n• Klik 🔔 Pantau untuk notif saat restock\n\n"
                    f"<b>❓ Bagaimana cara top-up saldo?</b>\n• Hubungi admin\n\n"
                    f"<b>❓ Apakah saldo bisa hangus?</b>\n• Tidak, saldo dikembalikan otomatis")
            await safe_edit(query, teks, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='info_bantuan')]]))

        elif data == 'info_kontak':
            teks = f"📞 <b>Kontak Admin</b>\n\n👤 <b>Telegram:</b> @mreivaldoo\n💬 <b>Jam Operasional:</b> 24/7"
            await safe_edit(query, teks, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='info_bantuan')]]))

        elif data == 'favorit':
            watchlist = db_watchlist_get(user_id)
            if not watchlist:
                teks = "⭐ <b>Favorit</b>\n\nBelum ada produk favorit."
            else:
                teks = f"⭐ <b>Favorit</b>\n\nAnda memantau {len(watchlist)} produk:\n\n"
                for wid, pid, price in watchlist: teks += f"⭐ Gojek Rp {format_rupiah(price)}\n"
            await safe_edit(query, teks, InlineKeyboardMarkup(kb_favorit()))

        elif data == 'riwayat_menu':
            await safe_edit(query, "📜 <b>Riwayat Order</b>\n\nPilih rentang waktu:", InlineKeyboardMarkup(kb_riwayat()))

        elif data.startswith('riwayat_'):
            filter_type = data.replace('riwayat_', '')
            label = {"today": "Hari Ini", "week": "7 Hari Terakhir", "month": "Bulan Ini", "all": "Semua"}.get(filter_type, "Semua")
            rows = db_get_history(user_id, 20, filter_type)
            if not rows:
                teks = f"📜 <b>Riwayat ({label})</b>\n\nBelum ada transaksi."
            else:
                teks = f"📜 <b>Riwayat ({label})</b>\n\nMenampilkan {len(rows)} transaksi:\n\n"
                for oid, phone, status, otp, created in rows:
                    emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else ("❌" if status == "CANCELLED" else "⏳"))
                    teks += f"{emoji} <code>{format_phone(phone)}</code>\n   {status} | OTP: {otp or '-'}\n   {created}\n\n"
            await safe_edit(query, teks[:4000], InlineKeyboardMarkup(kb_riwayat()))

        elif data == 'beli_nomor': await send_products_menu(query.message.chat_id, context, query=query)
        elif data == 'bulk_menu': await send_bulk_menu(query.message.chat_id, context, query=query)

        elif data.startswith('bulkprice_'):
            parsed = safe_parse_callback(data, 'bulkprice_', 2)
            if not parsed:
                await query.answer("❌ Data tidak valid", show_alert=True); return
            pid, harga = parsed
            _, saldo = await cek_saldo_api()
            teks = (f"📦 <b>Beli Banyak — Rp {format_rupiah(harga)}</b>\n\n"
                    f"💰 Saldo: <b>Rp {format_rupiah(saldo) if saldo >= 0 else '?'}</b>\n"
                    f"💵 Harga satuan: <b>Rp {format_rupiah(harga)}</b>\n\n"
                    f"Perkiraan total:\n"
                    f"• 5 = Rp {format_rupiah(harga * 5)}\n"
                    f"• 10 = Rp {format_rupiah(harga * 10)}\n"
                    f"• 20 = Rp {format_rupiah(harga * 20)}\n"
                    f"• 30 = Rp {format_rupiah(harga * 30)}\n"
                    f"• 40 = Rp {format_rupiah(harga * 40)}\n"
                    f"• 50 = Rp {format_rupiah(harga * 50)}\n"
                    f"• 100 = Rp {format_rupiah(harga * 100)}")
            await safe_edit(query, teks, InlineKeyboardMarkup(kb_bulk_qty(pid, harga)))

        elif data.startswith('bulkqty_'):
            parsed = safe_parse_callback(data, 'bulkqty_', 3)
            if not parsed:
                await query.answer("❌ Data tidak valid", show_alert=True); return
            pid, harga, qty = parsed
            # Cek user sudah punya bulk aktif
            now = time.time()
            if user_id in USER_BULK_LOCKS and now - USER_BULK_LOCKS[user_id] < 60:
                await query.answer("⚠️ Anda masih punya bulk order yang sedang diproses!", show_alert=True)
                return
            USER_BULK_LOCKS[user_id] = now
            total = harga * qty
            total_batches = (qty + BATCH_SIZE - 1) // BATCH_SIZE
            queue_info = ""
            if BULK_SEMAPHORE.locked():
                queue_info = "\n⏳ <i>Ada user lain sedang bulk buy. Anda masuk antrean, mohon tunggu...</i>"
            await safe_edit(query,
                f"⏳ Membeli <b>{qty} nomor</b>...\n💵 Total: Rp {format_rupiah(total)}\n"
                f"📦 Diproses dalam <b>{total_batches} batch</b> × 10 nomor\n{queue_info}\n"
                f"<i>Mohon tunggu, proses ini butuh 15-60 detik.</i>")
            results, failed_count, err = await beli_banyak_api_async(pid, user_id, harga, qty)
            if results:
                ordered_list = [(oid, format_phone(n)) for oid, n in results]
                summary_teks = build_bulk_summary(ordered_list, set(), harga, TIMEOUT_SECONDS, failed_count)
                kb = [[InlineKeyboardButton("📦 Order Aktif", callback_data='aktif')],
                      [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                try:
                    msg = await query.edit_message_text(summary_teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                except:
                    msg = await safe_send(context, query.message.chat_id, summary_teks, InlineKeyboardMarkup(kb))
                if msg:
                    batch_id = f"{user_id}_{int(time.time())}"
                    asyncio.create_task(bulk_monitor_task(context, query.message.chat_id, batch_id, ordered_list, harga, msg.message_id, failed_count))
            else:
                kb = [[InlineKeyboardButton("🔙 Kembali", callback_data='bulk_menu')],
                      [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                await safe_edit(query, f"❌ {err}", InlineKeyboardMarkup(kb))

        elif data.startswith('watch_'):
            parsed = safe_parse_callback(data, 'watch_', 2)
            if not parsed:
                await query.answer("❌ Data tidak valid", show_alert=True); return
            pid, price = parsed
            if db_watchlist_add(user_id, pid, price):
                await safe_edit(query,
                    f"🔔 <b>Dipantau!</b>\n\nProduk Gojek Rp {format_rupiah(price)} akan dipantau.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]]))
            else: await query.answer("❌ Gagal tambah", show_alert=True)

        elif data == 'stok_habis':
            await query.answer("❌ Stok habis. Pilih harga lain.", show_alert=True)

        elif data.startswith('buy_'):
            parsed = safe_parse_callback(data, 'buy_', 2)
            if not parsed:
                await query.answer("❌ Data tidak valid", show_alert=True); return
            pid, harga = parsed
            await safe_edit(query, "⏳ Sedang membeli nomor...")
            order_id, nomor_raw = await beli_nomor_api(pid, user_id, harga)
            if order_id:
                nomor_display = format_phone(nomor_raw)
                kb = [[InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
                      [InlineKeyboardButton("🔄 Cek OTP Sekarang", callback_data=f"checkotp_{order_id}")],
                      [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                teks = (f"🛒 <b>Nomor berhasil dibeli!</b>\n\n📱 Nomor: <code>{nomor_display}</code>\n🆔 ID: <code>{order_id}</code>\n\n"
                        f"📋 <b>Langkah selanjutnya:</b>\n1️⃣ Salin nomor\n2️⃣ Masukkan ke aplikasi Gojek\n3️⃣ Ketuk <b>Cek OTP Sekarang</b>\n\n"
                        f"⚠️ OTP tidak masuk dalam {TIMEOUT_MINUTES} menit? Nomor otomatis dibatalkan.")
                await safe_edit(query, teks, InlineKeyboardMarkup(kb))
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display))
            else:
                await safe_edit(query, f"❌ {nomor_raw}", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')], [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

        elif data.startswith('checkotp_'):
            order_id = data.replace('checkotp_', '')
            cek = await http_get_async(f"{BASE_URL}/orders/{order_id}")
            otp_s = None; status_s = None
            if cek.get("success"): otp_s = cek["data"].get("otp_code"); status_s = cek["data"].get("status")
            if otp_s:
                tmpl = get_template("otp_found", "🔑 <b>Kode OTP Ditemukan!</b>\n\n🔑 Kode: <code>{otp}</code>")
                teks = tmpl.replace("{otp}", otp_s)
                await safe_edit(query, teks, InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")], [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
            elif status_s in ["CANCELLED", "EXPIRED", "COMPLETED"]:
                try: await query.message.delete()
                except: pass
            else:
                kb = [[InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")], [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                teks = f"⏳ <b>Status:</b> Menunggu SMS masuk\n⏱️ Sisa waktu: <b>{TIMEOUT_MINUTES}:00</b>\n\nBot akan otomatis mengubah pesan ini menjadi kode OTP."
                await safe_edit(query, teks, InlineKeyboardMarkup(kb))
                pending_messages[order_id] = query.message.message_id
                pending_messages_ts[order_id] = time.time()

        elif data.startswith('cancel_'):
            order_id = data.replace('cancel_', '')
            try: await query.message.delete()
            except: pass
            hasil = await batal_order_api(order_id)
            pending_messages.pop(order_id, None)
            pending_messages_ts.pop(order_id, None)
            if hasil["success"]:
                teks = (f"✅ <b>Berhasil Dibatalkan</b>\n\nID: <code>{order_id}</code>\n"
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
            hasil = await selesai_order_api(order_id)
            pending_messages.pop(order_id, None)
            pending_messages_ts.pop(order_id, None)
            if hasil["success"]:
                teks = (f"✅ <b>Berhasil Diselesaikan</b>\n\nID: <code>{order_id}</code>\n"
                        f"Nomor bisa digunakan di aplikasi Gojek.\n\n"
                        f"💬 <i>Pesan ini akan hilang dalam 10 detik.</i>")
            else:
                teks = (f"❌ <b>Gagal Menyelesaikan</b>\n\nID: <code>{order_id}</code>\nAlasan: {hasil['message']}\n\n"
                        f"💬 <i>Pesan ini akan hilang dalam 10 detik.</i>")
            await send_auto_delete_message(context, query.message.chat_id, teks)

    except Exception as e:
        await log_error(context, f"Button error: {str(e)[:200]}", user_id)
        try:
            await query.edit_message_text("⚠️ Terjadi kesalahan. Coba lagi.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
        except: pass

# --- COMMAND HANDLERS ---
async def saldo_command(update, context):
    teks, saldo = await cek_saldo_api()
    await safe_send(context, update.effective_chat.id, teks, InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
    if saldo >= 0 and saldo < LOW_BALANCE_THRESHOLD:
        await safe_send(context, update.effective_chat.id, f"⚠️ <b>Saldo Rendah!</b>\n\nSaldo: <b>Rp {format_rupiah(saldo)}</b>")

async def beli_command(update, context): await send_products_menu(update.effective_chat.id, context)
async def batal_command(update, context): await send_active_orders_menu(update.effective_chat.id, context, 'cancel')
async def selesai_command(update, context): await send_active_orders_menu(update.effective_chat.id, context, 'finish')
async def aktif_command(update, context): await send_active_orders_menu(update.effective_chat.id, context, 'cancel')

async def riwayat_command(update, context):
    rows = db_get_history(update.effective_user.id, 10, "all")
    if not rows:
        await safe_send(context, update.effective_chat.id, "📜 Belum ada riwayat.", InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
        return
    teks = "📜 <b>10 Transaksi Terakhir:</b>\n\n"
    for oid, phone, status, otp, created in rows:
        emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else "⏳")
        teks += f"{emoji} <code>{format_phone(phone)}</code>\n   {status} | OTP: {otp or '-'}\n   {created}\n\n"
    await safe_send(context, update.effective_chat.id, teks, InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def stats_command(update, context):
    s = db_get_stats()
    if not s:
        await safe_send(context, update.effective_chat.id, "⚠️ Gagal ambil statistik."); return
    teks = (f"📊 <b>Statistik Global Bot</b>\n\n📅 <b>Hari Ini:</b> {s['today'][0]} order | Rp {format_rupiah(s['today'][1])}\n"
            f"📆 <b>Bulan Ini:</b> {s['month'][0]} order | Rp {format_rupiah(s['month'][1])}\n"
            f"📈 <b>Total:</b> {s['total'][0]} order | Rp {format_rupiah(s['total'][1])}\n\n📋 <b>Status:</b>\n")
    for st, c in s['statuses'].items(): teks += f"   • {st}: {c}\n"
    await safe_send(context, update.effective_chat.id, teks, InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def refund_command(update, context):
    if not is_admin(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, "🚫 Akses ditolak."); return
    r = db_get_refund_stats()
    if not r:
        await safe_send(context, update.effective_chat.id, "⚠️ Gagal ambil data refund."); return
    teks = (f"💸 <b>Statistik Refund</b>\n\n"
            f"📅 Hari Ini: {r['today'][0]} order | Rp {format_rupiah(r['today'][1])}\n"
            f"📆 Bulan Ini: {r['month'][0]} order | Rp {format_rupiah(r['month'][1])}\n"
            f"📈 Total: {r['total'][0]} order | Rp {format_rupiah(r['total'][1])}")
    await safe_send(context, update.effective_chat.id, teks, InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def logs_command(update, context):
    if not is_admin(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, "🚫 Akses ditolak."); return
    conn = None
    try:
        conn = get_db_connection()
        rows = conn.execute("SELECT timestamp, level, message, user_id FROM logs ORDER BY id DESC LIMIT 20").fetchall()
    except: rows = []
    finally:
        if conn: conn.close()
    if not rows:
        await safe_send(context, update.effective_chat.id, "📜 Belum ada log.", InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]])); return
    teks = "📜 <b>20 Log Error Terakhir:</b>\n\n"
    for ts, lvl, msg, uid in rows:
        emoji = "🔴" if lvl == "ERROR" else "🟡"
        teks += f"{emoji} <code>{ts}</code>\n{msg[:200]}\n"
        if uid: teks += f"User: <code>{uid}</code>\n"
        teks += "\n"
    await safe_send(context, update.effective_chat.id, teks[:4000], InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def setwelcome_command(update, context):
    if not is_admin(update.effective_user.id): await safe_send(context, update.effective_chat.id, "🚫 Akses ditolak."); return
    if not context.args: await safe_send(context, update.effective_chat.id, "⚠️ Format: /setwelcome Teks baru"); return
    teks = " ".join(context.args)
    if set_template("welcome", teks): await safe_send(context, update.effective_chat.id, f"✅ Sambutan diubah:\n\n{teks}")
    else: await safe_send(context, update.effective_chat.id, "⚠️ Gagal simpan.")

async def setotp_command(update, context):
    if not is_admin(update.effective_user.id): await safe_send(context, update.effective_chat.id, "🚫 Akses ditolak."); return
    if not context.args: await safe_send(context, update.effective_chat.id, "⚠️ Format: /setotp Teks dengan {otp}"); return
    teks = " ".join(context.args)
    if set_template("otp_found", teks): await safe_send(context, update.effective_chat.id, "✅ Template OTP diubah.")
    else: await safe_send(context, update.effective_chat.id, "⚠️ Gagal simpan.")

async def backup_command(update, context):
    if not is_admin(update.effective_user.id): await safe_send(context, update.effective_chat.id, "🚫 Akses ditolak."); return
    try:
        if os.path.exists(DB_PATH):
            conn = get_db_connection()
            try:
                conn.execute("PRAGMA wal_checkpoint(FULL)")
            finally:
                conn.close()
            with open(DB_PATH, 'rb') as f:
                await context.bot.send_document(chat_id=update.effective_chat.id, document=f,
                    filename=f"backup_{time.strftime('%Y%m%d_%H%M')}.db", caption="🗄️ Backup Manual")
    except Exception as e: await safe_send(context, update.effective_chat.id, f"⚠️ Gagal: {str(e)[:100]}")

# --- COMMAND MULTI-USER ---
async def adduser_command(update, context):
    if not is_admin(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, "🚫 Hanya admin."); return
    if not context.args:
        await safe_send(context, update.effective_chat.id, "⚠️ Format: /adduser [ID_Telegram]"); return
    new_id = context.args[0].strip()
    if new_id in ALLOWED_USERS:
        await safe_send(context, update.effective_chat.id, f"ℹ️ ID {new_id} sudah ada."); return
    ALLOWED_USERS.append(new_id)
    set_template("allowed_users", ",".join(ALLOWED_USERS))
    await safe_send(context, update.effective_chat.id, f"✅ ID {new_id} ditambahkan.\nTotal user: {len(ALLOWED_USERS)}")

async def removeuser_command(update, context):
    if not is_admin(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, "🚫 Hanya admin."); return
    if not context.args:
        await safe_send(context, update.effective_chat.id, "⚠️ Format: /removeuser [ID]"); return
    rem_id = context.args[0].strip()
    if rem_id == str(ADMIN_ID):
        await safe_send(context, update.effective_chat.id, "❌ Tidak bisa menghapus admin."); return
    if rem_id in ALLOWED_USERS:
        ALLOWED_USERS.remove(rem_id)
        set_template("allowed_users", ",".join(ALLOWED_USERS))
        await safe_send(context, update.effective_chat.id, f"✅ ID {rem_id} dihapus.\nTotal user: {len(ALLOWED_USERS)}")
    else:
        await safe_send(context, update.effective_chat.id, f"❌ ID {rem_id} tidak ditemukan.")

async def listuser_command(update, context):
    if not is_admin(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, "🚫 Hanya admin."); return
    teks = "👥 <b>Daftar User:</b>\n\n"
    for i, uid in enumerate(ALLOWED_USERS, 1):
        role = "👑 Admin" if str(uid) == str(ADMIN_ID) else "👤 User"
        teks += f"{i}. <code>{uid}</code> {role}\n"
    await safe_send(context, update.effective_chat.id, teks)

# --- MAIN ---
async def post_init(application):
    global MESSAGE_QUEUE, BULK_SEMAPHORE, API_RATE_LOCK
    # Init semaphore & lock di dalam event loop
    BULK_SEMAPHORE = asyncio.Semaphore(1)
    API_RATE_LOCK = asyncio.Lock()
    MESSAGE_QUEUE = asyncio.Queue(maxsize=MESSAGE_QUEUE_MAXSIZE)
    # Start 2 worker
    for _ in range(2):
        task = asyncio.create_task(message_worker(application))
        MESSAGE_WORKERS.append(task)

    commands = [
        BotCommand("start", "🚀 Menu utama"), BotCommand("beli", "🛒 Beli nomor Gojek"),
        BotCommand("saldo", "💰 Cek saldo"), BotCommand("aktif", "📦 Nomor aktif"),
        BotCommand("riwayat", "📜 Riwayat transaksi"), BotCommand("stats", "📊 Statistik"),
        BotCommand("refund", "💸 Statistik refund"),
        BotCommand("batal", "❌ Batalkan nomor"), BotCommand("selesai", "✅ Selesaikan nomor"),
        BotCommand("help", "❓ Bantuan"), BotCommand("logs", "📜 Log error (admin)"),
        BotCommand("adduser", "➕ Tambah user (admin)"),
        BotCommand("removeuser", "➖ Hapus user (admin)"),
        BotCommand("listuser", "👥 Daftar user (admin)"),
    ]
    try: await application.bot.set_my_commands(commands)
    except Exception as e: print(f"Set commands error: {e}")
    asyncio.create_task(auto_backup_task(application))
    asyncio.create_task(restock_checker_task(application))
    asyncio.create_task(cleanup_task())

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    for cmd, fn in [("start", start), ("help", help_command), ("saldo", saldo_command),
                    ("beli", beli_command), ("batal", batal_command), ("selesai", selesai_command),
                    ("aktif", aktif_command), ("riwayat", riwayat_command), ("stats", stats_command),
                    ("refund", refund_command), ("logs", logs_command),
                    ("setwelcome", setwelcome_command), ("setotp", setotp_command), ("backup", backup_command),
                    ("adduser", adduser_command), ("removeuser", removeuser_command), ("listuser", listuser_command)]:
        application.add_handler(CommandHandler(cmd, fn))
    application.add_handler(CallbackQueryHandler(button_handler))
    print("✅ Bot v3.0 berjalan dengan multi-user & anti-error...")
    application.run_polling()
