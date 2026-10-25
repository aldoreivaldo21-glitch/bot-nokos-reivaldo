import os
import json
import re
import time
import sqlite3
import asyncio
import urllib.request
import urllib.error
from collections import defaultdict
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ==========================================
# KONFIGURASI UTAMA
# ==========================================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
API_KEY = os.environ.get("SMScode_API_KEY") or os.environ.get("SMSCODE_API_KEY")
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597")
BASE_URL = "https://api.smscode.gg/v1"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

DB_PATH = "bot_data.db"
TIMEOUT_MINUTES = 20
TIMEOUT_SECONDS = TIMEOUT_MINUTES * 60
MAX_BULK = 100
BATCH_SIZE = 10
PRODUCT_CACHE_TTL = 30
PROCESSING_LOCK_SECONDS = 10

# Global State (Mencegah Memory Leak & Double Click)
_product_cache = {"data": None, "ts": 0}
_processing_users = {}
pending_messages = {}
_db_conn = None

# ==========================================
# HELPER & FORMATTING
# ==========================================
def format_rupiah(n):
    try: return f"{int(n):,}".replace(",", ".")
    except: return str(n)

def format_countdown(seconds):
    if seconds < 0: seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

def get_progress_bar(current, total, length=10):
    if total <= 0: return "░" * length
    filled = int(length * current / total)
    return "█" * filled + "░" * (length - filled)

def get_timer_emoji(remaining):
    if remaining > 600: return "🟢"
    if remaining > 300: return "🟡"
    return "🔴"

def format_phone(p):
    if p and p.startswith("62"): return p[2:]
    return p

def is_admin(uid): return str(uid) == str(ADMIN_ID)

def try_acquire_lock(uid):
    now = time.time()
    if now - _processing_users.get(uid, 0) < PROCESSING_LOCK_SECONDS: return False
    _processing_users[uid] = now
    return True

def release_lock(uid): _processing_users.pop(uid, None)

# ==========================================
# DATABASE (Global Connection + WAL)
# ==========================================
def get_db():
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
    return _db_conn

def init_db():
    conn = get_db()
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
    conn.commit()

def db_save_order(order_id, user_id, phone, product_id, price):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO orders (order_id, user_id, phone, product_id, price, status) VALUES (?, ?, ?, ?, ?, 'ACTIVE')",
                 (str(order_id), user_id, phone, product_id, price))
    conn.commit()

def db_update_order(order_id, status, otp=None):
    conn = get_db()
    if otp: conn.execute("UPDATE orders SET status=?, otp=? WHERE order_id=?", (status, otp, str(order_id)))
    else: conn.execute("UPDATE orders SET status=? WHERE order_id=?", (status, str(order_id)))
    conn.commit()

