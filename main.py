import os
import json
import time
import sqlite3
import urllib.request
import urllib.error
import asyncio
import math
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# --- KONFIGURASI ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597")

_raw_key = os.environ.get("SMScode_API_KEY") or os.environ.get("SMSCODE_API_KEY") or ""
SMScode_API_KEY = _raw_key.strip()

BOT_VERSION = "13.2 Full Button Nav"
START_TIME = time.time()
MAX_BULK = 100
BATCH_SIZE = 10
TIMEOUT_MINUTES = 25
TIMEOUT_SECONDS = TIMEOUT_MINUTES * 60

BASE_URL = "https://api.smscode.gg/v1"
HEADERS = {
    "Authorization": f"Bearer {SMScode_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")
pending_messages = {}
active_bulk_data = {}

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

def get_uptime():
    secs = int(time.time() - START_TIME)
    days = secs // 86400; hours = (secs % 86400) // 3600; mins = (secs % 3600) // 60
    if days > 0: return f"{days}h {hours}j {mins}m"
    elif hours > 0: return f"{hours}j {mins}m"
    return f"{mins}m"

# --- KEYBOARD BAWAH (PERSISTENT) ---
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("🛒 Beli Nomor"), KeyboardButton("📦 Beli Banyak (Bulk)")],
        [KeyboardButton("💰 Cek Saldo"), KeyboardButton("📜 Riwayat")],
        [KeyboardButton("📦 Order Aktif"), KeyboardButton("ℹ️ Bantuan")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

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
    conn.commit(); conn.close()

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

def db_get_history(user_id, limit=10):
    try:
        conn = sqlite3.connect(DB_PATH)
        q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?"
        rows = conn.execute(q, (user_id, limit)).fetchall()
        conn.close(); return rows
    except Exception as e: return []

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
        conn.close()
        return {"today": today, "month": month, "total": total}
    except: return None

def get_template(key, default=""):
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM templates WHERE key=?", (key,)).fetchone()
        conn.close(); return row[0] if row else default
    except: return default

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

# --- HTTP ASYNC ---
def _http_get(url):
    req = urllib.request.Request(url, headers=HEADERS, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401: return {"error": "API Key salah atau tidak valid."}
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
        except: return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e: return {"error": str(e)}

def _http_post(url, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=HEADERS, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401: return {"error": "API Key salah atau tidak valid."}
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
        except: return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e: return {"error": str(e)}

async def http_get(url): return await asyncio.to_thread(_http_get, url)
async def http_post(url, payload): return await asyncio.to_thread(_http_post, url, payload)

def translate_error(err_msg):
    e = err_msg.lower()
    if "unauthorized" in e: return "🔐 API Key salah atau tidak valid."
    if "insufficient_balance" in e: return "💸 Saldo tidak cukup."
    if "no numbers available" in e: return "📭 Stok habis."
    if "cancel_too_early" in e: return "⏳ Tunggu minimal 2 menit sebelum membatalkan."
    if "cannot cancel order" in e: return "⚠️ Order sudah menerima OTP atau selesai, tidak bisa dibatalkan."
    if "timeout" in e: return "🌐 Koneksi timeout. Coba lagi."
    return f"⚠️ {err_msg[:100]}"

async def get_balance():
    data = await http_get(f"{BASE_URL}/balance")
    if data.get("success"): return data["data"].get("balance", 0)
    return -1

# --- API SMScode ---
async def get_gojek_products():
    data = await http_get(f"{BASE_URL}/catalog/products?country_id=7")
    if "error" in data: return None, translate_error(data["error"])
    if not data.get("success"): return None, "Gagal ambil data."
    products = data.get("data", [])
    if not isinstance(products, list): return None, "Format data tidak valid."
    gojek = [p for p in products if "Gojek" in p.get("name", "")]
    gojek.sort(key=lambda x: x.get('price', 999999))
    return gojek, None

async def get_active_orders():
    data = await http_get(f"{BASE_URL}/orders/active")
    if "error" in data: return None, translate_error(data["error"])
    if not data.get("success"): return None, "Gagal ambil order."
    orders = data.get("data", [])
    if not isinstance(orders, list): return None, "Format data tidak valid."
    return orders, None

async def cek_saldo_api():
    if not SMScode_API_KEY: return "❌ API Key belum diatur di ZevCloud.", -1
    data = await http_get(f"{BASE_URL}/balance")
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
    data = await http_post(f"{BASE_URL}/orders/create", {"product_id": product_id})
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
    saldo = await get_balance()
    if saldo >= 0 and saldo < total_cost:
        return None, 0, (f"❌ Saldo tidak cukup.\nSaldo: Rp {format_rupiah(saldo)}\n"
                         f"Butuh: Rp {format_rupiah(total_cost)} ({quantity}×Rp {format_rupiah(price)})")
    
    num_batches = math.ceil(quantity / BATCH_SIZE)
    tasks = []
    
    for i in range(num_batches):
        this_batch = min(BATCH_SIZE, quantity - (i * BATCH_SIZE))
        tasks.append(http_post(f"{BASE_URL}/orders/create", {"product_id": product_id, "quantity": this_batch}))
    
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_results = []
    total_failed = 0
    
    for res in responses:
        if isinstance(res, dict) and res.get("success"):
            orders = res["data"].get("orders", [])
            failed = res["data"].get("failed_count", 0)
            for od in orders:
                oid = str(od["id"]); nomor = od["phone_number"]
                db_save_order(oid, user_id, nomor, product_id, price)
                all_results.append((oid, nomor))
            total_failed += failed
        elif isinstance(res, dict) and "error" in res:
            pass
            
    if not all_results: return None, 0, "⚠️ Tidak ada order yang berhasil dibuat."
    return all_results, total_failed, None

async def batal_order_api(order_id):
    data = await http_post(f"{BASE_URL}/orders/cancel", {"id": int(order_id)})
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

async def selesai_order_api(order_id):
    data = await http_post(f"{BASE_URL}/orders/finish", {"id": int(order_id)})
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
             f"1️⃣ Masukkan nomor ke aplikasi Gojek\n"
             f"2️⃣ Bot akan kirim OTP otomatis\n"
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
        await asyncio.sleep(2) 
        elapsed += 2
        remaining = total - elapsed
        
        if order_id not in pending_messages and msg_id is not None:
            return

        data = await http_get(f"{BASE_URL}/orders/{order_id}")
        if not data.get("success"): continue
        
        od = data["data"]; otp = od.get("otp_code"); status = od.get("status")
        
        if otp:
            db_update_order(order_id, "OTP_RECEIVED", otp)
            tmpl = get_template("otp_found", "🔑 <b>Kode OTP Ditemukan!</b>\n\n📱 Nomor: <code>{nomor}</code>\n🔑 Kode: <code>{otp}</code>")
            teks = tmpl.replace("{otp}", otp).replace("{nomor}", nomor_display)
            
            kb = [[InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")]]
            
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

    await batal_order_api(order_id)
    if msg_id:
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="⌛ <b>Waktu Habis</b>\nNomor otomatis dibatalkan (Refund).", parse_mode='HTML')
        except: pass

# ============================================================
# === BULK MONITOR ===
# ============================================================
async def bulk_monitor_task(context, chat_id, batch_id, ordered_list, price_per_unit, summary_msg_id, pid, failed_count=0):
    total = len(ordered_list)
    pending = {oid: phone for oid, phone in ordered_list}
    received_set = set()
    start_time = time.time()
    timeout_seconds = TIMEOUT_SECONDS

    active_bulk_data[chat_id] = {
        "pending": pending, "pid": pid, "price": price_per_unit, "batch_id": batch_id
    }

    async def update_summary():
        try:
            if active_bulk_data.get(chat_id, {}).get("batch_id") != batch_id:
                return
            
            elapsed = int(time.time() - start_time)
            remaining = max(0, timeout_seconds - elapsed)
            teks = build_bulk_summary(ordered_list, received_set, price_per_unit, remaining, failed_count)
            
            kb = []
            if len(pending) > 0:
                kb.append([InlineKeyboardButton(f"🔄 Ganti Semua Nomor ({len(pending)})", callback_data=f"replaceall_{pid}_{price_per_unit}")])
                
            await context.bot.edit_message_text(chat_id=chat_id, message_id=summary_msg_id, text=teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
        except Exception: pass

    await update_summary()
    
    while pending and (time.time() - start_time) < timeout_seconds:
        if active_bulk_data.get(chat_id, {}).get("batch_id") != batch_id:
            return 
            
        await asyncio.sleep(3) 
        if not pending: break
        
        data = await http_get(f"{BASE_URL}/orders/active")
        active_map = {}
        if data.get("success"):
            for o in data.get("data", []): active_map[str(o.get("id"))] = o
            
        missing_oids = [oid for oid in pending if oid not in active_map]
        missing_data = {}
        if missing_oids:
            tasks = [http_get(f"{BASE_URL}/orders/{oid}") for oid in missing_oids]
            res_list = await asyncio.gather(*tasks, return_exceptions=True)
            for oid, res in zip(missing_oids, res_list):
                if isinstance(res, dict) and res.get("success"):
                    missing_data[oid] = res["data"]

        to_remove = []
        for oid, phone in list(pending.items()):
            o = active_map.get(oid) or missing_data.get(oid)
            if o is None: continue
            
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
            await batal_order_api(oid)
        await update_summary()
        
    if active_bulk_data.get(chat_id, {}).get("batch_id") == batch_id:
        active_bulk_data.pop(chat_id, None)

# ============================================================
# === UI TAMPILAN MENU ===
# ============================================================
async def send_premium_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.first_name or "Bos"
    _, saldo = await cek_saldo_api()
    
    teks = (
        f"✨ <b>REIVALDO NOKOS</b> ✨\n"
        f"<i>Solusi OTP Cepat & Otomatis 24/7</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👋 Halo, <b>{user}</b>!\n"
        f"💰 <b>Saldo Anda:</b> Rp {format_rupiah(saldo) if saldo >= 0 else '-'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <b>Kenapa pakai bot ini?</b>\n"
        f"✅ Proses OTP super cepat (cek 2 detik)\n"
        f"✅ Auto-refund jika OTP macet\n"
        f"✅ Ganti nomor tinggal 1 klik\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👇 <b>Silakan pilih menu di bawah ini:</b>"
    )
    
    if update.message:
        await update.message.reply_text(teks, reply_markup=get_main_keyboard(), parse_mode='HTML')
    elif update.callback_query:
        try: await update.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text=teks, reply_markup=get_main_keyboard(), parse_mode='HTML')

async def send_beli_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products, err = await get_gojek_products()
    if err:
        return await context.bot.send_message(update.effective_chat.id, f"❌ {err}")
    
    keyboard = []; seen = set()
    for p in products:
        harga = p.get('price', '?'); pid = p.get('id'); stok = p.get('available', 0)
        if harga in seen: continue
        seen.add(harga)
        if stok and stok > 0:
            keyboard.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(harga)} | 📦 {stok}", callback_data=f"buy_{pid}_{harga}")])
        else:
            keyboard.append([InlineKeyboardButton(f"❌ Rp {format_rupiah(harga)} (Habis)", callback_data=f"watch_{pid}_{harga}")])
        if len(keyboard) >= 8: break
        
    keyboard.append([InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')])
    
    if update.message:
        await update.message.reply_text("🛒 <b>Pilih Harga Nomor Gojek:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    elif update.callback_query:
        try: await update.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🛒 <b>Pilih Harga Nomor Gojek:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def send_bulk_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products, err = await get_gojek_products()
    if err:
        return await context.bot.send_message(update.effective_chat.id, f"❌ {err}")
    
    keyboard = []; seen = set()
    for p in products:
        harga = p.get('price', '?'); pid = p.get('id'); stok = p.get('available', 0)
        if harga in seen: continue
        seen.add(harga)
        if stok and stok > 0:
            keyboard.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(harga)} | 📦 {stok}", callback_data=f"bulkprice_{pid}_{harga}")])
        if len(keyboard) >= 8: break
        
    keyboard.append([InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')])
    
    if update.message:
        await update.message.reply_text("📦 <b>Pilih Harga (Bulk):</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    elif update.callback_query:
        try: await update.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text="📦 <b>Pilih Harga (Bulk):</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# ============================================================
# === HANDLER TOMBOL BAWAH ===
# ============================================================
async def msg_beli(update: Update, context: ContextTypes.DEFAULT_TYPE): await send_beli_menu(update, context)
async def msg_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE): await send_bulk_menu(update, context)

async def msg_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks, _ = await cek_saldo_api()
    await update.message.reply_text(teks, parse_mode='HTML')

async def msg_riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_get_history(update.effective_user.id, 20)
    if not rows: return await update.message.reply_text("Belum ada transaksi.")
    
    teks = "📜 <b>Riwayat 20 Transaksi Terakhir:</b>\n\n"
    for oid, phone, status, otp, created in rows:
        emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else ("❌" if status == "CANCELLED" else "⏳"))
        teks += f"{emoji} <code>{format_phone(phone)}</code> | {status} | {otp or '-'}\n"
    await update.message.reply_text(teks, parse_mode='HTML')

async def msg_aktif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders, err = await get_active_orders()
    if err: return await update.message.reply_text(f"❌ {err}")
    if not orders: return await update.message.reply_text("📭 Tidak ada order aktif.")
    
    kb = []
    for o in orders[:10]:
        kb.append([InlineKeyboardButton(f"❌ Batal {format_phone(o['phone_number'])}", callback_data=f"cancel_{o['id']}")])
    await update.message.reply_text("📦 <b>Order Aktif (Pilih untuk batal):</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

async def msg_bantuan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = (f"ℹ️ <b>Bantuan</b>\n\n"
            f"1. Klik <b>Beli Nomor</b> untuk membeli 1 nomor.\n"
            f"2. Klik <b>Beli Banyak</b> untuk order massal.\n"
            f"3. Jika OTP macet, klik Ganti Nomor.\n"
            f"4. Auto-refund dalam {TIMEOUT_MINUTES} menit.")
    await update.message.reply_text(teks, parse_mode='HTML')

async def msg_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚠️ <b>Mohon gunakan tombol di bawah layar untuk mengakses menu.</b>", parse_mode='HTML', reply_markup=get_main_keyboard())

# ============================================================
# === CALLBACK HANDLER (INLINE BUTTONS) ===
# ============================================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.answer("🚫 Akses ditolak.", show_alert=True)
        return

    data = query.data
    try:
        if data.startswith(('cancel_', 'finish_', 'replace_', 'buy_', 'bulk', 'watch_', 'menu_')):
            try: await query.answer()
            except: pass
        
        if data == 'menu_utama':
            await send_premium_menu(update, context)
        elif data == 'menu_beli':
            await send_beli_menu(update, context)
        elif data == 'menu_bulk':
            await send_bulk_menu(update, context)

        elif data.startswith('bulkprice_'):
            parts = data.replace('bulkprice_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1])
            _, saldo = await cek_saldo_api()
            teks = (f"📦 <b>Beli Banyak — Rp {format_rupiah(harga)}</b>\n\n"
                    f"💰 Saldo: <b>Rp {format_rupiah(saldo) if saldo >= 0 else '?'}</b>\n\n"
                    f"Pilih jumlah:")
            kb = [
                [InlineKeyboardButton("📦 5 Nomor", callback_data=f"bulkqty_{pid}_{harga}_5"),
                 InlineKeyboardButton("📦 10 Nomor", callback_data=f"bulkqty_{pid}_{harga}_10")],
                [InlineKeyboardButton("📦 20 Nomor", callback_data=f"bulkqty_{pid}_{harga}_20"),
                 InlineKeyboardButton("📦 50 Nomor", callback_data=f"bulkqty_{pid}_{harga}_50")],
                [InlineKeyboardButton("📦 100 Nomor", callback_data=f"bulkqty_{pid}_{harga}_100")],
                [InlineKeyboardButton("🔙 Kembali", callback_data='menu_bulk')],
                [InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]
            ]
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

        elif data.startswith('bulkqty_'):
            parts = data.replace('bulkqty_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1]); qty = int(parts[2])
            total = harga * qty
            await query.edit_message_text(f"⏳ Membeli <b>{qty} nomor</b>...\nTotal: Rp {format_rupiah(total)}", parse_mode='HTML')
            results, failed_count, err = await beli_banyak_api_async(pid, user_id, harga, qty)
            if results:
                ordered_list = [(oid, format_phone(n)) for oid, n in results]
                summary_teks = build_bulk_summary(ordered_list, set(), harga, TIMEOUT_SECONDS, failed_count)
                msg = await query.edit_message_text(summary_teks, parse_mode='HTML')
                batch_id = f"{user_id}_{int(time.time())}"
                asyncio.create_task(bulk_monitor_task(context, query.message.chat_id, batch_id, ordered_list, harga, msg.message_id, pid, failed_count))
            else:
                await query.edit_message_text(f"❌ {err}")

        elif data.startswith('watch_'):
            parts = data.replace('watch_', '').split('_')
            pid = int(parts[0]); price = int(parts[1])
            if db_watchlist_add(user_id, pid, price):
                await query.edit_message_text(f"🔔 <b>Dipantau!</b>\n\nProduk Gojek Rp {format_rupiah(price)} akan dipantau.", parse_mode='HTML')
            else: await query.answer("❌ Gagal tambah", show_alert=True)

        elif data.startswith('buy_'):
            parts = data.replace('buy_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1])
            await query.edit_message_text("⏳ Sedang membeli nomor...")
            order_id, nomor_raw = await beli_nomor_api(pid, user_id, harga)
            if order_id:
                nomor_display = format_phone(nomor_raw)
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display, pid, harga))
                await query.message.delete()
            else:
                await query.edit_message_text(f"❌ {nomor_raw}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data='menu_utama')]]))

        elif data.startswith('replaceall_'):
            parts = data.split("_")
            pid = int(parts[1]); harga = int(parts[2])
            chat_id = query.message.chat_id
            
            if chat_id not in active_bulk_data or not active_bulk_data[chat_id]["pending"]:
                return await query.answer("❌ Tidak ada nomor yang bisa diganti.", show_alert=True)
                
            await query.answer("🔄 Membatalkan semua nomor lama...")
            await query.edit_message_text("⏳ Memproses ganti semua nomor. Mohon tunggu...", parse_mode='HTML')
            
            if chat_id in active_bulk_data:
                active_bulk_data[chat_id]["batch_id"] = "CANCELLED"
                
            pending_olds = list(active_bulk_data[chat_id]["pending"].keys())
            tasks = [batal_order_api(oid) for oid in pending_olds]
            cancel_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            success_cancel = sum(1 for res in cancel_results if isinstance(res, dict) and res.get("success"))
                
            if success_cancel == 0:
                return await query.edit_message_text("❌ Gagal membatalkan nomor lama. Mungkin OTP sudah masuk atau belum 2 menit.", parse_mode='HTML')
                
            await query.edit_message_text(f"♻️ Berhasil batalkan {success_cancel} nomor. Membeli nomor baru...", parse_mode='HTML')
            
            results, failed_count, err = await beli_banyak_api_async(pid, user_id, harga, success_cancel)
            if results:
                ordered_list = [(oid, format_phone(n)) for oid, n in results]
                active_bulk_data.pop(chat_id, None) 
                
                summary_teks = build_bulk_summary(ordered_list, set(), harga, TIMEOUT_SECONDS, failed_count)
                msg = await query.edit_message_text(summary_teks, parse_mode='HTML')
                batch_id = f"{user_id}_{int(time.time())}"
                asyncio.create_task(bulk_monitor_task(context, chat_id, batch_id, ordered_list, harga, msg.message_id, pid, failed_count))
            else:
                await query.edit_message_text(f"❌ Gagal membeli nomor pengganti: {err}")

        elif data.startswith('replace_'):
            parts = data.split("_")
            old_oid = parts[1]; pid = int(parts[2]); harga = int(parts[3])
            
            await query.answer("🔄 Memproses ganti nomor...", show_alert=False)
            res, msg = await batal_order_api(old_oid)
            if not res: return await query.answer(f"❌ Gagal batal: {msg[:50]}", show_alert=True)
                
            pending_messages.pop(old_oid, None)
            
            order_id, nomor_raw = await beli_nomor_api(pid, user_id, harga)
            if order_id:
                nomor_display = format_phone(nomor_raw)
                is_bulk = "BULK SUMMARY" in getattr(query.message, 'text', '')
                if not is_bulk:
                    try: await query.message.delete()
                    except: pass
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display, pid, harga))
            else:
                await query.answer(f"❌ Gagal ganti nomor: {nomor_raw}", show_alert=True)

        elif data.startswith('cancel_'):
            order_id = data.replace('cancel_', '')
            try: await query.message.delete()
            except: pass
            hasil = await batal_order_api(order_id)
            pending_messages.pop(order_id, None)
            
            if hasil["success"]:
                await query.answer(f"✅ Berhasil Dibatalkan\n💰 Refund: Rp {format_rupiah(hasil.get('refund_amount', 0))}", show_alert=True)
            else:
                await query.answer(f"❌ Gagal Batal: {hasil['message'][:50]}", show_alert=True)

        elif data.startswith('finish_'):
            order_id = data.replace('finish_', '')
            try: await query.message.delete()
            except: pass
            hasil = await selesai_order_api(order_id)
            pending_messages.pop(order_id, None)
            
            if hasil["success"]:
                await query.answer("✅ Selesai & Nomor berhasil dilepas.", show_alert=True)
            else:
                await query.answer(f"❌ Gagal: {hasil['message'][:50]}", show_alert=True)

    except Exception as e:
        try: await query.answer("⚠️ Terjadi kesalahan sistem.", show_alert=True)
        except: pass

# ============================================================
# === START & BACKGROUND ===
# ============================================================
async def restock_checker_task(context):
    while True:
        await asyncio.sleep(5 * 60)
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute("SELECT id, user_id, product_id, price FROM watchlist").fetchall()
            conn.close()
            if not rows: continue
            products, err = await get_gojek_products()
            if err or not products: continue
            for wid, uid, pid, price in rows:
                for p in products:
                    if p.get('id') == pid and p.get('available', 0) > 0:
                        try:
                            await context.bot.send_message(chat_id=uid, text=f"🎉 <b>Stok Kembali Tersedia!</b>\n\n📦 Gojek\n💵 Harga: Rp {format_rupiah(price)}\n📊 Stok: {p.get('available')}", parse_mode='HTML')
                        except: pass
                        db_watchlist_remove_by_id(wid)
                        break
        except Exception as e: print(f"Restock error: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_premium_menu(update, context)

async def post_init(application):
    try: await application.bot.delete_my_commands()
    except: pass
    asyncio.create_task(restock_checker_task(application))

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start))
    
    application.add_handler(MessageHandler(filters.Regex('^🛒 Beli Nomor$'), msg_beli))
    application.add_handler(MessageHandler(filters.Regex('^📦 Beli Banyak \(Bulk\)$'), msg_bulk))
    application.add_handler(MessageHandler(filters.Regex('^💰 Cek Saldo$'), msg_saldo))
    application.add_handler(MessageHandler(filters.Regex('^📜 Riwayat$'), msg_riwayat))
    application.add_handler(MessageHandler(filters.Regex('^📦 Order Aktif$'), msg_aktif))
    application.add_handler(MessageHandler(filters.Regex('^ℹ️ Bantuan$'), msg_bantuan))
    
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_unknown))
    
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
