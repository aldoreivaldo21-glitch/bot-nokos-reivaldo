import os
import json
import re
import time
import sqlite3
import asyncio
import urllib.request
import urllib.error
import logging
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
RATE_LIMIT_WINDOW = 10
RATE_LIMIT_MAX = 8
PROCESSING_LOCK_SECONDS = 10

# Setup Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Global State
_product_cache = {"data": None, "ts": 0}
_rate_bucket = defaultdict(list)
_processing_users = {}

# ==========================================
# HELPER & FORMATTING
# ==========================================
def format_rupiah(n):
    try: return f"{int(n):,}".replace(",", ".")
    except: return str(n)

def format_time(seconds):
    m, s = divmod(max(0, int(seconds)), 60)
    return f"{m:02d}:{s:02d}"

def get_progress_bar(elapsed, total, length=10):
    if total <= 0: return "░" * length
    filled = int(length * elapsed / total)
    return "█" * filled + "░" * (length - filled)

def get_timer_emoji(remaining):
    if remaining > 600: return "🟢"
    if remaining > 300: return "🟡"
    return "🔴"

def format_phone(p):
    if p and p.startswith("62"): return p[2:]
    return p

def is_admin(uid): return str(uid) == str(ADMIN_ID)

def check_rate_limit(uid):
    now = time.time()
    calls = _rate_bucket[uid]
    while calls and now - calls[0] > RATE_LIMIT_WINDOW:
        calls.pop(0)
    if len(calls) >= RATE_LIMIT_MAX: return False
    calls.append(now)
    return True

def try_acquire_lock(uid):
    now = time.time()
    if now - _processing_users.get(uid, 0) < PROCESSING_LOCK_SECONDS: return False
    _processing_users[uid] = now
    return True

def release_lock(uid): _processing_users.pop(uid, None)