def db_get_history(user_id, limit=10):
    conn = get_db()
    return conn.execute("SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()

def db_watchlist_add(user_id, product_id, price):
    conn = get_db()
    conn.execute("INSERT INTO watchlist (user_id, product_id, price) VALUES (?, ?, ?)", (user_id, product_id, price))
    conn.commit()

def db_watchlist_remove(wid):
    conn = get_db()
    conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
    conn.commit()

def db_watchlist_get():
    conn = get_db()
    return conn.execute("SELECT id, user_id, product_id, price FROM watchlist").fetchall()

def get_template(key, default=""):
    conn = get_db()
    row = conn.execute("SELECT value FROM templates WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def set_template(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO templates (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

# ==========================================
# API WRAPPER (Asinkronus / Non-Blocking)
# ==========================================
def req_api(endpoint, method="GET", payload=None):
    url = f"{BASE_URL}{endpoint}"
    data = json.dumps(payload).encode('utf-8') if payload else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        except: return {"error": f"HTTP {e.code}"}
    except Exception as e: return {"error": str(e)}

async def api_get(endpoint):
    return await asyncio.to_thread(req_api, endpoint, "GET")

async def api_post(endpoint, payload):
    return await asyncio.to_thread(req_api, endpoint, "POST", payload)

def get_gojek_products(force=False):
    global _product_cache
    now = time.time()
    if not force and _product_cache["data"] and (now - _product_cache["ts"]) < PRODUCT_CACHE_TTL:
        return _product_cache["data"], None
        
    res = req_api("/catalog/products?country_id=7")
    if "error" in res: return None, res["error"]
    if not res.get("success"): return None, "Gagal mengambil data."
    
    products = [p for p in res.get("data", []) if "Gojek" in p.get("name", "")]
    products.sort(key=lambda x: x.get('price', 999999))
    _product_cache["data"] = products
    _product_cache["ts"] = now
    return products, None

def get_active_orders():
    return req_api("/orders/active")

# ==========================================
# KEYBOARDS
# ==========================================
def kb_menu_utama():
    return [
        [InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')],
        [InlineKeyboardButton("💼 Akun Saya", callback_data='akun_saya'),
         InlineKeyboardButton("⭐ Favorit", callback_data='favorit')],
        [InlineKeyboardButton("📜 Riwayat", callback_data='riwayat_all'),
         InlineKeyboardButton("ℹ️ Info", callback_data='info_bantuan')]
    ]

def kb_akun():
    return [
        [InlineKeyboardButton("💰 Cek Saldo", callback_data='cek_saldo')],
        [InlineKeyboardButton("📦 Order Aktif", callback_data='aktif')],
        [InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]
    ]

# ==========================================
# HANDLERS: UTAMA & MENU
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.first_name
    welcome = get_template("welcome", "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>")
    
    res = await api_get("/balance")
    saldo = res.get("data", {}).get("balance", 0) if res.get("success") else 0
    
    teks = (
        f"{welcome}\n\n"
        f"👋 Halo, <b>{user}</b>!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Saldo Anda:</b> Rp {format_rupiah(saldo)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Silakan pilih menu di bawah:"
    )
    if update.message:
        await update.message.reply_text(teks, reply_markup=InlineKeyboardMarkup(kb_menu_utama()), parse_mode='HTML')
    else:
        await update.callback_query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_menu_utama()), parse_mode='HTML')

async def menu_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer("Mengecek...")
    res = await api_get("/balance")
    saldo = res.get("data", {}).get("balance", 0) if res.get("success") else 0
    await query.edit_message_text(f"💰 <b>Saldo Anda:</b>\n\n<b>Rp {format_rupiah(saldo)}</b>", reply_markup=InlineKeyboardMarkup(kb_akun()), parse_mode='HTML')

async def menu_beli(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    products, err = get_gojek_products()
    
    if err: return await query.edit_message_text(f"❌ Gagal memuat: <code>{err}</code>", parse_mode='HTML')
    
    kb = []
    seen = set()
    for p in products:
        if p['price'] not in seen and p.get('available', 0) > 0:
            seen.add(p['price'])
            kb.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(p['price'])} | 📦 {p['available']}", callback_data=f"buy_{p['id']}_{p['price']}")])
            
    kb.append([InlineKeyboardButton("📦 Beli Banyak (Bulk)", callback_data='bulk_menu')])
    kb.append([InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')])
    
    if len(kb) <= 2: return await query.edit_message_text("📭 Stok kosong.", reply_markup=InlineKeyboardMarkup(kb))
    await query.edit_message_text("🛒 <b>Pilih Harga Nomor Gojek:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

# ==========================================
# CORE: BELI NOMOR & POLLING SUPER CEPAT
# ==========================================
async def buy_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    
    if not try_acquire_lock(uid):
        return await query.answer("⏳ Tunggu transaksi sebelumnya selesai.", show_alert=True)
        
    try:
        _, pid, price = query.data.split("_")
        price = int(price)
        
        await query.edit_message_text("⏳ <b>Memproses pesanan...</b>", parse_mode='HTML')
        res = await api_post("/orders/create", {"product_id": int(pid)})
        
        if not res.get("success"):
            err = res.get("error", res.get("message", "Gagal"))
            return await query.edit_message_text(f"❌ Gagal membeli: <code>{err}</code>", parse_mode='HTML')
            
        order = res["data"]["orders"][0]
        oid, phone = str(order["id"]), format_phone(order["phone_number"])
        db_save_order(oid, uid, phone, int(pid), price)
        
        await show_live_order(query.message.chat_id, oid, phone, int(pid), price, context)
    finally:
        release_lock(uid)

async def show_live_order(chat_id, oid, phone, pid, price, context):
    sisa = TIMEOUT_SECONDS
    msg_id = None
    
    # Cek setiap 2 detik (Super Cepat)
    for loop in range(TIMEOUT_SECONDS // 2):
        await asyncio.sleep(2) 
        sisa -= 2
        
        res = await api_get(f"/orders/{oid}")
        if not res.get("success"): continue
        
        otp, status = res["data"].get("otp_code"), res["data"].get("status")
        
        if otp:
            db_update_order(oid, "OTP_RECEIVED", otp)
            kb = [[InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{oid}")]]
            try:
                await context.bot.send_message(chat_id, f"🔑 <b>OTP DITERIMA!</b>\n\n📱 Nomor: <code>{phone}</code>\n🔑 Kode: <b><code>{otp}</code></b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
            except: pass
            if msg_id:
                try: await context.bot.delete_message(chat_id, msg_id)
                except: pass
            return
            
        elif status in ["CANCELLED", "EXPIRED"]:
            if msg_id:
                try: await context.bot.delete_message(chat_id, msg_id)
                except: pass
            return
            
        elif loop % 3 == 0: # Update UI tiap 6 detik
            bar = get_progress_bar(TIMEOUT_SECONDS - sisa, TIMEOUT_SECONDS, 10)
            teks = (
                f"⏳ <b>MENUNGGU OTP...</b>\n\n"
                f"📱 Nomor: <code>{phone}</code>\n"
                f"{get_timer_emoji(sisa)} Sisa: <b>{format_countdown(sisa)}</b>\n"
                f"<code>[{bar}]</code>"
            )
            kb = [
                [InlineKeyboardButton("🔄 Ganti Nomor Baru", callback_data=f"replace_{oid}_{pid}_{price}")],
                [InlineKeyboardButton("❌ Batalkan", callback_data=f"cancel_{oid}")]
            ]
            try:
                if not msg_id:
                    msg = await context.bot.send_message(chat_id, teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                    msg_id = msg.message_id
                else:
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
            except: pass

    # Habis waktu
    await api_post("/orders/cancel", {"id": int(oid)})
    db_update_order(oid, "CANCELLED")
    if msg_id:
        try: await context.bot.edit_message_text(chat_id, message_id=msg_id, text=f"⌛ <b>WAKTU HABIS</b>\n\nNomor {phone} di-refund otomatis.", parse_mode='HTML')
        except: pass

# ==========================================
# FITUR REPLACE NOMOR (GANTI NOMOR)
# ==========================================
async def replace_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer("Memproses...")
    _, old_oid, pid, price = query.data.split("_")
    
    await query.edit_message_text("🔄 <b>Membatalkan nomor lama...</b>", parse_mode='HTML')
    res = await api_post("/orders/cancel", {"id": int(old_oid)})
    
    if not res.get("success"):
        err = res.get("error", res.get("message", "Gagal"))
        return await query.edit_message_text(f"❌ Gagal ganti nomor: <code>{err}</code>\n<i>(Sistem jeda 2 menit)</i>", parse_mode='HTML')
        
    db_update_order(old_oid, "CANCELLED")
    await query.edit_message_text("♻️ <b>Membeli nomor baru...</b>", parse_mode='HTML')
    
    res_buy = await api_post("/orders/create", {"product_id": int(pid)})
    if not res_buy.get("success"):
        return await query.edit_message_text("❌ Gagal membeli nomor baru.", parse_mode='HTML')
        
    order = res_buy["data"]["orders"][0]
    new_oid, new_phone = str(order["id"]), format_phone(order["phone_number"])
    db_save_order(new_oid, query.from_user.id, new_phone, int(pid), int(price))
    
    await query.message.delete()
    await show_live_order(query.message.chat_id, new_oid, new_phone, int(pid), int(price), context)

# ==========================================
# INSTANT DELETE (BATAL & SELESAI)
# ==========================================
async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    oid = query.data.split("_")[1]
    
    await api_post("/orders/cancel", {"id": int(oid)})
    db_update_order(oid, "CANCELLED")
    pending_messages.pop(oid, None)
    
    # Hapus instan & Pop-up Alert
    try: await query.message.delete()
    except: pass
    await query.answer("✅ Berhasil dibatalkan. Saldo dikembalikan.", show_alert=True)

async def finish_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    oid = query.data.split("_")[1]
    
    await api_post("/orders/finish", {"id": int(oid)})
    db_update_order(oid, "COMPLETED")
    pending_messages.pop(oid, None)
    
    # Hapus instan & Pop-up Alert
    try: await query.message.delete()
    except: pass
    await query.answer("✅ Selesai. Nomor telah dilepas.", show_alert=True)

# ==========================================
# BULK ORDER (BELI BANYAK)
# ==========================================
async def menu_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    products, _ = get_gojek_products()
    kb = []
    seen = set()
    for p in products:
        if p['price'] not in seen and p.get('available', 0) >= 5:
            seen.add(p['price'])
            kb.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(p['price'])}", callback_data=f"bulkprice_{p['id']}_{p['price']}")])
    kb.append([InlineKeyboardButton("🔙 Kembali", callback_data="beli_nomor")])
    await query.edit_message_text("📦 <b>BULK ORDER</b>\n\nPilih harga:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

async def bulk_qty_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, pid, price = query.data.split("_")
    kb = [
        [InlineKeyboardButton("5", callback_data=f"bulkqty_{pid}_{price}_5"), InlineKeyboardButton("10", callback_data=f"bulkqty_{pid}_{price}_10"), InlineKeyboardButton("20", callback_data=f"bulkqty_{pid}_{price}_20")],
        [InlineKeyboardButton("50", callback_data=f"bulkqty_{pid}_{price}_50"), InlineKeyboardButton("100", callback_data=f"bulkqty_{pid}_{price}_100")],
        [InlineKeyboardButton("🔙 Kembali", callback_data="bulk_menu")]
    ]
    await query.edit_message_text(f"📦 Pilih jumlah (Rp {format_rupiah(price)}/pcs):", reply_markup=InlineKeyboardMarkup(kb))

async def process_bulk_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, pid, price, qty = query.data.split("_")
    pid, price, qty, uid = int(pid), int(price), int(qty), query.from_user.id
    
    if not try_acquire_lock(uid):
        return await query.answer("⏳ Tunggu transaksi sebelumnya.", show_alert=True)
        
    try:
        await query.edit_message_text(f"⏳ <b>Memproses {qty} nomor...</b>", parse_mode='HTML')
        results, remaining, batch = [], qty, 0
        
        while remaining > 0:
            batch += 1
            res = await api_post("/orders/create", {"product_id": pid, "quantity": min(BATCH_SIZE, remaining)})
            if "error" in res and batch == 1: return await query.edit_message_text(f"❌ Gagal: {res['error']}")
            if res.get("success"):
                orders = res["data"].get("orders", [])
                for od in orders:
                    oid, phone = str(od["id"]), format_phone(od["phone_number"])
                    results.append((oid, phone))
                    db_save_order(oid, uid, phone, pid, price)
                remaining -= (len(orders) + res["data"].get("failed_count", 0))
                if not orders: break
            else: break
            if remaining > 0: await asyncio.sleep(3)
            
        if not results: return await query.edit_message_text("❌ Tidak ada nomor yang didapatkan.")
        await query.message.delete()
        await start_bulk_monitor(query.message.chat_id, results, price, context)
    finally:
        release_lock(uid)

async def start_bulk_monitor(chat_id, ordered_list, price, context):
    total = len(ordered_list)
    received_set, pending = set(), {oid: phone for oid, phone in ordered_list}
    start_time, summary_msg_id = time.time(), None
    
    while pending and (time.time() - start_time) < TIMEOUT_SECONDS:
        sisa = TIMEOUT_SECONDS - (time.time() - start_time)
        bar = get_progress_bar(len(received_set), total, 12)
        
        teks = f"📦 <b>BULK SUMMARY</b>\n\n📊 Progress: <code>[{bar}]</code> <b>{len(received_set)}/{total}</b>\n{get_timer_emoji(sisa)} Sisa: <b>{format_countdown(sisa)}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for oid, phone in list(pending.items())[:5]: teks += f"⏳ <code>{phone}</code>\n"
        if len(pending) > 5: teks += f"<i>...dan {len(pending)-5} lainnya</i>"
            
        kb = [[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")]]
        try:
            if not summary_msg_id:
                msg = await context.bot.send_message(chat_id, teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                summary_msg_id = msg.message_id
            else:
                await context.bot.edit_message_text(chat_id, summary_msg_id, teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        except: pass

        res = await api_get("/orders/active")
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
                
        for oid in to_remove: pending.pop(oid)
        if not pending: break
        await asyncio.sleep(3) # Bulk cek tiap 3 detik
        
    if pending:
        for oid, _ in pending.items():
            await api_post("/orders/cancel", {"id": int(oid)})
            db_update_order(oid, "CANCELLED")
        if summary_msg_id:
            try: await context.bot.edit_message_text(chat_id, summary_msg_id, f"⌛ <b>WAKTU HABIS</b>\nSisa {len(pending)} nomor di-refund.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")]]), parse_mode='HTML')
            except: pass

# ==========================================
# RIWAYAT, WATCHLIST, & INFO
# ==========================================
async def menu_riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    rows = db_get_history(query.from_user.id, 10)
    
    teks = "📜 <b>10 TRANSAKSI TERAKHIR</b>\n\n"
    for oid, phone, status, otp, tgl in rows:
        emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else ("❌" if status == "CANCELLED" else "⏳"))
        teks += f"{emoji} <code>{format_phone(phone)}</code>\n   {status} | OTP: {otp or '-'}\n   {tgl}\n\n"
    if not rows: teks = "📜 Belum ada riwayat."
        
    await query.edit_message_text(teks[:4000], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="menu_utama")]]), parse_mode='HTML')

async def menu_favorit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    rows = db_watchlist_get() # Ambil semua untuk user ini (simplifikasi)
    teks = "⭐ <b>WATCHLIST RESTOCK</b>\n\n"
    if not rows: teks += "Belum ada produk yang dipantau."
    else:
        for wid, uid, pid, price in rows:
            teks += f"🔔 Gojek Rp {format_rupiah(price)}\n"
            
    await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="menu_utama")]]), parse_mode='HTML')

