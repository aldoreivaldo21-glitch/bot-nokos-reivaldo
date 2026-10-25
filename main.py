import os
import json
import time
import sqlite3
import asyncio
import urllib.request
import urllib.error
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ==========================================
# KONFIGURASI UTAMA
# ==========================================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597")
API_KEY = os.environ.get("SMScode_API_KEY")
BASE_URL = "https://api.smscode.gg/v1"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

DB_PATH = "bot_data.db"
TIMEOUT_MINUTES = 20
TIMEOUT_SECONDS = TIMEOUT_MINUTES * 60

# Setup Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# DATABASE & HELPER
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY, user_id INTEGER, phone TEXT, product_id INTEGER,
        price INTEGER, status TEXT, otp TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def format_rupiah(n):
    return f"{int(n):,}".replace(",", ".")

def format_time(seconds):
    m, s = divmod(max(0, seconds), 60)
    return f"{m:02d}:{s:02d}"

# ==========================================
# API WRAPPER (Sinkronus, dijalankan di thread terpisah)
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
        except: 
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}

async def api_get(endpoint):
    return await asyncio.to_thread(req_api, endpoint, "GET")

async def api_post(endpoint, payload):
    return await asyncio.to_thread(req_api, endpoint, "POST", payload)

# ==========================================
# UI & MENU
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.first_name
    teks = (
        f"👋 Halo, <b>{user}</b>!\n\n"
        f"Selamat datang di <b>Bot Nokos Premium</b>.\n"
        f"Bot ini dilengkapi fitur <i>Auto-Refund</i> dan <i>Ganti Nomor</i> jika OTP tidak masuk.\n\n"
        f"Silakan pilih menu di bawah ini:"
    )
    kb = [
        [InlineKeyboardButton("🛒 Beli Nomor Gojek", callback_data="menu_beli")],
        [InlineKeyboardButton("💰 Cek Saldo", callback_data="menu_saldo")]
    ]
    if update.message:
        await update.message.reply_text(teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    else:
        await update.callback_query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

async def menu_beli(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Memuat produk...")
    await query.edit_message_text("⏳ Mengambil data produk dari server...")
    
    res = await api_get("/catalog/products?country_id=7")
    
    # --- PERBAIKAN ERROR HANDLING DI SINI ---
    if not res.get("success"):
        error_msg = res.get("error", res.get("message", "Tidak ada respon dari server"))
        return await query.edit_message_text(f"❌ Gagal ambil data produk.\n\nAlasan: <code>{error_msg}</code>", parse_mode='HTML')
    
    products = [p for p in res.get("data", []) if "Gojek" in p.get("name", "")]
    if not products:
        return await query.edit_message_text("❌ Produk Gojek sedang kosong atau stok habis.")
    
    kb = []
    seen = set()
    for p in products:
        if p['price'] not in seen and p.get('available', 0) > 0:
            seen.add(p['price'])
            kb.append([InlineKeyboardButton(f"📱 Rp {format_rupiah(p['price'])} (Stok: {p['available']})", callback_data=f"buy_{p['id']}_{p['price']}")])
    
    kb.append([InlineKeyboardButton("🔙 Kembali", callback_data="menu_utama")])
    
    if len(kb) == 1: # Hanya ada tombol kembali, artinya tidak ada stok
        return await query.edit_message_text("❌ Maaf, saat ini stok nomor Gojek sedang kosong.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        
    await query.edit_message_text("🛒 <b>Pilih Nomor Gojek:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

# ==========================================
# PROSES BELI & GANTI NOMOR
# ==========================================
async def buy_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, pid, price = query.data.split("_")
    price = int(price)
    user_id = query.from_user.id
    
    await query.edit_message_text("⏳ Sedang memproses pembelian...")
    
    # Beli via API
    res = await api_post("/orders/create", {"product_id": int(pid)})
    if not res.get("success"):
        error_msg = res.get("error", res.get("message", "Cek saldo Anda atau hubungi admin."))
        return await query.edit_message_text(f"❌ Gagal membeli nomor.\n\nAlasan: <code>{error_msg}</code>", parse_mode='HTML')
    
    order = res["data"]["orders"][0]
    oid = str(order["id"])
    phone = order["phone_number"]
    
    # Simpan ke DB
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO orders (order_id, user_id, phone, product_id, price, status) VALUES (?, ?, ?, ?, ?, 'ACTIVE')", 
                 (oid, user_id, phone, int(pid), price))
    conn.commit()
    conn.close()
    
    # Tampilkan pesan Live Polling
    await show_live_order(query.message.chat_id, oid, phone, int(pid), price, context)

async def show_live_order(chat_id, oid, phone, pid, price, context):
    sisa_waktu = TIMEOUT_SECONDS
    kb = [
        [InlineKeyboardButton("🔄 Ganti Nomor (OTP Macet)", callback_data=f"replace_{oid}_{pid}_{price}")],
        [InlineKeyboardButton("❌ Batalkan (Refund)", callback_data=f"cancel_{oid}")]
    ]
    
    pesan = (
        f"📱 <b>Nomor Berhasil Dibeli!</b>\n\n"
        f"Nomor: <code>{phone}</code>\n"
        f"Status: ⏳ <i>Menunggu SMS...</i>\n"
        f"Waktu: <b>{format_time(sisa_waktu)}</b>\n\n"
        f"<i>Bot akan otomatis mengirim OTP ke chat ini saat SMS masuk.</i>"
    )
    
    msg = await context.bot.send_message(chat_id=chat_id, text=pesan, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    message_id = msg.message_id
    
    # Looping cek OTP (Cepat & Efisien)
    for _ in range(TIMEOUT_SECONDS // 3):
        await asyncio.sleep(3) # Cek setiap 3 detik
        sisa_waktu -= 3
        
        # Cek ke API
        res = await api_get(f"/orders/{oid}")
        if not res.get("success"): continue
        
        data = res["data"]
        otp = data.get("otp_code")
        status = data.get("status")
        
        if otp:
            # OTP MASUK!
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE orders SET status='OTP_RECEIVED', otp=? WHERE order_id=?", (otp, oid))
            conn.commit()
            conn.close()
            
            kb_sukses = [[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{oid}")]]
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=f"🔑 <b>KODE OTP DITERIMA!</b>\n\nNomor: <code>{phone}</code>\nKode: <b>{otp}</b>",
                    reply_markup=InlineKeyboardMarkup(kb_sukses), parse_mode='HTML'
                )
            except: pass
            return
            
        elif status in ["CANCELLED", "EXPIRED"]:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except: pass
            return
            
        elif sisa_waktu % 15 == 0 and sisa_waktu > 0:
            # Update timer setiap 15 detik agar tidak kena limit Telegram
            pesan_timer = (
                f"📱 <b>Nomor Berhasil Dibeli!</b>\n\n"
                f"Nomor: <code>{phone}</code>\n"
                f"Status: ⏳ <i>Menunggu SMS...</i>\n"
                f"Waktu: <b>{format_time(sisa_waktu)}</b>\n\n"
                f"<i>Jika macet, Anda bisa klik tombol Ganti Nomor di bawah.</i>"
            )
            try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=pesan_timer, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
            except: pass

    # Waktu Habis (Auto Cancel)
    await api_post("/orders/cancel", {"id": int(oid)})
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE orders SET status='CANCELLED' WHERE order_id=?", (oid,))
    conn.commit()
    conn.close()
    
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=f"⌛ <b>Waktu Habis</b>\n\nNomor <code>{phone}</code> tidak menerima OTP.\nSaldo telah dikembalikan otomatis.",
            parse_mode='HTML'
        )
    except: pass

# ==========================================
# FITUR REPLACE NOMOR (GANTI NOMOR)
# ==========================================
async def replace_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Memproses ganti nomor...")
    _, old_oid, pid, price = query.data.split("_")
    
    # 1. Batalkan nomor lama
    await query.edit_message_text("⏳ Membatalkan nomor lama dan meminta refund...")
    cancel_res = await api_post("/orders/cancel", {"id": int(old_oid)})
    
    if not cancel_res.get("success"):
        error_msg = cancel_res.get("error", cancel_res.get("message", "API Error"))
        return await query.edit_message_text(
            f"❌ <b>Gagal Ganti Nomor.</b>\n\nAlasan: <code>{error_msg}</code>\n\n<i>Terkadang sistem SMScode meminta jeda 2 menit sebelum nomor bisa dibatalkan.</i>",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="menu_utama")]])
        )
        
    # 2. Update DB lama jadi CANCELLED
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE orders SET status='CANCELLED' WHERE order_id=?", (old_oid,))
    conn.commit()
    conn.close()
    
    # 3. Beli nomor baru
    await query.edit_message_text("♻️ Nomor lama dibatalkan. Sedang membeli nomor baru...")
    buy_res = await api_post("/orders/create", {"product_id": int(pid)})
    
    if not buy_res.get("success"):
        error_msg = buy_res.get("error", buy_res.get("message", "Gagal membeli"))
        return await query.edit_message_text(f"❌ Gagal membeli nomor baru.\n\nAlasan: <code>{error_msg}</code>", parse_mode='HTML')
        
    order = buy_res["data"]["orders"][0]
    new_oid = str(order["id"])
    new_phone = order["phone_number"]
    
    # 4. Simpan DB baru
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO orders (order_id, user_id, phone, product_id, price, status) VALUES (?, ?, ?, ?, ?, 'ACTIVE')", 
                 (new_oid, query.from_user.id, new_phone, int(pid), int(price)))
    conn.commit()
    conn.close()
    
    # 5. Hapus pesan lama, buat pesan Live Polling baru
    await query.message.delete()
    await show_live_order(query.message.chat_id, new_oid, new_phone, int(pid), int(price), context)

