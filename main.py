import os
import json
import time
import sqlite3
import urllib.request
import urllib.error
import asyncio
import math
import traceback
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ==========================================
# KONFIGURASI UTAMA
# ==========================================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597")

_raw_key = os.environ.get("SMScode_API_KEY") or os.environ.get("SMSCODE_API_KEY") or ""
SMScode_API_KEY = _raw_key.strip()

BOT_VERSION = "19.0 Auto-Recovery"
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
_processing_users = {}

# ==========================================
# HELPER & UTILITY
# ==========================================
def format_rupiah(n):
    try: return f"{int(n):,}".replace(",", ".")
    except: return str(n)

def format_countdown(seconds):
    if seconds < 0: seconds = 0
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{m:02d}:{s:02d}"

def format_phone(p):
    if not p: return "-"
    p_str = str(p)
    if p_str.startswith("62"): return p_str[2:]
    return p_str

def is_admin(uid): return str(uid) == str(ADMIN_ID)

def try_acquire_lock(uid):
    now = time.time()
    if now - _processing_users.get(uid, 0) < 5:
        return False
    _processing_users[uid] = now
    return True

def release_lock(uid): _processing_users.pop(uid, None)

async def send_temp_msg(context, chat_id, text, delay=4):
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
        await asyncio.sleep(delay)
        await msg.delete()
    except: pass

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("🛒 Beli Nomor"), KeyboardButton("📦 Beli Banyak (Bulk)")],
        [KeyboardButton("💰 Cek Saldo"), KeyboardButton("📜 Riwayat")],
        [KeyboardButton("📦 Order Aktif"), KeyboardButton("ℹ️ Bantuan")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

# ==========================================
# DATABASE
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY, user_id INTEGER, phone TEXT, product_id INTEGER,
        price INTEGER, status TEXT, otp TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS templates (key TEXT PRIMARY KEY, value TEXT)''')
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

def db_get_history(user_id, limit=20):
    try:
        conn = sqlite3.connect(DB_PATH)
        q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?"
        rows = conn.execute(q, (user_id, limit)).fetchall()
        conn.close(); return rows
    except Exception as e: return []

# ==========================================
# HTTP ASYNC
# ==========================================
def _http_get(url):
    req = urllib.request.Request(url, headers=HEADERS, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401: return {"error": "API Key salah."}
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
        if e.code == 401: return {"error": "API Key salah."}
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
        except: return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e: return {"error": str(e)}

async def http_get(url): return await asyncio.to_thread(_http_get, url)
async def http_post(url, payload): return await asyncio.to_thread(_http_post, url, payload)

def translate_error(err_msg):
    e = err_msg.lower()
    if "unauthorized" in e: return "🔐 API Key salah."
    if "insufficient_balance" in e: return "💸 Saldo tidak cukup."
    if "no numbers available" in e: return "📭 Stok habis."
    if "cancel_too_early" in e: return "⏳ Tunggu 2 menit sebelum batal."
    if "cannot cancel order" in e: return "⚠️ Order sudah OTP/selesai."
    if "timeout" in e: return "🌐 Koneksi timeout."
    return f"⚠️ {err_msg[:100]}"

async def get_balance():
    data = await http_get(f"{BASE_URL}/balance")
    if data.get("success"): return data["data"].get("balance", 0)
    return -1

# ==========================================
# API SMScode
# ==========================================
async def get_gojek_products():
    data = await http_get(f"{BASE_URL}/catalog/products?country_id=7")
    if "error" in data: return None, translate_error(data["error"])
    products = data.get("data", [])
    if not isinstance(products, list): return None, "Format katalog tidak valid."
    gojek = [p for p in products if "Gojek" in p.get("name", "")]
    gojek.sort(key=lambda x: x.get('price', 999999))
    return gojek, None

async def get_active_orders():
    data = await http_get(f"{BASE_URL}/orders/active")
    if "error" in data: return None, translate_error(data["error"])
    
    raw_data = data.get("data", [])
    if isinstance(raw_data, dict) and "orders" in raw_data:
        orders = raw_data["orders"]
    elif isinstance(raw_data, list):
        orders = raw_data
    else:
        orders = []
        
    return orders, None

async def cek_saldo_api():
    if not SMScode_API_KEY: return "❌ API Key belum diatur.", -1
    data = await http_get(f"{BASE_URL}/balance")
    if "error" in data: return translate_error(data["error"]), -1
    if data.get("success"):
        saldo = data["data"].get("balance", 0)
        return f"💰 Saldo Saya: <b>Rp {format_rupiah(saldo)}</b>", saldo
    return "⚠️ Gagal ambil saldo.", -1

async def beli_nomor_api(product_id, user_id, price):
    saldo = await get_balance()
    if saldo >= 0 and saldo < price:
        return None, f"❌ Saldo tidak cukup (Butuh: Rp {format_rupiah(price)})."
    data = await http_post(f"{BASE_URL}/orders/create", {"product_id": product_id})
    if "error" in data: return None, translate_error(data["error"])
    if data.get("success"):
        od = data["data"]["orders"][0]
        oid = str(od["id"]); nomor = od["phone_number"]
        db_save_order(oid, user_id, nomor, product_id, price)
        return oid, nomor
    return None, "⚠️ Gagal membeli."

async def beli_banyak_api_async(product_id, user_id, price, quantity):
    total_cost = price * quantity
    saldo = await get_balance()
    if saldo >= 0 and saldo < total_cost:
        return None, 0, f"❌ Saldo tidak cukup (Butuh: Rp {format_rupiah(total_cost)})."
    
    num_batches = math.ceil(quantity / BATCH_SIZE)
    tasks = []
    for i in range(num_batches):
        this_batch = min(BATCH_SIZE, quantity - (i * BATCH_SIZE))
        tasks.append(http_post(f"{BASE_URL}/orders/create", {"product_id": product_id, "quantity": this_batch}))
    
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    all_results, total_failed = [], 0
    
    for res in responses:
        if isinstance(res, dict) and res.get("success"):
            orders = res["data"].get("orders", [])
            for od in orders:
                oid, nomor = str(od["id"]), od["phone_number"]
                db_save_order(oid, user_id, nomor, product_id, price)
                all_results.append((oid, nomor))
            total_failed += res["data"].get("failed_count", 0)
            
    if not all_results: return None, 0, "⚠️ Tidak ada order yang berhasil."
    return all_results, total_failed, None

async def batal_order_api(order_id):
    data = await http_post(f"{BASE_URL}/orders/cancel", {"id": int(order_id)})
    if "error" in data: return {"success": False, "message": translate_error(data["error"])}
    if data.get("success"):
        db_update_order(order_id, "CANCELLED")
        return {"success": True, "message": "✅ Berhasil Dibatalkan"}
    return {"success": False, "message": "⚠️ Gagal Batal"}

async def selesai_order_api(order_id):
    data = await http_post(f"{BASE_URL}/orders/finish", {"id": int(order_id)})
    if "error" in data: return {"success": False, "message": translate_error(data["error"])}
    if data.get("success"):
        db_update_order(order_id, "COMPLETED")
        return {"success": True, "message": "✅ Selesai"}
    return {"success": False, "message": "⚠️ Gagal Selesai"}

# ==========================================
# POLLING & BULK MONITOR
# ==========================================
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
            teks = f"🔑 <b>OTP:</b> <code>{otp}</code>\n📱 <b>Nomor:</b> <code>{nomor_display}</code>"
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
            
        elif status == "ACTIVE" and not msg_id:
            try:
                kb = [
                    [InlineKeyboardButton("🔄 Ganti Nomor", callback_data=f"replace_{order_id}_{pid}_{price}")],
                    [InlineKeyboardButton("❌ Batalkan", callback_data=f"cancel_{order_id}")]
                ]
                msg = await context.bot.send_message(chat_id=chat_id,
                    text=f"⏳ <b>Menunggu SMS...</b>\n📱 <code>{nomor_display}</code>\n⏱️ Sisa: <b>{format_countdown(remaining)}</b>",
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                msg_id = msg.message_id
                pending_messages[order_id] = msg_id
            except: pass
            
        elif status == "ACTIVE" and msg_id and elapsed % 10 == 0:
            try:
                kb = [
                    [InlineKeyboardButton("🔄 Ganti Nomor", callback_data=f"replace_{order_id}_{pid}_{price}")],
                    [InlineKeyboardButton("❌ Batalkan", callback_data=f"cancel_{order_id}")]
                ]
                await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                    text=f"⏳ <b>Menunggu SMS...</b>\n📱 <code>{nomor_display}</code>\n⏱️ Sisa: <b>{format_countdown(remaining)}</b>",
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
            except: pass

    await batal_order_api(order_id)
    if msg_id:
        try: await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except: pass
        await send_temp_msg(context, chat_id, "⌛ Waktu habis, nomor dibatalkan.")

def build_bulk_summary(ordered_list, received_set, price_per_unit, remaining_seconds):
    total = len(ordered_list)
    text = f"🛒 <b>Bulk Order ({total} Nomor)</b>\n"
    text += f"📊 Progress: <b>{len(received_set)}/{total}</b>\n"
    text += f"⏱️ Sisa: <b>{format_countdown(remaining_seconds)}</b>\n\n"
    for i, (oid, phone) in enumerate(ordered_list, 1):
        check = " ✅" if oid in received_set else ""
        text += f"{i}.<code>{phone}</code>{check}\n"
    return text

async def bulk_monitor_task(context, chat_id, batch_id, ordered_list, price_per_unit, summary_msg_id, pid):
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
            if active_bulk_data.get(chat_id, {}).get("batch_id") != batch_id: return
            elapsed = int(time.time() - start_time)
            remaining = max(0, timeout_seconds - elapsed)
            teks = build_bulk_summary(ordered_list, received_set, price_per_unit, remaining)
            
            kb = []
            if len(pending) > 0:
                kb.append([InlineKeyboardButton(f"🔄 Ganti Semua ({len(pending)})", callback_data=f"replaceall_{pid}_{price_per_unit}")])
                
            await context.bot.edit_message_text(chat_id=chat_id, message_id=summary_msg_id, text=teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb) if kb else None)
        except Exception: pass

    await update_summary()
    
    while pending and (time.time() - start_time) < timeout_seconds:
        if active_bulk_data.get(chat_id, {}).get("batch_id") != batch_id: return 
            
        await asyncio.sleep(3) 
        if not pending: break
        
        data = await http_get(f"{BASE_URL}/orders/active")
        active_map = {str(o.get("id")): o for o in data.get("data", [])} if data.get("success") and isinstance(data.get("data"), list) else {}
            
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
                        text=f"🔑 <b>OTP:</b> <code>{otp}</code>\n📱 <b>Nomor:</b> <code>{phone}</code>",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{oid}")]]),
                        parse_mode='HTML')
                except Exception: pass
                to_remove.append(oid)
            elif status in ["CANCELLED", "EXPIRED", "COMPLETED"]:
                db_update_order(oid, status); to_remove.append(oid)
                
        for oid in to_remove: pending.pop(oid, None)
        await update_summary()
        
    try: await context.bot.delete_message(chat_id=chat_id, message_id=summary_msg_id)
    except: pass
    
    if pending:
        for oid, _ in list(pending.items()): await batal_order_api(oid)
        await send_temp_msg(context, chat_id, "⌛ Waktu Bulk habis, sisa nomor dibatalkan.")
        
    if active_bulk_data.get(chat_id, {}).get("batch_id") == batch_id:
        active_bulk_data.pop(chat_id, None)

# ==========================================
# UI & HANDLERS
# ==========================================
async def send_premium_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.first_name or "Bos"
    _, saldo = await cek_saldo_api()
    
    teks = (
        f"✨ <b>REIVALDO NOKOS</b> ✨\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👋 Halo, <b>{user}</b>!\n"
        f"💰 <b>Saldo:</b> Rp {format_rupiah(saldo) if saldo >= 0 else '-'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <i>Proses OTP super cepat (cek 2 detik)</i>\n"
        f"👇 <b>Silakan pilih menu:</b>"
    )
    
    if update.message:
        await update.message.reply_text(teks, reply_markup=get_main_keyboard(), parse_mode='HTML')
    elif update.callback_query:
        try: await update.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text=teks, reply_markup=get_main_keyboard(), parse_mode='HTML')

async def send_beli_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products, err = await get_gojek_products()
    if err: return await send_temp_msg(context, update.effective_chat.id, f"❌ {err}")
    
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
        
    keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')])
    
    if update.message:
        await update.message.reply_text("🛒 <b>Pilih Harga:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    elif update.callback_query:
        try: await update.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🛒 <b>Pilih Harga:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def send_bulk_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products, err = await get_gojek_products()
    if err: return await send_temp_msg(context, update.effective_chat.id, f"❌ {err}")
    
    keyboard = []; seen = set()
    for p in products:
        harga = p.get('price', '?'); pid = p.get('id'); stok = p.get('available', 0)
        if harga in seen: continue
        seen.add(harga)
        if stok and stok > 0:
            keyboard.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(harga)} | 📦 {stok}", callback_data=f"bulkprice_{pid}_{harga}")])
        if len(keyboard) >= 8: break
        
    keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')])
    
    if update.message:
        await update.message.reply_text("📦 <b>Pilih Harga (Bulk):</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    elif update.callback_query:
        try: await update.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text="📦 <b>Pilih Harga (Bulk):</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# HANDLERS BAWAH
async def msg_beli(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    try: await send_beli_menu(update, context)
    except Exception as e: await send_temp_msg(context, update.effective_chat.id, "⚠️ Error sistem.")

async def msg_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    try: await send_bulk_menu(update, context)
    except Exception as e: await send_temp_msg(context, update.effective_chat.id, "⚠️ Error sistem.")

async def msg_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        teks, _ = await cek_saldo_api()
        await update.message.reply_text(teks, parse_mode='HTML')
    except Exception as e: await send_temp_msg(context, update.effective_chat.id, "⚠️ Error sistem.")

async def msg_riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = db_get_history(update.effective_user.id, 20)
        if not rows: return await send_temp_msg(context, update.effective_chat.id, "Belum ada transaksi.")
        
        teks = "📜 <b>20 Transaksi Terakhir:</b>\n\n"
        for oid, phone, status, otp, created in rows:
            emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else ("❌" if status == "CANCELLED" else "⏳"))
            teks += f"{emoji} <code>{format_phone(phone)}</code> | {status} | {otp or '-'}\n"
        await update.message.reply_text(teks, parse_mode='HTML')
    except Exception as e: await send_temp_msg(context, update.effective_chat.id, "⚠️ Error sistem.")

async def msg_aktif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        orders, err = await get_active_orders()
        if err: return await send_temp_msg(context, update.effective_chat.id, f"❌ {err}")
        if not orders: return await send_temp_msg(context, update.effective_chat.id, "📭 Tidak ada order aktif.")
        
        kb = []
        conn = sqlite3.connect(DB_PATH)
        
        for o in orders[:10]:
            oid = str(o.get('id', '0'))
            otp = o.get('otp_code')
            
            nomor_raw = o.get('phone_number') or o.get('phone')
            if not nomor_raw:
                db_row = conn.execute("SELECT phone FROM orders WHERE order_id=?", (oid,)).fetchone()
                nomor_raw = db_row[0] if db_row else "-"
                
            nomor = format_phone(nomor_raw)
            
            label = f"📱 {nomor}" + (f" (🔑 {otp})" if otp else " (⏳)")
            kb.append([InlineKeyboardButton(label, callback_data=f"cancel_{oid}")])
            
        conn.close()
            
        kb.append([InlineKeyboardButton("🗑️ Batalkan Semua Nomor", callback_data='cancel_all_active')])
        kb.append([InlineKeyboardButton("🔙 Tutup", callback_data='menu_utama')])
        
        await update.message.reply_text("📦 <b>Order Aktif (Klik untuk Batal):</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    except Exception as e: 
        print(traceback.format_exc())
        await send_temp_msg(context, update.effective_chat.id, "⚠️ Error sistem saat memuat order aktif.")

async def msg_bantuan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        teks = (f"ℹ️ <b>Bantuan</b>\n\nGunakan tombol di bawah layar.\nAuto-refund dalam {TIMEOUT_MINUTES} menit.")
        await update.message.reply_text(teks, parse_mode='HTML')
    except: pass

async def msg_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚠️ <b>Gunakan tombol di bawah layar.</b>", parse_mode='HTML', reply_markup=get_main_keyboard())

# ==========================================
# CALLBACK HANDLER
# ==========================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.answer("🚫 Akses ditolak.", show_alert=True)
        return

    data = query.data
    
    is_transaction = data.startswith(('bulkqty_', 'buy_', 'replaceall_', 'replace_'))
    
    if is_transaction:
        if not try_acquire_lock(user_id):
            return await query.answer("⏳ Mohon tunggu, transaksi sebelumnya sedang diproses.", show_alert=True)
            
    try:
        if data.startswith(('cancel_', 'finish_', 'replace_', 'buy_', 'bulk', 'watch_', 'menu_')):
            try: await query.answer()
            except: pass
        
        if data == 'menu_utama': await send_premium_menu(update, context)
        elif data == 'menu_beli': await send_beli_menu(update, context)
        elif data == 'menu_bulk': await send_bulk_menu(update, context)

        elif data == 'cancel_all_active':
            await query.edit_message_text("⏳ <b>Membatalkan semua nomor aktif...</b>", parse_mode='HTML')
            
            orders, err = await get_active_orders()
            if err: return await send_temp_msg(context, query.message.chat_id, f"❌ {err}")
            
            to_cancel = [str(o.get('id')) for o in orders if not o.get('otp_code')]
            
            if not to_cancel:
                return await query.edit_message_text("✅ Tidak ada nomor yang bisa dibatalkan (semua sudah ada OTP).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]]))
                
            tasks = [batal_order_api(oid) for oid in to_cancel]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            success = sum(1 for res in results if isinstance(res, dict) and res.get('success'))
            
            await query.edit_message_text(
                f"✅ <b>Batalkan Massal Selesai</b>\n\n"
                f"Total dibatalkan: <b>{success}</b> nomor.\n"
                f"Saldo telah dikembalikan ke akun Anda.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Utama", callback_data='menu_utama')]]),
                parse_mode='HTML'
            )

        elif data.startswith('bulkprice_'):
            parts = data.replace('bulkprice_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1])
            _, saldo = await cek_saldo_api()
            teks = (f"📦 <b>Beli Banyak — Rp {format_rupiah(harga)}</b>\n"
                    f"💰 Saldo: <b>Rp {format_rupiah(saldo) if saldo >= 0 else '?'}</b>\n\n"
                    f"Pilih jumlah:")
            kb = [
                [InlineKeyboardButton("📦 5 Nomor", callback_data=f"bulkqty_{pid}_{harga}_5"),
                 InlineKeyboardButton("📦 10 Nomor", callback_data=f"bulkqty_{pid}_{harga}_10")],
                [InlineKeyboardButton("📦 20 Nomor", callback_data=f"bulkqty_{pid}_{harga}_20"),
                 InlineKeyboardButton("📦 50 Nomor", callback_data=f"bulkqty_{pid}_{harga}_50")],
                [InlineKeyboardButton("📦 100 Nomor", callback_data=f"bulkqty_{pid}_{harga}_100")],
                [InlineKeyboardButton("🔙 Kembali", callback_data='menu_bulk')]
            ]
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

        elif data.startswith('bulkqty_'):
            parts = data.replace('bulkqty_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1]); qty = int(parts[2])
            await query.message.delete() 
            
            temp_msg = await context.bot.send_message(query.message.chat_id, f"⏳ Membeli <b>{qty} nomor</b>...", parse_mode='HTML')
            results, failed_count, err = await beli_banyak_api_async(pid, user_id, harga, qty)
            try: await temp_msg.delete()
            except: pass
            
            if results:
                ordered_list = [(oid, format_phone(n)) for oid, n in results]
                summary_teks = build_bulk_summary(ordered_list, set(), harga, TIMEOUT_SECONDS)
                msg = await context.bot.send_message(query.message.chat_id, summary_teks, parse_mode='HTML')
                batch_id = f"{user_id}_{int(time.time())}"
                asyncio.create_task(bulk_monitor_task(context, query.message.chat_id, batch_id, ordered_list, harga, msg.message_id, pid))
            else:
                await send_temp_msg(context, query.message.chat_id, f"❌ {err}")

        elif data.startswith('watch_'):
            parts = data.replace('watch_', '').split('_')
            pid = int(parts[0]); price = int(parts[1])
            await query.answer("🔔 Produk dipantau!", show_alert=True)
            await query.message.delete()

        elif data.startswith('buy_'):
            parts = data.replace('buy_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1])
            await query.message.delete() 
            
            temp_msg = await context.bot.send_message(query.message.chat_id, "⏳ Sedang membeli...", parse_mode='HTML')
            order_id, nomor_raw = await beli_nomor_api(pid, user_id, harga)
            try: await temp_msg.delete()
            except: pass
            
            if order_id:
                nomor_display = format_phone(nomor_raw)
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display, pid, harga))
            else:
                await send_temp_msg(context, query.message.chat_id, f"❌ {nomor_raw}")

        elif data.startswith('replaceall_'):
            parts = data.split("_")
            pid = int(parts[1]); harga = int(parts[2])
            chat_id = query.message.chat_id
            
            if chat_id not in active_bulk_data or not active_bulk_data[chat_id]["pending"]:
                return await query.answer("❌ Tidak ada nomor.", show_alert=True)
                
            await query.answer("🔄 Membatalkan nomor lama...")
            
            if chat_id in active_bulk_data: active_bulk_data[chat_id]["batch_id"] = "CANCELLED"
            pending_olds = list(active_bulk_data[chat_id]["pending"].keys())
            
            tasks = [batal_order_api(oid) for oid in pending_olds]
            cancel_results = await asyncio.gather(*tasks, return_exceptions=True)
            success_cancel = sum(1 for res in cancel_results if isinstance(res, dict) and res.get("success"))
                
            if success_cancel == 0:
                return await send_temp_msg(context, chat_id, "❌ Gagal batal (mungkin OTP sudah masuk).")
                
            await query.edit_message_text(f"♻️ Batalkan {success_cancel} nomor. Membeli pengganti...", parse_mode='HTML')
            
            results, failed_count, err = await beli_banyak_api_async(pid, user_id, harga, success_cancel)
            if results:
                ordered_list = [(oid, format_phone(n)) for oid, n in results]
                active_bulk_data.pop(chat_id, None) 
                
                summary_teks = build_bulk_summary(ordered_list, set(), harga, TIMEOUT_SECONDS)
                msg = await query.edit_message_text(summary_teks, parse_mode='HTML')
                batch_id = f"{user_id}_{int(time.time())}"
                asyncio.create_task(bulk_monitor_task(context, chat_id, batch_id, ordered_list, harga, msg.message_id, pid))
            else:
                await send_temp_msg(context, chat_id, f"❌ Gagal beli pengganti: {err}")

        elif data.startswith('replace_'):
            parts = data.split("_")
            old_oid = parts[1]; pid = int(parts[2]); harga = int(parts[3])
            
            await query.answer("🔄 Ganti nomor...", show_alert=False)
            res, msg = await batal_order_api(old_oid)
            if not res: return await query.answer(f"❌ Gagal: {msg[:30]}", show_alert=True)
                
            pending_messages.pop(old_oid, None)
            try: await query.message.delete()
            except: pass
            
            order_id, nomor_raw = await beli_nomor_api(pid, user_id, harga)
            if order_id:
                nomor_display = format_phone(nomor_raw)
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display, pid, harga))
            else:
                await send_temp_msg(context, query.message.chat_id, f"❌ Gagal: {nomor_raw}")

        elif data.startswith('cancel_'):
            order_id = data.replace('cancel_', '')
            try: await query.message.delete()
            except: pass
            hasil = await batal_order_api(order_id)
            pending_messages.pop(order_id, None)
            
            if hasil["success"]:
                await query.answer("✅ Dibatalkan", show_alert=True)
            else:
                await query.answer(f"❌ Gagal: {hasil['message'][:30]}", show_alert=True)

        elif data.startswith('finish_'):
            order_id = data.replace('finish_', '')
            try: await query.message.delete()
            except: pass
            hasil = await selesai_order_api(order_id)
            pending_messages.pop(order_id, None)
            
            if hasil["success"]:
                await query.answer("✅ Selesai & Lepas Nomor", show_alert=True)
            else:
                await query.answer(f"❌ Gagal: {hasil['message'][:30]}", show_alert=True)

    except Exception as e:
        print(traceback.format_exc())
        try: await query.answer("⚠️ Kesalahan sistem.", show_alert=True)
        except: pass
    finally:
        if is_transaction:
            release_lock(user_id)

# ==========================================
# AUTO RECOVERY
# ==========================================
async def recover_pending_orders(application):
    await asyncio.sleep(5) # Tunggu bot siap
    try:
        conn = sqlite3.connect(DB_PATH)
        # Cari order yang masih ACTIVE
        rows = conn.execute("SELECT order_id, phone, product_id, price FROM orders WHERE status='ACTIVE'").fetchall()
        conn.close()
        
        if not rows: return
        
        recovered = 0
        for oid, phone, pid, price in rows:
            data = await http_get(f"{BASE_URL}/orders/{oid}")
            if data.get("success"):
                od = data["data"]
                if od.get("status") == "ACTIVE":
                    recovered += 1
                    # Mulai task polling OTP di background
                    asyncio.create_task(auto_poll_otp(int(ADMIN_ID), application, oid, format_phone(phone), pid, price))
                else:
                    db_update_order(oid, od.get("status"))
                    
        if recovered > 0:
            await application.bot.send_message(
                chat_id=int(ADMIN_ID),
                text=f"🔄 <b>Auto-Recovery Berhasil</b>\n\nBot mendeteksi server restart. Berhasil melanjutkan pemantauan OTP untuk <b>{recovered}</b> order aktif.",
                parse_mode='HTML'
            )
    except Exception as e:
        print(f"Recovery error: {e}")

# ==========================================
# MAIN RUNNER
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_premium_menu(update, context)

async def post_init(application):
    try: await application.bot.delete_my_commands()
    except: pass
    # Jalankan fungsi Auto-Recovery
    asyncio.create_task(recover_pending_orders(application))

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