async def info_bantuan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    teks = (
        f"ℹ️ <b>INFO & BANTUAN</b>\n\n"
        f"<b>❓ Kenapa OTP tidak masuk?</b>\nTunggu 5-10 menit. Jika lewat {TIMEOUT_MINUTES} menit, saldo otomatis direfund.\n\n"
        f"<b>❓ Bagaimana cara top-up?</b>\nHubungi admin @mreivaldoo.\n\n"
        f"<b>❓ Apakah saldo bisa hangus?</b>\nTidak. Saldo kembali otomatis jika nomor expired."
    )
    await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="menu_utama")]]), parse_mode='HTML')

# ==========================================
# ADMIN COMMANDS
# ==========================================
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    conn = get_db()
    t = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders").fetchone()
    await update.message.reply_text(f"📊 <b>STATISTIK GLOBAL</b>\n\nTotal Order: {t[0]}\nTotal Nilai: Rp {format_rupiah(t[1])}", parse_mode='HTML')

async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args: return await update.message.reply_text("⚠️ Format: /setwelcome Teks baru")
    set_template("welcome", " ".join(context.args))
    await update.message.reply_text("✅ Sambutan diubah.")

# ==========================================
# BACKGROUND TASKS (RESTOCK ONLY - BACKUP REMOVED)
# ==========================================
async def restock_checker_task(context):
    while True:
        await asyncio.sleep(5 * 60) # Cek tiap 5 menit
        try:
            rows = db_watchlist_get()
            if not rows: continue
            products, err = get_gojek_products(force=True)
            if err or not products: continue
            
            for wid, uid, pid, price in rows:
                for p in products:
                    if p.get('id') == pid and p.get('available', 0) > 0:
                        try:
                            await context.bot.send_message(chat_id=uid,
                                text=f"🎉 <b>Stok Kembali Tersedia!</b>\n\n📦 Gojek\n💵 Harga: Rp {format_rupiah(price)}\n📊 Stok: {p.get('available')}\n\nSegera beli!",
                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Beli Sekarang", callback_data='beli_nomor')]]),
                                parse_mode='HTML')
                        except: pass
                        db_watchlist_remove(wid)
                        break
        except Exception: pass

