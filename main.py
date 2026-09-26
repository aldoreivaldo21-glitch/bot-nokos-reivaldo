import logging
import os
import json
import time
import sqlite3
import urllib.request
import urllib.error
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# --- KONFIGURASI ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597")
SMScode_API_KEY = os.environ.get("SMScode_API_KEY")

BASE_URL = "https://api.smscode.gg/v1"
HEADERS = {
    "Authorization": f"Bearer {SMScode_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")
pending_messages = {}  # {order_id: message_id}

# --- DATABASE (untuk Riwayat & Log) ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        user_id INTEGER,
        phone TEXT,
        product_id INTEGER,
        price INTEGER,
        status TEXT,
        otp TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def db_save_order(order_id, user_id, phone, product_id, price):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO orders (order_id, user_id, phone, product_id, price, status) VALUES (?, ?, ?, ?, ?, 'ACTIVE')",
                  (str(order_id), user_id, phone, product_id, price))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB Error: {e}")

def db_update_order(order_id, status, otp=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if otp:
            c.execute("UPDATE orders SET status=?, otp=? WHERE order_id=?", (status, otp, str(order_id)))
        else:
            c.execute("UPDATE orders SET status=? WHERE order_id=?", (status, str(order_id)))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB Error: {e}")

def db_get_history(user_id, limit=10):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT order_id, phone, status, otp, created_at FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"DB Error: {e}")
        return []

# --- FUNGSI BANTUAN ---
def http_get(url):
    req = urllib.request.Request(url, headers=HEADERS, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        except:
            return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}

def http_post(url, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=HEADERS, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        except:
            return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}

def translate_error(err_msg):
    """Terjemahkan error dari SMScode ke Bahasa Indonesia."""
    err_lower = err_msg.lower()
    if "no numbers available" in err_lower: return "❌ Stok nomor habis. Coba harga lain."
    if "insufficient balance" in err_lower: return "❌ Saldo Anda tidak cukup."
    if "unauthorized" in err_lower: return "❌ API Key salah atau tidak valid."
    if "not found" in err_lower: return "❌ Order tidak ditemukan."
    if "idempotency" in err_lower: return "⚠️ Order duplikat terdeteksi."
    if "rate limit" in err_lower or "too many" in err_lower: return "⚠️ Terlalu banyak permintaan. Tunggu sebentar."
    if "cannot be cancelled" in err_lower: return "❌ Order tidak bisa dibatalkan (mungkin sudah selesai)."
    return f"⚠️ {err_msg[:180]}"

def is_admin(user_id):
    return str(user_id) == str(ADMIN_ID)

def format_phone(phone):
    if phone and phone.startswith("62"):
        return phone[2:]
    return phone

# --- FUNGSI API SMScode ---
def get_gojek_products():
    data = http_get(f"{BASE_URL}/catalog/products?country_id=7")
    if "error" in data: return None, translate_error(data["error"])
    if not data.get("success"): return None, "Gagal mengambil data dari API."
    products = data.get("data", [])
    if not isinstance(products, list): return None, "Format data dari API tidak valid."
    gojek = [p for p in products if "Gojek" in p.get("name", "")]
    gojek.sort(key=lambda x: x.get('price', 999999))
    return gojek, None

def get_active_orders():
    data = http_get(f"{BASE_URL}/orders/active")
    if "error" in data: return None, translate_error(data["error"])
    if not data.get("success"): return None, "Gagal mengambil daftar order aktif."
    orders = data.get("data", [])
    if not isinstance(orders, list): return None, "Format data order tidak valid."
    return orders, None

def cek_saldo_api():
    if not SMScode_API_KEY: return "❌ API Key belum diatur."
    data = http_get(f"{BASE_URL}/balance")
    if "error" in data: return translate_error(data["error"])
    if data.get("success"):
        saldo = data["data"].get("balance", 0)
        currency = data["data"].get("currency", "IDR")
        return f"💰 Saldo Anda: {saldo} {currency}"
    return "⚠️ Gagal mengambil saldo."

def beli_nomor_api(product_id, user_id, price):
    if not SMScode_API_KEY: return None, "❌ API Key belum diatur."
    data = http_post(f"{BASE_URL}/orders/create", {"product_id": product_id})
    if "error" in data: return None, translate_error(data["error"])
    if data.get("success"):
        order_data = data["data"]["orders"][0]
        oid = str(order_data["id"])
        nomor = order_data["phone_number"]
        db_save_order(oid, user_id, nomor, product_id, price)
        return oid, nomor
    return None, "⚠️ Gagal membeli nomor."

