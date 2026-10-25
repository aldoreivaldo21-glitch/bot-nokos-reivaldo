import os
import json
import time
import sqlite3
import urllib.request
import urllib.error
import asyncio
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# --- KONFIGURASI ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597")

_raw_key = os.environ.get("SMScode_API_KEY") or os.environ.get("SMSCODE_API_KEY") or ""
SMScode_API_KEY = _raw_key.strip()

BOT_VERSION = "10.1 Fixed"
START_TIME = time.time()
MAX_BULK = 100
BATCH_SIZE = 10
TIMEOUT_MINUTES = 20
TIMEOUT_SECONDS = TIMEOUT_MINUTES * 60

BASE_URL = "https://api.smscode.gg/v1"
HEADERS = {
    "Authorization": f"Bearer {SMScode_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")
pending_messages = {}

# --- HELPER ---
def format_rupiah(n):
    try: return f"{int(n):,}".replace(",", ".")
    except: return str(n)

def format_countdown(seconds):
    if seconds < 0: seconds = 0
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{m:02d}:{s:02d}"

def format_phone(p):
    if p and p.startswith("62"): return p[2:]
    return p

def is_admin(uid): return str(uid) == str(ADMIN_ID)

# --- FUNGSI YANG SEMPAT HILANG ---
def get_uptime():
    secs = int(time.time() - START_TIME)
    days = secs // 86400; hours = (secs % 86400) // 3600; mins = (secs % 3600) // 60
    if days > 0: return f"{days}h {hours}j {mins}m"
    elif hours > 0: return f"{hours}j {mins}m"
    return f"{mins}m"
# --------------------------------

# --- DATABASE ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY, user_id INTEGER, phone TEXT, product_id INTEGER,
        price INTEGER, status TEXT, otp TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
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

def db_get_refund_stats():
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
        if e.code == 401: return {"error": "API Key salah atau tidak valid. Cek kembali di ZevCloud."}
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
        if e.code == 401: return {"error": "API Key salah atau tidak valid. Cek kembali di ZevCloud."}
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
        except: return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e: return {"error": str(e)}

def translate_error(err_msg):
    e = err_msg.lower()
    if "unauthorized" in e: return "🔐 API Key salah atau tidak valid."
    if "insufficient_balance" in e: return "💸 Saldo tidak cukup."
    if "no numbers available" in e: return "📭 Stok habis."
    if "cancel_too_early" in e: return "⏳ Tunggu minimal 2 menit sebelum membatalkan."
    if "cannot cancel order" in e: return "⚠️ Order sudah menerima OTP atau selesai, tidak bisa dibatalkan."
    if "timeout" in e: return "🌐 Koneksi timeout. Coba lagi."
    return f"⚠️ {err_msg[:100]}"

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
    if not SMScode_API_KEY: return "❌ API Key belum diatur di ZevCloud.", -1
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
            conn.execute("UPDATE orders SET status='CANCELLED', refund_amount=?, refunded_at=CURRENT_TIMESTAMP WHERE order_id=?", (refund_amount, str(order_id)))
            conn.commit(); conn.close()
        except Exception as e:
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
# === BULK SUMMARY BUILDER ===
# ============================================================
def build_bulk_summary(ordered_list, received_set, price_per_unit, remaining_seconds, failed_count=0):
    total = len(ordered_list)
    jumlah_dapat = len(received_set)
    timer_str = format_countdown(remaining_seconds)
    text = f"🛒 <b>Berhasil Beli {total} Nomor!</b>\n"
    if failed_count > 0: text += f"⚠️ Gagal: <b>{failed_count}</b> nomor\n"
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
             f"3️⃣ Klik ✅ Selesai setelah OTP dipakai")
    return text