# ==========================================
# CALLBACK ROUTER
# ==========================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    uid = query.from_user.id
    
    # Cek Admin (Karena bot ini khusus Anda)
    if not is_admin(uid):
        return await query.answer("🚫 Akses ditolak.", show_alert=True)
    
    await query.answer() # Wajib agar loading spinner hilang
    
    try:
        if data == "menu_utama": await start(update, context)
        elif data == "akun_saya": 
            await query.edit_message_text("💼 <b>AKUN SAYA</b>", reply_markup=InlineKeyboardMarkup(kb_akun()), parse_mode='HTML')
        elif data == "cek_saldo": await menu_saldo(update, context)
        elif data == "riwayat_all": await menu_riwayat(update, context)
        elif data == "favorit": await menu_favorit(update, context)
        elif data == "info_bantuan": await info_bantuan(update, context)
        
        elif data == "beli_nomor": await menu_beli(update, context)
        elif data == "bulk_menu": await menu_bulk(update, context)
        
        elif data.startswith("buy_"): await buy_number(update, context)
        elif data.startswith("bulkprice_"): await bulk_qty_menu(update, context)
        elif data.startswith("bulkqty_"): await process_bulk_order(update, context)
        
        elif data.startswith("replace_"): await replace_number(update, context)
        elif data.startswith("cancel_"): await cancel_order(update, context)
        elif data.startswith("finish_"): await finish_order(update, context)
        
    except Exception as e:
        try: await query.edit_message_text(f"⚠️ Error: {str(e)[:100]}", parse_mode='HTML')
        except: pass

# ==========================================
# MAIN
# ==========================================
async def post_init(application):
    commands = [
        BotCommand("start", "🚀 Menu utama"), 
        BotCommand("stats", "📊 Statistik Global (Admin)"),
        BotCommand("setwelcome", "💬 Ubah Sambutan (Admin)")
    ]
    try: await application.bot.set_my_commands(commands)
    except: pass
    asyncio.create_task(restock_checker_task(application))

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Auto-detect Webhook vs Polling
    port = int(os.environ.get("PORT", 8080))
    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("SPACE_HOST")
    
    if hostname:
        print(f"🚀 Menjalankan Mode Webhook di {port}...")
        application.run_webhook(
            listen="0.0.0.0", port=port, url_path=TOKEN,
            webhook_url=f"https://{hostname}/{TOKEN}"
        )
    else:
        print("🔌 Menjalankan Mode Polling (ZevCloud/VPS)...")
        application.run_polling()