# ==========================================
# HANDLER LAINNYA
# ==========================================
async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    oid = query.data.split("_")[1]
    
    res = await api_post("/orders/cancel", {"id": int(oid)})
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE orders SET status='CANCELLED' WHERE order_id=?", (oid,))
    conn.commit()
    conn.close()
    
    await query.edit_message_text("✅ Nomor berhasil dibatalkan. Saldo dikembalikan.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")]]))

async def finish_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    oid = query.data.split("_")[1]
    
    await api_post("/orders/finish", {"id": int(oid)})
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE orders SET status='COMPLETED' WHERE order_id=?", (oid,))
    conn.commit()
    conn.close()
    
    await query.message.delete()

async def menu_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    res = await api_get("/balance")
    
    if res.get("success"):
        saldo = res.get("data", {}).get("balance", 0)
    else:
        saldo = 0
        error_msg = res.get("error", "Gagal mengambil saldo")
        return await query.edit_message_text(f"❌ Gagal cek saldo.\nAlasan: <code>{error_msg}</code>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="menu_utama")]]), parse_mode='HTML')
    
    kb = [[InlineKeyboardButton("🔙 Kembali", callback_data="menu_utama")]]
    await query.edit_message_text(f"💰 <b>Saldo Anda:</b>\n\nRp {format_rupiah(saldo)}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if data == "menu_utama": await start(update, context)
    elif data == "menu_beli": await menu_beli(update, context)
    elif data == "menu_saldo": await menu_saldo(update, context)
    elif data.startswith("buy_"): await buy_number(update, context)
    elif data.startswith("replace_"): await replace_number(update, context)
    elif data.startswith("cancel_"): await cancel_order(update, context)
    elif data.startswith("finish_"): await finish_order(update, context)
    else: await query.answer("Fitur belum tersedia.")

# ==========================================
# MAIN RUNNER (Polling / Webhook Auto-detect)
# ==========================================
if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Deteksi otomatis environment host
    port = int(os.environ.get("PORT", 8080))
    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("SPACE_HOST")
    
    if hostname:
        print("🚀 Menjalankan Mode Webhook...")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TOKEN,
            webhook_url=f"https://{hostname}/{TOKEN}"
        )
    else:
        print("🔌 Menjalankan Mode Polling (Lokal/VPS)...")
        app.run_polling()