# ============================================================
# === SINGLE ORDER POLLING (CEPAT 2 DETIK) ===
# ============================================================
async def auto_poll_otp(chat_id, context, order_id, nomor_display, pid, price):
    total = TIMEOUT_SECONDS
    elapsed = 0
    msg_id = None
    
    while elapsed < total:
        await asyncio.sleep(2) # Jeda 2 detik
        elapsed += 2
        remaining = total - elapsed
        
        if order_id not in pending_messages and msg_id is not None:
            return

        data = http_get(f"{BASE_URL}/orders/{order_id}")
        if not data.get("success"): continue
        
        od = data["data"]; otp = od.get("otp_code"); status = od.get("status")
        
        if otp:
            db_update_order(order_id, "OTP_RECEIVED", otp)
            tmpl = get_template("otp_found", "🔑 <b>Kode OTP Ditemukan!</b>\n\n📱 Nomor: <code>{nomor}</code>\n🔑 Kode: <code>{otp}</code>")
            teks = tmpl.replace("{otp}", otp).replace("{nomor}", nomor_display)
            
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
            
        elif status == "ACTIVE" and msg_id and elapsed % 10 == 0:
            try:
                kb = [
                    [InlineKeyboardButton("🔄 Ganti Nomor", callback_data=f"replace_{order_id}_{pid}_{price}")],
                    [InlineKeyboardButton("❌ Batalkan", callback_data=f"cancel_{order_id}")]
                ]
                await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                    text=f"⏳ <b>Status:</b> Menunggu SMS...\n⏱️ Sisa waktu: <b>{format_countdown(remaining)}</b>",
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
            except: pass
            
        elif status == "ACTIVE" and not msg_id:
            try:
                kb = [
                    [InlineKeyboardButton("🔄 Ganti Nomor", callback_data=f"replace_{order_id}_{pid}_{price}")],
                    [InlineKeyboardButton("❌ Batalkan", callback_data=f"cancel_{order_id}")]
                ]
                msg = await context.bot.send_message(chat_id=chat_id,
                    text=f"🛒 <b>Nomor:</b> <code>{nomor_display}</code>\n⏳ <b>Status:</b> Menunggu SMS...\n\n<i>Bot akan mengirim OTP otomatis ke sini.</i>",
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                msg_id = msg.message_id
                pending_messages[order_id] = msg_id
            except: pass

    batal_order_api(order_id)
    if msg_id:
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="⌛ <b>Waktu Habis</b>\nNomor otomatis dibatalkan (Refund).", parse_mode='HTML')
        except: pass

# ============================================================
# === BULK MONITOR (CEPAT) ===
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
        except Exception: pass

    await update_summary()
    
    while pending and (time.time() - start_time) < timeout_seconds:
        await asyncio.sleep(3) 
        if not pending: break
        
        data = http_get(f"{BASE_URL}/orders/active")
        active_map = {}
        if data.get("success"):
            for o in data.get("data", []): active_map[str(o.get("id"))] = o
            
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
                        text=(f"🔑 <b>OTP Diterima!</b>\n\n📱 Nomor: <code>{phone}</code>\n🔑 Kode: <code>{otp}</code>\n\n📊 Progress: <b>{len(received_set)}/{total}</b>"),
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
            await context.bot.send_message(chat_id, f"✅ Tidak ada nomor yang perlu dibatalkan.\n\n📭 Semua {skipped} nomor aktif sudah menerima OTP.", parse_mode='HTML')
        except: pass
        return
        
    success = 0; failed = 0; refund_total = 0
    for o in to_cancel:
        oid = str(o.get("id"))
        hasil = batal_order_api(oid)
        if hasil["success"]:
            success += 1; refund_total += hasil.get("refund_amount", 0)
        else:
            failed += 1
        await asyncio.sleep(0.3)
        
    teks = (f"✅ <b>Batalkan Massal Selesai</b>\n\n"
            f"📊 Diperiksa: <b>{len(orders)}</b>\n"
            f"🗑️ Dibatalkan: <b>{success}</b>\n"
            f"⏭️ Dilewati (OTP): <b>{skipped}</b>\n"
            f"❌ Gagal: <b>{failed}</b>\n\n"
            f"💰 <b>Refund: Rp {format_rupiah(refund_total)}</b>")
    try:
        await context.bot.send_message(chat_id, teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))
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
                            await context.bot.send_message(chat_id=uid, text=f"🎉 <b>Stok Kembali Tersedia!</b>\n\n📦 Gojek\n💵 Harga: Rp {format_rupiah(price)}\n📊 Stok: {p.get('available')}\n\nSegera beli!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Beli Sekarang", callback_data='beli_nomor')]]), parse_mode='HTML')
                        except: pass
                        db_watchlist_remove_by_id(wid)
                        break
        except Exception as e: print(f"Restock error: {e}")

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
        [InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]
    ]

def kb_aktif_menu():
    return [
        [InlineKeyboardButton("❌ Batalkan 1 Nomor", callback_data='aktif_cancel')],
        [InlineKeyboardButton("✅ Selesaikan 1 Nomor", callback_data='aktif_finish')],
        [InlineKeyboardButton("🗑️ Batalkan Semua (Tanpa OTP)", callback_data='aktif_cancel_all')],
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
            f"• Pembelian dipecah jadi batch 10 nomor\n"
            f"• Timer live {TIMEOUT_MINUTES} menit\n"
            f"• Auto-cancel jika {TIMEOUT_MINUTES} menit tidak ada OTP")
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