def batal_order_api(order_id):
    data = http_post(f"{BASE_URL}/orders/cancel", {"id": int(order_id)})
    if "error" in data: return translate_error(data["error"])
    if data.get("success"):
        db_update_order(order_id, "CANCELLED")
        return f"✅ Order <code>{order_id}</code> berhasil dibatalkan.\n💰 Saldo telah dikembalikan."
    return "⚠️ Gagal membatalkan order."

def selesai_order_api(order_id):
    data = http_post(f"{BASE_URL}/orders/finish", {"id": int(order_id)})
    if "error" in data: return translate_error(data["error"])
    if data.get("success"):
        db_update_order(order_id, "COMPLETED")
        return f"✅ Order <code>{order_id}</code> berhasil diselesaikan."
    return "⚠️ Gagal menyelesaikan order."

# --- AUTO POLLING DENGAN AUTO-CANCEL (OPSI B) ---
async def auto_poll_otp(chat_id: int, context: ContextTypes.DEFAULT_TYPE, order_id: str, nomor_display: str):
    total_seconds = 25 * 60

    for i in range(total_seconds // 5):
        await asyncio.sleep(5)
        elapsed = (i + 1) * 5
        remaining = total_seconds - elapsed

        data = http_get(f"{BASE_URL}/orders/{order_id}")
        if not data.get("success"):
            continue

        order_data = data["data"]
        otp = order_data.get("otp_code")
        status = order_data.get("status")
        msg_id = pending_messages.get(order_id)

        if otp:
            # OTP DITEMUKAN
            db_update_order(order_id, "OTP_RECEIVED", otp)
            if msg_id:
                try:
                    keyboard = [
                        [InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")],
                        [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
                    ]
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id,
                        text=f"🔑 <b>Kode OTP Ditemukan!</b>\n\nKode: <code>{otp}</code>\n\nMasukkan kode ke aplikasi Gojek, lalu klik ✅ Selesai.",
                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
                    )
                    pending_messages.pop(order_id, None)
                except Exception: pass
            else:
                try:
                    keyboard = [
                        [InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")],
                        [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
                    ]
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🔑 <b>Kode OTP Ditemukan!</b>\n\nNomor: <code>{nomor_display}</code>\nKode: <code>{otp}</code>",
                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
                    )
                except Exception: pass
            return

        elif status in ["CANCELLED", "EXPIRED", "COMPLETED"]:
            db_update_order(order_id, status)
            if msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id,
                        text=f"ℹ️ Order <code>{order_id}</code> telah {status}.",
                        parse_mode='HTML'
                    )
                    pending_messages.pop(order_id, None)
                except Exception: pass
            return

        elif status == "ACTIVE" and remaining > 0:
            if msg_id:
                mins = remaining // 60
                secs = remaining % 60
                try:
                    keyboard = [
                        [InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
                        [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
                    ]
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id,
                        text=f"⏳ <b>Status:</b> Menunggu SMS masuk\n⏱️ Sisa waktu: <b>{mins:02d}:{secs:02d}</b>\n\nBot akan otomatis mengubah pesan ini menjadi kode OTP saat SMS masuk.",
                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
                    )
                except Exception: pass

    # === WAKTU HABIS: AUTO-CANCEL (OPSI B) ===
    hasil_cancel = batal_order_api(order_id)
    pesan = (f"⏰ <b>Waktu Habis (25 Menit)</b>\n\n"
             f"Nomor <code>{nomor_display}</code> telah <b>dibatalkan otomatis</b> karena tidak ada SMS yang masuk.\n\n"
             f"{hasil_cancel}")
    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=pesan,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]),
                parse_mode='HTML'
            )
            pending_messages.pop(order_id, None)
        except Exception: pass
    else:
        try:
            await context.bot.send_message(chat_id=chat_id, text=pesan, parse_mode='HTML')
        except Exception: pass

