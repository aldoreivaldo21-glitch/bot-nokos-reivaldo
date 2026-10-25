import os
import json
import time
import sqlite3
import urllib.request
import urllib.error
import asyncio
import threading
from collections import defaultdict
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ==========================================
# KONFIGURASI UTAMA
# ==========================================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597")
API_KEY = os.environ.get("SMScode_API_KEY") or os.environ.get("SMSCODE_API_KEY")

BOT_VERSION = "7.3 Verified"
MAX_BULK = 100
BATCH_SIZE = 10
TIMEOUT_MINUTES = 20
TIMEOUT_SECONDS = TIMEOUT_MINUTES * 60

BASE_URL = "https://api.smscode.gg/v1"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")

# --- STATE & CACHE ---
pending_messages = {}
_product_cache = {"data": None, "ts": 0}
_saldo_cache = {"value": -1, "ts": 0}
_processing_users = {}

# --- DATABASE GLOBAL ---
_db_conn = None
_db_lock = threading.Lock()

def get_db():
    global _db_conn
    with _db_lock:
        if _db_conn is None:
            _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
            _db_conn.execute("PRAGMA journal_mode=WAL")
        return _db_conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY, user_id INTEGER, phone TEXT, product_id INTEGER,
        price INTEGER, status TEXT, otp TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS templates (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        level TEXT, message TEXT, user_id INTEGER
    )''')
    conn.commit()

# --- HELPER ---
def format_rupiah(n):
    try: return f"{int(n):,}".replace(",", ".")
    except: return str(n)

def format_countdown(seconds):
    if seconds < 0: seconds = 0
    m = seconds // 60
    s = seconds % 60
    return f"{m:02d}:{s:02d}"

def format_phone(p):
    if p and p.startswith("62"): return p[2:]
    return p

def is_admin(uid): return str(uid) == str(ADMIN_ID)

def try_acquire_lock(uid):
    now = time.time()
    if now - _processing_users.get(uid, 0) < 10:
        return False
    _processing_users[uid] = now
    return True

def release_lock(uid): _processing_users.pop(uid, None)

def get_progress_bar(elapsed, total, length=10):
    if total <= 0: return "░" * length
    filled = int(length * elapsed / total)
    return "█" * filled + "░" * (length - filled)

def db_save_log(level, message, user_id=None):
    try:
        get_db().execute("INSERT INTO logs (level, message, user_id) VALUES (?, ?, ?)", (level, message[:500], user_id))
        get_db().commit()
    except: pass

def db_save_order(order_id, user_id, phone, product_id, price):
    try:
        get_db().execute("INSERT OR REPLACE INTO orders (order_id, user_id, phone, product_id, price, status) VALUES (?, ?, ?, ?, ?, 'ACTIVE')",
                     (str(order_id), user_id, phone, product_id, price))
        get_db().commit()
    except Exception as e: print(f"DB Error: {e}")

def db_update_order(order_id, status, otp=None):
    try:
        if otp: get_db().execute("UPDATE orders SET status=?, otp=? WHERE order_id=?", (status, otp, str(order_id)))
        else: get_db().execute("UPDATE orders SET status=? WHERE order_id=?", (status, str(order_id)))
        get_db().commit()
    except Exception as e: print(f"DB Error: {e}")

def get_template(key, default=""):
    try:
        row = get_db().execute("SELECT value FROM templates WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    except: return default

# --- HTTP ASYNC ---
def _http_get(url):
    req = urllib.request.Request(url, headers=HEADERS, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
        except: return {"error": f"HTTP {e.code}"}
    except Exception as e: return {"error": str(e)}

def _http_post(url, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=HEADERS, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
        except: return {"error": f"HTTP {e.code}"}
    except Exception as e: return {"error": str(e)}

async def http_get(url): return await asyncio.to_thread(_http_get, url)
async def http_post(url, payload): return await asyncio.to_thread(_http_post, url, payload)

# --- API & CACHE ---
async def get_gojek_products(force=False):
    global _product_cache
    now = time.time()
    if not force and _product_cache["data"] is not None and (now - _product_cache["ts"]) < 30:
        return _product_cache["data"], None
    
    data = await http_get(f"{BASE_URL}/catalog/products?country_id=7")
    if "error" in data: return None, data["error"]
    if not data.get("success"): return None, "Gagal ambil data."
    
    gojek = [p for p in data.get("data", []) if "Gojek" in p.get("name", "")]
    gojek.sort(key=lambda x: x.get('price', 999999))
    _product_cache["data"] = gojek
    _product_cache["ts"] = now
    return gojek, None

async def cek_saldo_api():
    global _saldo_cache
    now = time.time()
    if _saldo_cache["value"] >= 0 and (now - _saldo_cache["ts"]) < 30:
        return f"💰 Saldo Saya: <b>Rp {format_rupiah(_saldo_cache['value'])}</b>", _saldo_cache["value"]
    
    data = await http_get(f"{BASE_URL}/balance")
    if data.get("success"):
        saldo = data["data"].get("balance", 0)
        _saldo_cache["value"] = saldo
        _saldo_cache["ts"] = now
        return f"💰 Saldo Saya: <b>Rp {format_rupiah(saldo)}</b>", saldo
    return "⚠️ Gagal ambil saldo.", -1

def invalidate_saldo_cache():
    global _saldo_cache
    _saldo_cache["value"] = -1
    _saldo_cache["ts"] = 0

# --- CORE LOGIC (BELI, BATAL, GANTI) ---
async def beli_nomor_api(product_id, user_id, price):
    data = await http_post(f"{BASE_URL}/orders/create", {"product_id": product_id})
    if data.get("success"):
        od = data["data"]["orders"][0]
        oid = str(od["id"]); nomor = od["phone_number"]
        db_save_order(oid, user_id, nomor, product_id, price)
        invalidate_saldo_cache()
        return oid, nomor
    return None, data.get("error", "Gagal membeli")

async def batal_order_api(order_id):
    data = await http_post(f"{BASE_URL}/orders/cancel", {"id": int(order_id)})
    if data.get("success"):
        db_update_order(order_id, "CANCELLED")
        invalidate_saldo_cache()
        return True, "Berhasil"
    
    err_msg = data.get("error", "Gagal membatalkan")
    return False, err_msg

async def selesai_order_api(order_id):
    data = await http_post(f"{BASE_URL}/orders/finish", {"id": int(order_id)})
    if data.get("success"):
        db_update_order(order_id, "COMPLETED")
        return True, "Berhasil"
    return False, data.get("error", "Gagal selesai")

# ==========================================
# FITUR POLLING OTP SATUAN (CEPAT 2 DETIK)
# ==========================================
async def auto_poll_otp(chat_id, context, order_id, nomor_display, pid, price):
    total = TIMEOUT_SECONDS
    elapsed = 0
    msg_id = None
    
    while elapsed < total:
        await asyncio.sleep(2) 
        elapsed += 2
        remaining = total - elapsed
        
        # Cek apakah dibatalkan paksa
        if order_id not in pending_messages and msg_id is not None:
            return 
            
        data = await http_get(f"{BASE_URL}/orders/{order_id}")
        if not data.get("success"): continue
        
        od = data["data"]
        otp = od.get("otp_code")
        status = od.get("status")
        
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
                    [InlineKeyboardButton("🔄 Ganti Nomor (OTP Macet)", callback_data=f"replace_{order_id}_{pid}_{price}")],
                    [InlineKeyboardButton("❌ Batalkan", callback_data=f"cancel_{order_id}")]
                ]
                await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                    text=f"⏳ <b>Status:</b> Menunggu SMS...\n⏱️ Sisa waktu: <b>{format_countdown(remaining)}</b>",
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
            except: pass
            
        elif status == "ACTIVE" and not msg_id:
            try:
                kb = [
                    [InlineKeyboardButton("🔄 Ganti Nomor (OTP Macet)", callback_data=f"replace_{order_id}_{pid}_{price}")],
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

# ==========================================
# FITUR BULK ORDER (BELI BANYAK)
# ==========================================
async def process_bulk_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, pid, price, qty = query.data.split("_")
    pid, price, qty, uid = int(pid), int(price), int(qty), query.from_user.id
    
    if not try_acquire_lock(uid):
        return await query.answer("⏳ Mohon tunggu, transaksi sebelumnya sedang diproses.", show_alert=True)
        
    try:
        await query.edit_message_text(f"⏳ <b>Memproses {qty} nomor...</b>", parse_mode='HTML')
        results, remaining, batch = [], qty, 0
        
        while remaining > 0:
            batch += 1
            res = await http_post(f"{BASE_URL}/orders/create", {"product_id": pid, "quantity": min(BATCH_SIZE, remaining)})
            if "error" in res and batch == 1: 
                return await query.edit_message_text(f"❌ Gagal: {res['error']}")
                
            if res.get("success"):
                orders = res["data"].get("orders", [])
                for od in orders:
                    oid, phone = str(od["id"]), format_phone(od["phone_number"])
                    results.append((oid, phone))
                    db_save_order(oid, uid, phone, pid, price)
                remaining -= (len(orders) + res["data"].get("failed_count", 0))
                if not orders: break
            else: 
                break
            if remaining > 0: 
                await asyncio.sleep(3)
            
        if not results: 
            return await query.edit_message_text("❌ Tidak ada nomor yang didapatkan.")
            
        await query.message.delete()
        # MENGGUNAKAN asyncio.create_task AGAR TIDAK MEMBLOKIR BOT
        asyncio.create_task(start_bulk_monitor(query.message.chat_id, results, price, context))
        invalidate_saldo_cache()
    finally:
        release_lock(uid)

async def start_bulk_monitor(chat_id, ordered_list, price, context):
    total = len(ordered_list)
    received_set, pending = set(), {oid: phone for oid, phone in ordered_list}
    start_time, summary_msg_id = time.time(), None
    
    while pending and (time.time() - start_time) < TIMEOUT_SECONDS:
        sisa = TIMEOUT_SECONDS - (time.time() - start_time)
        bar = get_progress_bar(len(received_set), total, 12)
        
        teks = f"📦 <b>BULK SUMMARY</b>\n\n📊 Progress: <code>[{bar}]</code> <b>{len(received_set)}/{total}</b>\n⏱️ Sisa: <b>{format_countdown(sisa)}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for oid, phone in list(pending.items())[:5]: 
            teks += f"⏳ <code>{phone}</code>\n"
        if len(pending) > 5: 
            teks += f"<i>...dan {len(pending)-5} lainnya</i>"
            
        kb = [[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")]]
        try:
            if not summary_msg_id:
                msg = await context.bot.send_message(chat_id, teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                summary_msg_id = msg.message_id
            else:
                await context.bot.edit_message_text(chat_id, summary_msg_id, teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        except: pass

        res = await http_get(f"{BASE_URL}/orders/active")
        active = res.get("data", []) if res.get("success") else []
        to_remove = []
        
        for oid, phone in pending.items():
            match = next((o for o in active if str(o["id"]) == oid), None)
            if match and match.get("otp_code"):
                otp = match["otp_code"]
                received_set.add(oid); to_remove.append(oid)
                db_update_order(oid, "OTP_RECEIVED", otp)
                try:
                    await context.bot.send_message(chat_id, f"🔑 <b>OTP MASUK!</b>\n\n📱 Nomor: <code>{phone}</code>\n🔑 Kode: <b><code>{otp}</code></b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{oid}")]]), parse_mode='HTML')
                except: pass
                
        for oid in to_remove: 
            pending.pop(oid, None)
            
        if not pending: 
            break
        await asyncio.sleep(3)
        
    if pending:
        for oid, _ in pending.items():
            await batal_order_api(oid)
        if summary_msg_id:
            try: await context.bot.edit_message_text(chat_id, summary_msg_id, f"⌛ <b>WAKTU HABIS</b>\nSisa {len(pending)} nomor di-refund.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")]]), parse_mode='HTML')
            except: pass
    else:
        if summary_msg_id:
            try: await context.bot.edit_message_text(chat_id, summary_msg_id, f"🎉 <b>SEMUA OTP DITERIMA!</b>\nTotal {total} nomor berhasil.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")]]), parse_mode='HTML')
            except: pass

# ==========================================
# MENUS & UI
# ==========================================
def kb_menu_utama():
    return [
        [InlineKeyboardButton("🛒 Beli Satuan", callback_data='beli_nomor')],
        [InlineKeyboardButton("📦 Beli Banyak (Bulk)", callback_data='bulk_menu')],
        [InlineKeyboardButton("💼 Akun Saya", callback_data='akun_saya'),
         InlineKeyboardButton("📜 Riwayat", callback_data='riwayat_menu')],
        [InlineKeyboardButton("ℹ️ Info & Bantuan", callback_data='info_bantuan')]
    ]

def kb_akun():
    return [
        [InlineKeyboardButton("💰 Saldo", callback_data='cek_saldo'),
         InlineKeyboardButton("📦 Order Aktif", callback_data='aktif')],
        [InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]
    ]

async def start(update, context):
    welcome = get_template("welcome", "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>")
    user = update.effective_user.first_name or "Bos"
    _, s = await cek_saldo_api()
    
    teks = (f"{welcome}\n\n👋 Halo, <b>{user}</b>!\n\n━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Saldo Saya:</b> Rp {format_rupiah(s) if s >= 0 else '-'}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💡 <i>Bot otomatis membatalkan & refund jika OTP tidak masuk dalam {TIMEOUT_MINUTES} menit.</i>")
    
    if update.message:
        await update.message.reply_text(teks, reply_markup=InlineKeyboardMarkup(kb_menu_utama()), parse_mode='HTML')
    else:
        await update.callback_query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_menu_utama()), parse_mode='HTML')

# ==========================================
# HANDLER UTAMA
# ==========================================
async def button_handler(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    
    # Mencegah error "query is too old" saat tombol diklik
    try: await query.answer()
    except: pass
    
    if not is_admin(user_id):
        return await query.edit_message_text("🚫 Akses ditolak.")
        
    data = query.data
    try:
        if data == 'menu_utama': await start(update, context)
        
        elif data == 'akun_saya':
            _, s = await cek_saldo_api()
            teks = (f"💼 <b>Akun Saya</b>\n\n🆔 ID: <code>{user_id}</code>\n"
                    f"💰 <b>Saldo:</b> Rp {format_rupiah(s) if s >= 0 else '-'}")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_akun()), parse_mode='HTML')
            
        elif data == 'cek_saldo':
            teks, _ = await cek_saldo_api()
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_akun()), parse_mode='HTML')

        elif data == 'beli_nomor':
            await query.edit_message_text("⏳ Mengambil katalog...", parse_mode='HTML')
            products, err = await get_gojek_products()
            if err: return await query.edit_message_text(f"❌ {err}")
            
            keyboard = []
            for p in products:
                if p.get('available', 0) > 0:
                    keyboard.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(p['price'])} | 📦 {p['available']}", callback_data=f"buy_{p['id']}_{p['price']}")])
            keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')])
            await query.edit_message_text("🛒 <b>Pilih Harga (Satuan):</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

        elif data == 'bulk_menu':
            await query.edit_message_text("⏳ Mengambil katalog bulk...", parse_mode='HTML')
            products, err = await get_gojek_products()
            if err: return await query.edit_message_text(f"❌ {err}")
            
            keyboard = []
            for p in products:
                if p.get('available', 0) >= 5: 
                    keyboard.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(p['price'])}", callback_data=f"bulkprice_{p['id']}_{p['price']}")])
            keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')])
            await query.edit_message_text("📦 <b>Pilih Harga (Bulk):</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

        elif data.startswith('bulkprice_'):
            _, pid, price = data.split("_")
            kb = [
                [InlineKeyboardButton("5 Nomor", callback_data=f"bulkqty_{pid}_{price}_5"), 
                 InlineKeyboardButton("10 Nomor", callback_data=f"bulkqty_{pid}_{price}_10")],
                [InlineKeyboardButton("20 Nomor", callback_data=f"bulkqty_{pid}_{price}_20"), 
                 InlineKeyboardButton("50 Nomor", callback_data=f"bulkqty_{pid}_{price}_50")],
                [InlineKeyboardButton("🔙 Kembali", callback_data="bulk_menu")]
            ]
            await query.edit_message_text(f"📦 <b>Bulk Order</b>\n\nHarga: Rp {format_rupiah(int(price))}/pcs\nPilih jumlah:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

        elif data.startswith('bulkqty_'):
            await process_bulk_order(update, context)

        elif data.startswith('buy_'):
            if not try_acquire_lock(user_id):
                return await query.answer("⏳ Mohon tunggu, transaksi sebelumnya sedang diproses.", show_alert=True)
            
            parts = data.split("_")
            pid, harga = int(parts[1]), int(parts[2])
            await query.edit_message_text("⏳ Sedang membeli nomor...", parse_mode='HTML')
            
            oid, nomor = await beli_nomor_api(pid, user_id, harga)
            if oid:
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, oid, format_phone(nomor), pid, harga))
                await query.message.delete()
            else:
                await query.edit_message_text(f"❌ Gagal: {nomor}")
            release_lock(user_id)

        elif data.startswith('replace_'):
            parts = data.split("_")
            old_oid, pid, harga = parts[1], int(parts[2]), int(parts[3])
            
            await query.edit_message_text("🔄 Membatalkan nomor lama & membeli baru...", parse_mode='HTML')
            res, msg = await batal_order_api(old_oid)
            
            if not res:
                return await query.answer(f"❌ Gagal batal: {msg[:50]}", show_alert=True)
                
            pending_messages.pop(old_oid, None)
            oid, nomor = await beli_nomor_api(pid, user_id, harga)
            if oid:
                await query.message.delete()
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, oid, format_phone(nomor), pid, harga))
            else:
                await query.edit_message_text(f"❌ Gagal ganti nomor: {nomor}")

        elif data.startswith('cancel_'):
            oid = data.replace('cancel_', '')
            res, msg = await batal_order_api(oid)
            if res:
                await query.answer("✅ Berhasil Dibatalkan", show_alert=True)
                try: await query.message.delete()
                except: pass
                pending_messages.pop(oid, None)
            else:
                if "cancel_too_early" in str(msg).lower():
                    await query.answer("⏳ Tunggu minimal 2 menit dari pembelian.", show_alert=True)
                else:
                    await query.answer(f"❌ Gagal batal: {msg[:50]}", show_alert=True)

        elif data.startswith('finish_'):
            oid = data.replace('finish_', '')
            res, msg = await selesai_order_api(oid)
            if res:
                await query.answer("✅ Selesai & Lepas Nomor", show_alert=True)
                try: await query.message.delete()
                except: pass
                pending_messages.pop(oid, None)
            else:
                await query.answer(f"❌ Gagal: {msg[:50]}", show_alert=True)

        elif data == 'riwayat_menu':
            rows = get_db().execute("SELECT phone, status, otp FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (user_id,)).fetchall()
            teks = "📜 <b>10 Riwayat Terakhir:</b>\n\n"
            for phone, status, otp in rows:
                emoji = "✅" if status=="COMPLETED" else ("🔑" if otp else ("❌" if status=="CANCELLED" else "⏳"))
                teks += f"{emoji} <code>{format_phone(phone)}</code> | {status} | {otp or '-'}\n"
            if not rows: teks = "Belum ada riwayat."
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]]), parse_mode='HTML')

        elif data == 'aktif':
            data_act = await http_get(f"{BASE_URL}/orders/active")
            orders = data_act.get("data", []) if data_act.get("success") else []
            if not orders: return await query.edit_message_text("📭 Tidak ada order aktif.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='akun_saya')]]))
            
            kb = []
            for o in orders[:10]:
                kb.append([InlineKeyboardButton(f"❌ Batal {format_phone(o['phone_number'])}", callback_data=f"cancel_{o['id']}")])
            kb.append([InlineKeyboardButton("🔙 Kembali", callback_data='akun_saya')])
            await query.edit_message_text("📦 <b>Order Aktif (Pilih untuk batal):</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

        elif data == 'info_bantuan':
            teks = (f"ℹ️ <b>Bantuan</b>\n\n"
                    f"1. Beli nomor di menu utama.\n"
                    f"2. Tunggu OTP masuk otomatis (cek tiap 2 detik).\n"
                    f"3. Jika macet, klik Ganti Nomor.\n"
                    f"4. Auto-refund dalam {TIMEOUT_MINUTES} menit.")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]]), parse_mode='HTML')

    except Exception as e:
        db_save_log("ERROR", str(e), user_id)
        try: await query.edit_message_text("⚠️ Terjadi kesalahan sistem.")
        except: pass

# --- MAIN RUNNER ---
async def post_init(application):
    commands = [BotCommand("start", "🚀 Menu utama"), BotCommand("help", "❓ Bantuan")]
    try: await application.bot.set_my_commands(commands)
    except: pass

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    port = int(os.environ.get("PORT", 8080))
    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("SPACE_HOST")
    
    if hostname:
        print(f"🚀 Running Webhook on {port}")
        application.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"https://{hostname}/{TOKEN}")
    else:
        print("🔌 Running Polling Mode")
        application.run_polling()