async def help_command(update, context):
    await update.message.reply_text("Gunakan menu di bawah ini:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

async def button_handler(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.answer("🚫 Akses ditolak.", show_alert=True)
        return

    data = query.data
    try:
        if data.startswith(('cancel_', 'finish_', 'menu_', 'beli_', 'bulk_', 'aktif_', 'info_', 'riwayat_', 'akun_')):
            try: await query.answer()
            except: pass
        
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
            teks, _ = cek_saldo_api()
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_akun()), parse_mode='HTML')

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
                await query.edit_message_text(f"❌ {err}", reply_markup=InlineKeyboardMarkup(kb_aktif_menu()))
                return
            to_cancel = [o for o in orders if not o.get("otp_code")]
            skipped = len(orders) - len(to_cancel)
            if not to_cancel:
                await query.edit_message_text(f"✅ <b>Tidak Ada yang Perlu Dibatalkan</b>\n\nSemua <b>{skipped}</b> nomor aktif sudah menerima OTP.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='aktif')]]), parse_mode='HTML')
                return
            await query.edit_message_text(f"⚠️ <b>Batalkan Massal</b>\n\nAkan dibatalkan: <b>{len(to_cancel)}</b> (belum OTP)\nDilewati: <b>{skipped}</b> (sudah OTP)\n\nLanjutkan?",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Ya, Batalkan", callback_data='aktif_cancel_all_confirm')], [InlineKeyboardButton("❌ Batal", callback_data='aktif')]]), parse_mode='HTML')

        elif data == 'aktif_cancel_all_confirm':
            await query.edit_message_text("⏳ <b>Sedang membatalkan semua nomor...</b>", parse_mode='HTML')
            asyncio.create_task(batch_cancel_task(context, query.message.chat_id, user_id))

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
            teks = (f"📖 <b>FAQ</b>\n\n<b>❓ Kenapa nomor tidak muncul OTP?</b>\n• Cek koneksi internet\n• Tunggu 5-10 menit\n• Jika lewat {TIMEOUT_MINUTES} menit, otomatis dibatalkan & saldo kembali\n\n"
                    f"<b>❓ Apakah saldo bisa hangus?</b>\n• Tidak, saldo dikembalikan otomatis.")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='info_bantuan')]]), parse_mode='HTML')

        elif data == 'info_kontak':
            teks = (f"📞 <b>Kontak Admin</b>\n\n👤 <b>Telegram:</b> @mreivaldoo\n💬 <b>Jam Operasional:</b> 24/7")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='info_bantuan')]]), parse_mode='HTML')

        elif data == 'favorit':
            watchlist = db_watchlist_get(user_id)
            if not watchlist:
                teks = "⭐ <b>Favorit</b>\n\nBelum ada produk favorit.\n\n💡 Tambah dari menu <b>🛒 Beli Nomor</b> (klik 🔔 saat stok habis)."
            else:
                teks = f"⭐ <b>Favorit</b>\n\nAnda memantau {len(watchlist)} produk:\n\n"
                for wid, pid, price in watchlist: teks += f"⭐ Gojek Rp {format_rupiah(price)}\n"
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_favorit()), parse_mode='HTML')

        elif data == 'riwayat_menu':
            await query.edit_message_text("📜 <b>Riwayat Order</b>\n\nPilih rentang waktu:", reply_markup=InlineKeyboardMarkup(kb_riwayat()), parse_mode='HTML')

        elif data.startswith('riwayat_'):
            filter_type = data.replace('riwayat_', '')
            label = {"today": "Hari Ini", "week": "7 Hari", "month": "Bulan Ini", "all": "Semua"}.get(filter_type, "Semua")
            rows = db_get_history(user_id, 20, filter_type)
            if not rows: teks = f"📜 <b>Riwayat ({label})</b>\n\nBelum ada transaksi."
            else:
                teks = f"📜 <b>Riwayat ({label})</b>\n\n"
                for oid, phone, status, otp, created in rows:
                    emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else ("❌" if status == "CANCELLED" else "⏳"))
                    teks += f"{emoji} <code>{format_phone(phone)}</code> | {status} | {otp or '-'}\n"
            await query.edit_message_text(teks[:4000], reply_markup=InlineKeyboardMarkup(kb_riwayat()), parse_mode='HTML')

        elif data == 'beli_nomor': await send_products_menu(query.message.chat_id, context, query=query)
        elif data == 'bulk_menu': await send_bulk_menu(query.message.chat_id, context, query=query)

        elif data.startswith('bulkprice_'):
            parts = data.replace('bulkprice_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1])
            _, saldo = cek_saldo_api()
            teks = (f"📦 <b>Beli Banyak — Rp {format_rupiah(harga)}</b>\n\n"
                    f"💰 Saldo: <b>Rp {format_rupiah(saldo) if saldo >= 0 else '?'}</b>\n\n"
                    f"Pilih jumlah:")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_bulk_qty(pid, harga)), parse_mode='HTML')

        elif data.startswith('bulkqty_'):
            parts = data.replace('bulkqty_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1]); qty = int(parts[2])
            total = harga * qty
            await query.edit_message_text(f"⏳ Membeli <b>{qty} nomor</b>...\nTotal: Rp {format_rupiah(total)}", parse_mode='HTML')
            results, failed_count, err = await beli_banyak_api_async(pid, user_id, harga, qty)
            if results:
                ordered_list = [(oid, format_phone(n)) for oid, n in results]
                summary_teks = build_bulk_summary(ordered_list, set(), harga, TIMEOUT_SECONDS, failed_count)
                kb = [[InlineKeyboardButton("📦 Order Aktif", callback_data='aktif')], [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]
                msg = await query.edit_message_text(summary_teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                batch_id = f"{user_id}_{int(time.time())}"
                asyncio.create_task(bulk_monitor_task(context, query.message.chat_id, batch_id, ordered_list, harga, msg.message_id, failed_count))
            else:
                await query.edit_message_text(f"❌ {err}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='bulk_menu')]]))

        elif data.startswith('watch_'):
            parts = data.replace('watch_', '').split('_')
            pid = int(parts[0]); price = int(parts[1])
            if db_watchlist_add(user_id, pid, price):
                await query.edit_message_text(f"🔔 <b>Dipantau!</b>\n\nProduk Gojek Rp {format_rupiah(price)} akan dipantau.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]]), parse_mode='HTML')
            else: await query.answer("❌ Gagal tambah", show_alert=True)

        elif data.startswith('buy_'):
            parts = data.replace('buy_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1])
            await query.edit_message_text("⏳ Sedang membeli nomor...")
            order_id, nomor_raw = beli_nomor_api(pid, user_id, harga)
            if order_id:
                nomor_display = format_phone(nomor_raw)
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display, pid, harga))
                await query.message.delete()
            else:
                await query.edit_message_text(f"❌ {nomor_raw}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]]))

        elif data.startswith('replace_'):
            parts = data.replace('replace_', '').split('_')
            old_oid, pid, harga = parts[0], int(parts[1]), int(parts[2])
            
            await query.edit_message_text("🔄 Membatalkan nomor lama & membeli baru...", parse_mode='HTML')
            await batal_order_api(old_oid)
            pending_messages.pop(old_oid, None)
            
            order_id, nomor_raw = beli_nomor_api(pid, user_id, harga)
            if order_id:
                nomor_display = format_phone(nomor_raw)
                await query.message.delete()
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display, pid, harga))
            else:
                await query.edit_message_text(f"❌ Gagal ganti nomor: {nomor_raw}")

        elif data.startswith('cancel_'):
            order_id = data.replace('cancel_', '')
            try: await query.message.delete()
            except: pass
            hasil = batal_order_api(order_id)
            pending_messages.pop(order_id, None)
            
            if hasil["success"]:
                await query.answer(f"✅ Berhasil Dibatalkan\n💰 Refund: Rp {format_rupiah(hasil.get('refund_amount', 0))}", show_alert=True)
            else:
                await query.answer(f"❌ Gagal Batal: {hasil['message'][:50]}", show_alert=True)

        elif data.startswith('finish_'):
            order_id = data.replace('finish_', '')
            try: await query.message.delete()
            except: pass
            hasil = selesai_order_api(order_id)
            pending_messages.pop(order_id, None)
            
            if hasil["success"]:
                await query.answer("✅ Selesai & Nomor berhasil dilepas.", show_alert=True)
            else:
                await query.answer(f"❌ Gagal: {hasil['message'][:50]}", show_alert=True)

    except Exception as e:
        db_save_log("ERROR", str(e), user_id)
        try: await query.answer("⚠️ Terjadi kesalahan sistem.", show_alert=True)
        except: pass

# --- MAIN RUNNER ---
async def post_init(application):
    commands = [BotCommand("start", "🚀 Menu utama")]
    try: await application.bot.set_my_commands(commands)
    except: pass
    asyncio.create_task(restock_checker_task(application))

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    port = int(os.environ.get("PORT", 8080))
    webhook_url = os.environ.get("WEBHOOK_URL")
    
    if webhook_url:
        print(f"🚀 Running Webhook on {port} -> {webhook_url}")
        application.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{webhook_url}/{TOKEN}")
    else:
        hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("SPACE_HOST")
        if hostname:
            print(f"🚀 Running Webhook on {port} -> {hostname}")
            application.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"https://{hostname}/{TOKEN}")
        else:
            print("🔌 Running Polling Mode")
            application.run_polling()