# --- TAMPILAN MENU ---
async def send_products_menu(chat_id, context, query=None):
    products, err = get_gojek_products()
    if err:
        if query: await query.edit_message_text(f"❌ {err}")
        else: await context.bot.send_message(chat_id, f"❌ {err}")
        return
    if not products:
        text = "❌ Tidak ada produk Gojek tersedia saat ini."
        if query: await query.edit_message_text(text)
        else: await context.bot.send_message(chat_id, text)
        return

    keyboard = []
    seen = set()
    for p in products:
        harga = p.get('price', '?')
        pid = p.get('id')
        stok = p.get('available', 0)
        if harga in seen: continue
        seen.add(harga)
        if stok and stok > 0:
            label = f"💵 Rp {harga} | 📦 {stok}"
            callback = f"confirm_{pid}_{harga}"
        else:
            label = f"❌ Rp {harga} (Habis)"
            callback = "stok_habis"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])
        if len(keyboard) >= 10: break
    keyboard.append([InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')])

    text = ("🛒 <b>Pilih Harga Nomor Gojek</b>\n\n"
            "Angka 📦 menunjukkan stok tersedia.\n"
            "Bot otomatis membatalkan jika OTP tidak masuk dalam 25 menit.")
    reply_markup = InlineKeyboardMarkup(keyboard)
    if query: await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')
    else: await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode='HTML')

async def send_active_orders_menu(chat_id, context, action, query=None):
    orders, err = get_active_orders()
    if err:
        if query: await query.edit_message_text(f"❌ {err}")
        else: await context.bot.send_message(chat_id, f"❌ {err}")
        return
    if not orders:
        text = "📭 Tidak ada nomor aktif saat ini."
        if query: await query.edit_message_text(text)
        else: await context.bot.send_message(chat_id, text)
        return

    if action == 'cancel':
        judul = "❌ <b>Pilih Nomor yang Ingin Dibatalkan</b>"
        prefix = "cancel_"
    else:
        judul = "✅ <b>Pilih Nomor yang Ingin Diselesaikan</b>"
        prefix = "finish_"

    keyboard = []
    for o in orders:
        oid = o.get('id')
        nomor = format_phone(o.get('phone_number', '?'))
        status = o.get('status', '?')
        otp = o.get('otp_code')
        label = f"📱 {nomor}"
        if otp: label += f" | 🔑 {otp}"
        elif status == 'ACTIVE': label += " | ⏳"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"{prefix}{oid}")])
    keyboard.append([InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')])

    text = f"{judul}\n\nTotal: {len(orders)} nomor"
    reply_markup = InlineKeyboardMarkup(keyboard)
    if query: await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')
    else: await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode='HTML')

# --- HANDLER TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')],
        [InlineKeyboardButton("💰 Cek Saldo", callback_data='cek_saldo'),
         InlineKeyboardButton("📦 Nomor Aktif", callback_data='aktif')],
        [InlineKeyboardButton("📜 Riwayat Order", callback_data='riwayat')]
    ]
    await update.message.reply_text(
        "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>\n\n"
        "👋 Silakan pilih menu di bawah.\n\n"
        "💡 <b>Tips:</b> Bot otomatis membatalkan nomor jika OTP tidak masuk dalam 25 menit.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ <b>Daftar Perintah:</b>\n\n"
        "🚀 /start - Menu utama\n"
        "🛒 /beli - Beli nomor Gojek\n"
        "💰 /saldo - Cek saldo\n"
        "📦 /aktif - Lihat nomor aktif\n"
        "📜 /riwayat - 10 transaksi terakhir\n"
        "❌ /batal - Batalkan nomor\n"
        "✅ /selesai - Selesaikan nomor",
        parse_mode='HTML'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.edit_message_text("🚫 Akses ditolak.")
        return

    data = query.data

    if data == 'menu_utama':
        keyboard = [
            [InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')],
            [InlineKeyboardButton("💰 Cek Saldo", callback_data='cek_saldo'),
             InlineKeyboardButton("📦 Nomor Aktif", callback_data='aktif')],
            [InlineKeyboardButton("📜 Riwayat Order", callback_data='riwayat')]
        ]
        await query.edit_message_text("🚀 <b>Menu Utama</b>\n\nSilakan pilih menu:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

    elif data == 'cek_saldo':
        keyboard = [[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
        await query.edit_message_text(cek_saldo_api(), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == 'beli_nomor':
        await send_products_menu(query.message.chat_id, context, query=query)

    elif data == 'aktif':
        await send_active_orders_menu(query.message.chat_id, context, 'cancel', query=query)

    elif data == 'riwayat':
        rows = db_get_history(user_id, 10)
        if not rows:
            teks = "📜 Belum ada riwayat transaksi."
        else:
            teks = "📜 <b>10 Transaksi Terakhir:</b>\n\n"
            for oid, phone, status, otp, created in rows:
                emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else "⏳")
                teks += f"{emoji} <code>{format_phone(phone)}</code>\n   Status: {status} | OTP: {otp or '-'}\n   {created}\n\n"
        keyboard = [[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
        await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

    elif data == 'stok_habis':
        await query.answer("❌ Stok habis untuk harga ini. Pilih harga lain.", show_alert=True)

    # === KONFIRMASI BELI (#4) ===
    elif data.startswith('confirm_'):
        parts = data.replace('confirm_', '').split('_')
        pid = int(parts[0])
        harga = parts[1]
        keyboard = [
            [InlineKeyboardButton(f"✅ Ya, Beli Rp {harga}", callback_data=f"buy_{pid}_{harga}")],
            [InlineKeyboardButton("❌ Batal", callback_data='beli_nomor')]
        ]
        await query.edit_message_text(
            f"🛒 <b>Konfirmasi Pembelian</b>\n\nHarga: <b>Rp {harga}</b>\n\nYakin ingin membeli nomor Gojek?",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
        )

    elif data.startswith('buy_'):
        parts = data.replace('buy_', '').split('_')
        pid = int(parts[0])
        harga = int(parts[1]) if len(parts) > 1 else 0
        await query.edit_message_text("⏳ Sedang membeli nomor...")
        order_id, nomor_raw = beli_nomor_api(pid, user_id, harga)
        if order_id:
            nomor_display = format_phone(nomor_raw)
            keyboard = [
                [InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
                [InlineKeyboardButton("🔄 Cek OTP Sekarang", callback_data=f"checkotp_{order_id}")],
                [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
            ]
            teks = (f"🛒 <b>Nomor berhasil dibeli!</b>\n\n"
                    f"📱 Nomor: <code>{nomor_display}</code>\n"
                    f"🆔 ID: <code>{order_id}</code>\n\n"
                    f"📋 <b>Langkah selanjutnya:</b>\n"
                    f"1️⃣ Ketuk nomor untuk salin\n"
                    f"2️⃣ Masukkan ke aplikasi Gojek\n"
                    f"3️⃣ Ketuk <b>Cek OTP Sekarang</b>\n\n"
                    f"⚠️ OTP tidak masuk dalam 25 menit? Nomor otomatis dibatalkan.")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display))
        else:
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]]
            await query.edit_message_text(f"❌ {nomor_raw}", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith('checkotp_'):
        order_id = data.replace('checkotp_', '')
        cek = http_get(f"{BASE_URL}/orders/{order_id}")
        otp_sekarang = None
        if cek.get("success"):
            otp_sekarang = cek["data"].get("otp_code")

        if otp_sekarang:
            keyboard = [
                [InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")],
                [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
            ]
            await query.edit_message_text(
                f"🔑 <b>Kode OTP Ditemukan!</b>\n\nKode: <code>{otp_sekarang}</code>\n\nMasukkan ke aplikasi Gojek, lalu klik ✅ Selesai.",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
            )
        else:
            keyboard = [
                [InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
                [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
            ]
            teks = (f"⏳ <b>Status:</b> Menunggu SMS masuk\n"
                    f"⏱️ Sisa waktu: <b>25:00</b>\n\n"
                    f"Bot akan otomatis mengubah pesan ini menjadi kode OTP saat SMS masuk.\n"
                    f"⚠️ Jika 25 menit habis, nomor otomatis dibatalkan.")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            pending_messages[order_id] = query.message.message_id

    elif data.startswith('cancel_'):
        order_id = data.replace('cancel_', '')
        await query.edit_message_text(f"⏳ Membatalkan Order <code>{order_id}</code>...", parse_mode='HTML')
        hasil = batal_order_api(order_id)
        pending_messages.pop(order_id, None)
        keyboard = [[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
        await query.edit_message_text(hasil, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

    elif data.startswith('finish_'):
        order_id = data.replace('finish_', '')
        await query.edit_message_text(f"⏳ Menyelesaikan Order <code>{order_id}</code>...", parse_mode='HTML')
        hasil = selesai_order_api(order_id)
        pending_messages.pop(order_id, None)
        keyboard = [[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
        await query.edit_message_text(hasil, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# --- COMMAND HANDLERS ---
async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(cek_saldo_api())

async def beli_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_products_menu(update.effective_chat.id, context)

async def batal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_active_orders_menu(update.effective_chat.id, context, 'cancel')

async def selesai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_active_orders_menu(update.effective_chat.id, context, 'finish')

async def aktif_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_active_orders_menu(update.effective_chat.id, context, 'cancel')

async def riwayat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_get_history(update.effective_user.id, 10)
    if not rows:
        await update.message.reply_text("📜 Belum ada riwayat transaksi.")
        return
    teks = "📜 <b>10 Transaksi Terakhir:</b>\n\n"
    for oid, phone, status, otp, created in rows:
        emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else "⏳")
        teks += f"{emoji} <code>{format_phone(phone)}</code>\n   Status: {status} | OTP: {otp or '-'}\n   {created}\n\n"
    await update.message.reply_text(teks, parse_mode='HTML')

# --- MAIN ---
if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("saldo", saldo_command))
    application.add_handler(CommandHandler("beli", beli_command))
    application.add_handler(CommandHandler("batal", batal_command))
    application.add_handler(CommandHandler("selesai", selesai_command))
    application.add_handler(CommandHandler("aktif", aktif_command))
    application.add_handler(CommandHandler("riwayat", riwayat_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    print("Bot berjalan...")
    application.run_polling()