# ==========================================
# DATABASE
# ==========================================
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db(); c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY, user_id INTEGER, phone TEXT, product_id INTEGER,
        price INTEGER, status TEXT, otp TEXT, refund_amount INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS templates (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER,
        price INTEGER, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        level TEXT, message TEXT, user_id INTEGER
    )''')
    conn.commit(); conn.close()

def db_save_order(order_id, user_id, phone, product_id, price):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO orders (order_id, user_id, phone, product_id, price, status) VALUES (?, ?, ?, ?, ?, 'ACTIVE')",
                 (str(order_id), user_id, phone, product_id, price))
    conn.commit(); conn.close()

def db_update_order(order_id, status, otp=None):
    conn = get_db()
    if otp: conn.execute("UPDATE orders SET status=?, otp=? WHERE order_id=?", (status, otp, str(order_id)))
    else: conn.execute("UPDATE orders SET status=? WHERE order_id=?", (status, str(order_id)))
    conn.commit(); conn.close()

def db_log_error(msg, uid=None):
    try:
        conn = get_db()
        conn.execute("INSERT INTO logs (level, message, user_id) VALUES ('ERROR', ?, ?)", (msg[:500], uid))
        conn.commit(); conn.close()
    except: pass

# ==========================================
# API WRAPPER
# ==========================================
def req_api(endpoint, method="GET", payload=None):
    url = f"{BASE_URL}{endpoint}"
    data = json.dumps(payload).encode('utf-8') if payload else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: 
            err_body = e.read().decode()
            return {"error": f"HTTP {e.code}: {err_body}"}
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
    
    products = [p for p in res.get("data", []) if "Gojek" in p.get("name", "")]
    products.sort(key=lambda x: x.get('price', 999999))
    _product_cache["data"] = products
    _product_cache["ts"] = now
    return products, None

# ==========================================
# KEYBOARDS (UI MODERN)
# ==========================================
def kb_main():
    return [
        [InlineKeyboardButton("🛒 Beli Nomor", callback_data="menu_beli")],
        [InlineKeyboardButton("📦 Beli Banyak (Bulk)", callback_data="menu_bulk")],
        [InlineKeyboardButton("👤 Akun & Riwayat", callback_data="menu_akun")],
        [InlineKeyboardButton("⚙️ Pengaturan", callback_data="menu_setting")]
    ]

def kb_akun():
    return [
        [InlineKeyboardButton("💰 Cek Saldo", callback_data="menu_saldo"),
         InlineKeyboardButton("📜 Riwayat Order", callback_data="menu_riwayat")],
        [InlineKeyboardButton("📦 Order Aktif", callback_data="menu_aktif")],
        [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_utama")]
    ]

def kb_setting():
    return [
        [InlineKeyboardButton("🔔 Notifikasi Restock", callback_data="menu_watchlist")],
        [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_utama")]
    ]

# ==========================================
# HANDLERS: START & MENU
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.first_name
    res = await api_get("/balance")
    saldo = res.get("data", {}).get("balance", 0) if res.get("success") else 0
    
    teks = (
        f"✨ <b>SELAMAT DATANG DI REIVALDO NOKOS</b> ✨\n\n"
        f"Halo <b>{user}</b>! 👋\n"
        f"Layanan penyedia nomor OTP otomatis 24/7.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Saldo Anda:</b> Rp {format_rupiah(saldo)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Silakan pilih menu di bawah ini:"
    )
    if update.message:
        await update.message.reply_text(teks, reply_markup=InlineKeyboardMarkup(kb_main()), parse_mode='HTML')
    else:
        await update.callback_query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_main()), parse_mode='HTML')

async def menu_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer("Mengecek...")
    res = await api_get("/balance")
    saldo = res.get("data", {}).get("balance", 0) if res.get("success") else 0
    
    await query.edit_message_text(
        f"💳 <b>INFORMASI SALDO</b>\n\n💵 <b>Rp {format_rupiah(saldo)}</b>",
        reply_markup=InlineKeyboardMarkup(kb_akun()), parse_mode='HTML'
    )

# ==========================================
# HANDLERS: KATALOG & BELI SATUAN
# ==========================================
async def menu_beli(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    products, err = get_gojek_products()
    
    if err: return await query.edit_message_text(f"❌ Gagal memuat: <code>{err}</code>", parse_mode='HTML')
    
    kb = []
    seen = set()
    for p in products:
        if p['price'] not in seen and p.get('available', 0) > 0:
            seen.add(p['price'])
            kb.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(p['price'])} | 📦 Stok: {p['available']}", callback_data=f"buy_{p['id']}_{p['price']}")])
    kb.append([InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_utama")])
    
    if len(kb) == 1: return await query.edit_message_text("📭 Stok kosong.", reply_markup=InlineKeyboardMarkup(kb))
    await query.edit_message_text("🛒 <b>KATALOG GOJEK (SATUAN)</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

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
            return await query.edit_message_text(f"❌ Gagal: <code>{res.get('error')}</code>", parse_mode='HTML')
            
        order = res["data"]["orders"][0]
        oid, phone = str(order["id"]), format_phone(order["phone_number"])
        db_save_order(oid, uid, phone, int(pid), price)
        
        await show_live_order(query.message.chat_id, oid, phone, int(pid), price, context)
    finally:
        release_lock(uid)

async def show_live_order(chat_id, oid, phone, pid, price, context):
    sisa = TIMEOUT_SECONDS
    msg_id = None
    
    for loop in range(TIMEOUT_SECONDS // 3):
        await asyncio.sleep(3)
        sisa -= 3
        
        res = await api_get(f"/orders/{oid}")
        if not res.get("success"): continue
        
        otp, status = res["data"].get("otp_code"), res["data"].get("status")
        
        if otp:
            db_update_order(oid, "OTP_RECEIVED", otp)
            kb = [[InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{oid}")]]
            try:
                await context.bot.send_message(chat_id, f"🎉 <b>OTP DITERIMA!</b>\n\n📱 Nomor: <code>{phone}</code>\n🔑 Kode: <b><code>{otp}</code></b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
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
            
        elif loop % 3 == 0: 
            bar = get_progress_bar(TIMEOUT_SECONDS - sisa, TIMEOUT_SECONDS, 10)
            teks = f"⏳ <b>MENUNGGU OTP...</b>\n\n📱 Nomor: <code>{phone}</code>\n{get_timer_emoji(sisa)} Sisa: <b>{format_time(sisa)}</b>\n<code>[{bar}]</code>"
            kb = [
                [InlineKeyboardButton("🔄 Ganti Nomor", callback_data=f"replace_{oid}_{pid}_{price}")],
                [InlineKeyboardButton("❌ Batalkan", callback_data=f"cancel_{oid}")]
            ]
            try:
                if not msg_id:
                    msg = await context.bot.send_message(chat_id, teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                    msg_id = msg.message_id
                else:
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
            except: pass

    await api_post("/orders/cancel", {"id": int(oid)})
    db_update_order(oid, "CANCELLED")
    if msg_id:
        try: await context.bot.edit_message_text(chat_id, message_id=msg_id, text=f"⌛ <b>WAKTU HABIS</b>\n\nNomor {phone} di-refund otomatis.", parse_mode='HTML')
        except: pass

# ==========================================
# HANDLERS: REPLACE, CANCEL, FINISH (INSTANT DELETE)
# ==========================================
async def replace_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer("Memproses...")
    _, old_oid, pid, price = query.data.split("_")
    
    await query.edit_message_text("🔄 <b>Membatalkan nomor lama...</b>", parse_mode='HTML')
    res = await api_post("/orders/cancel", {"id": int(old_oid)})
    
    if not res.get("success"):
        return await query.edit_message_text("❌ Gagal ganti nomor (Sistem jeda 2 menit).", parse_mode='HTML')
        
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

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; 
    
    # --- PERUBAHAN DISINI: Pop-up instan & Hapus pesan seketika ---
    await query.answer("✅ Berhasil Dibatalkan. Saldo dikembalikan.", show_alert=True)
    
    oid = query.data.split("_")[1]
    await api_post("/orders/cancel", {"id": int(oid)})
    db_update_order(oid, "CANCELLED")
    
    # Hapus pesan langsung
    try: await query.message.delete()
    except: pass

async def finish_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; 
    
    # --- PERUBAHAN DISINI: Pop-up instan & Hapus pesan seketika ---
    await query.answer("✅ Selesai. Nomor telah dilepas.", show_alert=True)
    
    oid = query.data.split("_")[1]
    await api_post("/orders/finish", {"id": int(oid)})
    db_update_order(oid, "COMPLETED")
    
    # Hapus pesan langsung
    try: await query.message.delete()
    except: pass

# ==========================================
# HANDLERS: BULK ORDER
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
    kb.append([InlineKeyboardButton("🔙 Kembali", callback_data="menu_utama")])
    await query.edit_message_text("📦 <b>BULK ORDER</b>\n\nPilih harga:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

async def bulk_qty_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, pid, price = query.data.split("_")
    kb = [
        [InlineKeyboardButton("5", callback_data=f"bulkqty_{pid}_{price}_5"), InlineKeyboardButton("10", callback_data=f"bulkqty_{pid}_{price}_10"), InlineKeyboardButton("20", callback_data=f"bulkqty_{pid}_{price}_20")],
        [InlineKeyboardButton("50", callback_data=f"bulkqty_{pid}_{price}_50"), InlineKeyboardButton("100", callback_data=f"bulkqty_{pid}_{price}_100")],
        [InlineKeyboardButton("🔙 Kembali", callback_data="menu_bulk")]
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
        
        teks = f"📦 <b>BULK SUMMARY</b>\n\n📊 Progress: <code>[{bar}]</code> <b>{len(received_set)}/{total}</b>\n{get_timer_emoji(sisa)} Sisa: <b>{format_time(sisa)}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
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
        await asyncio.sleep(5)
        
    if pending:
        for oid, _ in pending.items():
            await api_post("/orders/cancel", {"id": int(oid)})
            db_update_order(oid, "CANCELLED")
        if summary_msg_id:
            try: await context.bot.edit_message_text(chat_id, summary_msg_id, f"⌛ <b>WAKTU HABIS</b>\nSisa {len(pending)} nomor di-refund.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")]]), parse_mode='HTML')
            except: pass

# ==========================================
# HANDLERS: RIWAYAT & ORDER AKTIF
# ==========================================
async def menu_riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    conn = get_db()
    rows = conn.execute("SELECT phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (query.from_user.id,)).fetchall()
    conn.close()
    
    teks = "📜 <b>10 TRANSAKSI TERAKHIR</b>\n\n"
    for phone, status, otp, tgl in rows:
        emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else ("❌" if status == "CANCELLED" else "⏳"))
        teks += f"{emoji} <code>{phone}</code> | {status}\n   OTP: {otp or '-'} | {tgl}\n\n"
    if not rows: teks = "📜 Belum ada riwayat."
        
    await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb_akun()), parse_mode='HTML')

async def menu_aktif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer("Mengecek...")
    res = await api_get("/orders/active")
    if not res.get("success"): return await query.edit_message_text("❌ Gagal cek order aktif.")
    
    orders = res.get("data", [])
    if not orders: return await query.edit_message_text("📭 Tidak ada order aktif.", reply_markup=InlineKeyboardMarkup(kb_akun()))
    
    teks = "📦 <b>ORDER AKTIF</b>\n\n"
    kb = []
    for o in orders[:10]:
        phone, oid, otp = format_phone(o["phone_number"]), str(o["id"]), o.get("otp_code")
        teks += f"📱 <code>{phone}</code> | {'🔑 '+otp if otp else '⏳'}\n"
        if otp: kb.append([InlineKeyboardButton(f"Selesai {phone}", callback_data=f"finish_{oid}")])
        else: kb.append([InlineKeyboardButton(f"Batal {phone}", callback_data=f"cancel_{oid}")])
        
    kb.append([InlineKeyboardButton("🔙 Kembali", callback_data="menu_akun")])
    await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

# ==========================================
# ADMIN COMMANDS
# ==========================================
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    conn = get_db()
    rows = conn.execute("SELECT timestamp, message, user_id FROM logs ORDER BY id DESC LIMIT 15").fetchall()
    conn.close()
    teks = "📜 <b>ERROR LOGS</b>\n\n" + "\n".join([f"<code>{r[0]}</code>\n{r[1]}\nUser: {r[2]}\n" for r in rows]) if rows else "Belum ada error."
    await update.message.reply_text(teks[:4000], parse_mode='HTML')

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    conn = get_db()
    t = conn.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders").fetchone()
    conn.close()
    await update.message.reply_text(f"📊 <b>STATISTIK GLOBAL</b>\n\nTotal Order: {t[0]}\nTotal Nilai: Rp {format_rupiah(t[1])}", parse_mode='HTML')

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    with open(DB_PATH, 'rb') as f:
        await update.message.reply_document(document=f, filename=f"backup_{int(time.time())}.db", caption="🗄️ Backup DB")

# ==========================================
# HANDLER UTAMA & RUNNER
# ==========================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; data = query.data
    uid = query.from_user.id
    
    if not is_admin(uid) and not check_rate_limit(uid):
        return await query.answer("⏱️ Terlalu cepat, tunggu sebentar.", show_alert=True)
    
    await query.answer()
    
    try:
        if data == "menu_utama": await start(update, context)
        elif data == "menu_akun": 
            await query.edit_message_text("👤 <b>AKUN & RIWAYAT</b>\n\nSilakan pilih menu:", reply_markup=InlineKeyboardMarkup(kb_akun()), parse_mode='HTML')
        elif data == "menu_setting":
            await query.edit_message_text("⚙️ <b>PENGATURAN</b>", reply_markup=InlineKeyboardMarkup(kb_setting()), parse_mode='HTML')
            
        elif data == "menu_saldo": await menu_saldo(update, context)
        elif data == "menu_riwayat": await menu_riwayat(update, context)
        elif data == "menu_aktif": await menu_aktif(update, context)
        
        elif data == "menu_beli": await menu_beli(update, context)
        elif data == "menu_bulk": await menu_bulk(update, context)
        
        elif data.startswith("buy_"): await buy_number(update, context)
        elif data.startswith("bulkprice_"): await bulk_qty_menu(update, context)
        elif data.startswith("bulkqty_"): await process_bulk_order(update, context)
        
        elif data.startswith("replace_"): await replace_number(update, context)
        elif data.startswith("cancel_"): await cancel_order(update, context)
        elif data.startswith("finish_"): await finish_order(update, context)
        
        elif data == "menu_watchlist":
            await query.edit_message_text("🔔 Fitur watchlist restock sedang disiapkan.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="menu_setting")]]))
            
    except Exception as e:
        db_log_error(str(e), uid)
        await query.edit_message_text("⚠️ Terjadi kesalahan sistem.")

if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    port = int(os.environ.get("PORT", 8080))
    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("SPACE_HOST")
    
    if hostname:
        app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"https://{hostname}/{TOKEN}")
    else:
        print("🔌 Menjalankan Mode Polling (ZevCloud/VPS)...")
        app.run_polling()
