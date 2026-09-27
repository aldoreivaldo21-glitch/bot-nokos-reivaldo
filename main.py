import logging
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
SMScode_API_KEY = os.environ.get("SMScode_API_KEY")
LOW_BALANCE_THRESHOLD = 2000  # Rp

BASE_URL = "https://api.smscode.gg/v1"
HEADERS = {
    "Authorization": f"Bearer {SMScode_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")
pending_messages = {}

# --- DATABASE ---
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
    c.execute('''CREATE TABLE IF NOT EXISTS templates (
        key TEXT PRIMARY KEY,
        value TEXT
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

def db_get_stats():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE DATE(created_at) = DATE('now', 'localtime')")
        today = c.fetchone()
        c.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now', 'localtime')")
        month = c.fetchone()
        c.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders")
        total = c.fetchone()
        c.execute("SELECT status, COUNT(*) FROM orders GROUP BY status")
        statuses = dict(c.fetchall())
        conn.close()
        return {"today": today, "month": month, "total": total, "statuses": statuses}
    except Exception as e:
        print(f"DB Stats Error: {e}")
        return None

def get_template(key, default=""):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT value FROM templates WHERE key=?", (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else default
    except: return default

def set_template(key, value):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO templates (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
        return True
    except: return False

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
    err_lower = err_msg.lower()
    if "cannot cancel order" in err_lower and "otp_received" in err_lower:
        return "⚠️ Order sudah menerima OTP, tidak bisa dibatalkan."
    if "cannot cancel order" in err_lower and "completed" in err_lower:
        return "ℹ️ Order sudah selesai, tidak perlu dibatalkan."
    if "no numbers available" in err_lower:
        return "❌ Stok nomor habis. Coba harga lain."
    if "insufficient balance" in err_lower:
        return "❌ Saldo Anda tidak cukup."
    if "unauthorized" in err_lower:
        return "❌ API Key salah atau tidak valid."
    if "not found" in err_lower:
        return "❌ Order tidak ditemukan."
    if "idempotency" in err_lower:
        return "⚠️ Order duplikat terdeteksi."
    if "rate limit" in err_lower or "too many" in err_lower:
        return "⚠️ Terlalu banyak permintaan. Tunggu sebentar."
    if "conflict" in err_lower:
        return "⚠️ Konflik status order."
    return f"⚠️ {err_msg[:180]}"

def is_admin(user_id):
    return str(user_id) == str(ADMIN_ID)

def format_phone(phone):
    if phone and phone.startswith("62"):
        return phone[2:]
    return phone

async def log_error(context, error_msg, user_id=None):
    """Kirim log error ke admin (fitur #13)."""
    try:
        teks = f"⚠️ <b>Error Log</b>\n\n"
        if user_id: teks += f"User: <code>{user_id}</code>\n"
        teks += f"Pesan: <code>{error_msg[:400]}</code>\n"
        teks += f"Waktu: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        await context.bot.send_message(chat_id=ADMIN_ID, text=teks, parse_mode='HTML')
    except Exception:
        pass

async def check_balance_warning(context, chat_id, balance):
    """Kirim peringatan saldo rendah (fitur #3)."""
    if balance < LOW_BALANCE_THRESHOLD:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ <b>Peringatan Saldo Rendah!</b>\n\n"
                     f"Saldo Anda: <b>Rp {balance}</b>\n"
                     f"Sisa hanya cukup untuk {balance // 79} nomor lagi.\n\n"
                     f"Segera top-up untuk melanjutkan.",
                parse_mode='HTML'
            )
        except Exception:
            pass

def get_balance():
    data = http_get(f"{BASE_URL}/balance")
    if data.get("success"):
        return data["data"].get("balance", 0)
    return -1

# --- API SMScode ---
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
    if not SMScode_API_KEY: return "❌ API Key belum diatur.", -1
    data = http_get(f"{BASE_URL}/balance")
    if "error" in data: return translate_error(data["error"]), -1
    if data.get("success"):
        saldo = data["data"].get("balance", 0)
        currency = data["data"].get("currency", "IDR")
        return f"💰 Saldo Anda: {saldo} {currency}", saldo
    return "⚠️ Gagal mengambil saldo.", -1

def beli_nomor_api(product_id, user_id, price):
    if not SMScode_API_KEY: return None, "❌ API Key belum diatur."
    # Fitur #14: Cek saldo sebelum beli
    saldo = get_balance()
    if saldo >= 0 and saldo < price:
        return None, f"❌ Saldo tidak cukup.\nSaldo: Rp {saldo}\nDibutuhkan: Rp {price}"
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
    if "error" in data:
        return {"success": False, "message": translate_error(data["error"])}
    if data.get("success"):
        db_update_order(order_id, "CANCELLED")
        return {"success": True, "message": f"✅ Order <code>{order_id}</code> berhasil dibatalkan.\n💰 Saldo telah dikembalikan."}
    return {"success": False, "message": "⚠️ Gagal membatalkan order."}

def selesai_order_api(order_id):
    data = http_post(f"{BASE_URL}/orders/finish", {"id": int(order_id)})
    if "error" in data: return translate_error(data["error"])
    if data.get("success"):
        db_update_order(order_id, "COMPLETED")
        return f"✅ Order <code>{order_id}</code> berhasil diselesaikan."
    return "⚠️ Gagal menyelesaikan order."

# --- BACKUP OTOMATIS (Fitur #8) ---
async def auto_backup_task(context):
    """Kirim file DB ke admin setiap 6 jam."""
    while True:
        await asyncio.sleep(6 * 3600)  # 6 jam
        try:
            if os.path.exists(DB_PATH):
                with open(DB_PATH, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=ADMIN_ID,
                        document=f,
                        filename=f"backup_nokos_{time.strftime('%Y%m%d_%H%M')}.db",
                        caption=f"🗄️ <b>Backup Database Otomatis</b>\nWaktu: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                        parse_mode='HTML'
                    )
        except Exception as e:
            print(f"Backup error: {e}")

# --- AUTO POLLING + AUTO-CANCEL ---
async def auto_poll_otp(chat_id: int, context: ContextTypes.DEFAULT_TYPE, order_id: str, nomor_display: str):
    total_seconds = 25 * 60
    for i in range(total_seconds // 5):
        await asyncio.sleep(5)
        elapsed = (i + 1) * 5
        remaining = total_seconds - elapsed
        data = http_get(f"{BASE_URL}/orders/{order_id}")
        if not data.get("success"): continue
        order_data = data["data"]
        otp = order_data.get("otp_code")
        status = order_data.get("status")
        msg_id = pending_messages.get(order_id)

        if otp:
            db_update_order(order_id, "OTP_RECEIVED", otp)
            # Gunakan template kustom (fitur #10)
            tmpl = get_template("otp_found", "🔑 <b>Kode OTP Ditemukan!</b>\n\nKode: <code>{otp}</code>\n\nMasukkan ke Gojek lalu klik ✅ Selesai.")
            teks = tmpl.replace("{otp}", otp).replace("{nomor}", nomor_display).replace("{order_id}", order_id)
            if msg_id:
                try:
                    keyboard = [
                        [InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")],
                        [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
                    ]
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=teks, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
                    pending_messages.pop(order_id, None)
                except Exception: pass
            else:
                try:
                    keyboard = [
                        [InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")],
                        [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
                    ]
                    await context.bot.send_message(chat_id=chat_id, text=teks, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
                except Exception: pass
            return
        elif status in ["CANCELLED", "EXPIRED", "COMPLETED"]:
            db_update_order(order_id, status)
            if msg_id:
                try:
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=f"ℹ️ Order telah {status}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]), parse_mode='HTML')
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
                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
                except Exception: pass

    # Waktu habis
    cek_akhir = http_get(f"{BASE_URL}/orders/{order_id}")
    otp_akhir = None
    if cek_akhir.get("success"):
        otp_akhir = cek_akhir["data"].get("otp_code")
    if otp_akhir:
        db_update_order(order_id, "OTP_RECEIVED", otp_akhir)
        tmpl = get_template("otp_found", "🔑 <b>Kode OTP Ditemukan!</b>\n\nKode: <code>{otp}</code>")
        pesan = tmpl.replace("{otp}", otp_akhir).replace("{nomor}", nomor_display).replace("{order_id}", order_id)
        if msg_id:
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=pesan, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{order_id}")]]), parse_mode='HTML')
                pending_messages.pop(order_id, None)
            except Exception: pass
        return

    hasil_cancel = batal_order_api(order_id)
    if hasil_cancel["success"]:
        pesan = f"⏰ <b>Waktu Habis</b>\n\nNomor <code>{nomor_display}</code> telah dibatalkan otomatis.\n\n{hasil_cancel['message']}"
    else:
        pesan = f"⚠️ <b>Waktu Habis & Gagal Batalkan</b>\n\nAlasan: {hasil_cancel['message']}"
    if msg_id:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=pesan, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]), parse_mode='HTML')
            pending_messages.pop(order_id, None)
        except Exception: pass

# --- TAMPILAN MENU ---
def menu_utama_keyboard():
    return [
        [InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')],
        [InlineKeyboardButton("💰 Cek Saldo", callback_data='cek_saldo'),
         InlineKeyboardButton("📦 Nomor Aktif", callback_data='aktif')],
        [InlineKeyboardButton("📜 Riwayat Order", callback_data='riwayat'),
         InlineKeyboardButton("📊 Statistik", callback_data='stats')]
    ]

async def send_products_menu(chat_id, context, query=None):
    products, err = get_gojek_products()
    if err:
        if query: await query.edit_message_text(f"❌ {err}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))
        else: await context.bot.send_message(chat_id, f"❌ {err}")
        return
    if not products:
        text = "❌ Tidak ada produk Gojek tersedia saat ini."
        if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))
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
            callback = f"buy_{pid}_{harga}"
        else:
            label = f"❌ Rp {harga} (Habis)"
            callback = "stok_habis"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])
        if len(keyboard) >= 10: break
    keyboard.append([InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')])

    text = ("🛒 <b>Pilih Harga Nomor Gojek</b>\n\n📦 = stok tersedia\n⏱️ Auto-cancel jika OTP tidak masuk 25 menit.\n⚡ Klik harga untuk langsung membeli.")
    reply_markup = InlineKeyboardMarkup(keyboard)
    if query: await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')
    else: await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode='HTML')

async def send_active_orders_menu(chat_id, context, action, query=None):
    orders, err = get_active_orders()
    if err:
        if query: await query.edit_message_text(f"❌ {err}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))
        else: await context.bot.send_message(chat_id, f"❌ {err}")
        return
    if not orders:
        text = "📭 Tidak ada nomor aktif saat ini."
        if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))
        else: await context.bot.send_message(chat_id, text)
        return
    if action == 'cancel':
        judul = "❌ <b>Pilih Nomor yang Ingin Dibatalkan</b>"; prefix = "cancel_"
    else:
        judul = "✅ <b>Pilih Nomor yang Ingin Diselesaikan</b>"; prefix = "finish_"
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
    if query: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else: await context.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# --- HANDLER ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = get_template("welcome", "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>")
    await update.message.reply_text(
        f"{welcome}\n\n👋 Silakan pilih menu di bawah.\n\n💡 Bot otomatis membatalkan nomor jika OTP tidak masuk dalam 25 menit.",
        reply_markup=InlineKeyboardMarkup(menu_utama_keyboard()), parse_mode='HTML'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ <b>Daftar Perintah:</b>\n\n"
        "🚀 /start - Menu utama\n"
        "🛒 /beli - Beli nomor Gojek\n"
        "💰 /saldo - Cek saldo\n"
        "📦 /aktif - Lihat nomor aktif\n"
        "📜 /riwayat - 10 transaksi terakhir\n"
        "📊 /stats - Statistik transaksi\n"
        "❌ /batal - Batalkan nomor\n"
        "✅ /selesai - Selesaikan nomor\n\n"
        "👑 <b>Admin Only:</b>\n"
        "/setwelcome [teks] - Ubah sambutan\n"
        "/setotp [teks] - Ubah template OTP\n"
        "/backup - Backup database sekarang",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]])
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("🚫 Akses ditolak.")
        return
    data = query.data
    try:
        if data == 'menu_utama':
            welcome = get_template("welcome", "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>")
            await query.edit_message_text(f"{welcome}\n\nSilakan pilih menu:", reply_markup=InlineKeyboardMarkup(menu_utama_keyboard()), parse_mode='HTML')

        elif data == 'cek_saldo':
            teks, saldo = cek_saldo_api()
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))
            if saldo >= 0: await check_balance_warning(context, query.message.chat_id, saldo)

        elif data == 'beli_nomor':
            await send_products_menu(query.message.chat_id, context, query=query)

        elif data == 'aktif':
            await send_active_orders_menu(query.message.chat_id, context, 'cancel', query=query)

        elif data == 'riwayat':
            rows = db_get_history(user_id, 10)
            if not rows: teks = "📜 Belum ada riwayat transaksi."
            else:
                teks = "📜 <b>10 Transaksi Terakhir:</b>\n\n"
                for oid, phone, status, otp, created in rows:
                    emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else "⏳")
                    teks += f"{emoji} <code>{format_phone(phone)}</code>\n   Status: {status} | OTP: {otp or '-'}\n   {created}\n\n"
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]), parse_mode='HTML')

        elif data == 'stats':
            s = db_get_stats()
            if not s:
                await query.edit_message_text("⚠️ Gagal mengambil statistik.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))
            else:
                teks = (f"📊 <b>Statistik Bot</b>\n\n"
                        f"📅 <b>Hari Ini:</b>\n   Order: {s['today'][0]} | Pengeluaran: Rp {s['today'][1]}\n\n"
                        f"📆 <b>Bulan Ini:</b>\n   Order: {s['month'][0]} | Pengeluaran: Rp {s['month'][1]}\n\n"
                        f"📈 <b>Total:</b>\n   Order: {s['total'][0]} | Pengeluaran: Rp {s['total'][1]}\n\n"
                        f"📋 <b>Status Order:</b>\n")
                for status, count in s['statuses'].items():
                    teks += f"   {status}: {count}\n"
                await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]), parse_mode='HTML')

        elif data == 'stok_habis':
            await query.answer("❌ Stok habis. Pilih harga lain.", show_alert=True)

        elif data.startswith('buy_'):
            parts = data.replace('buy_', '').split('_')
            pid = int(parts[0]); harga = int(parts[1]) if len(parts) > 1 else 0
            await query.edit_message_text("⏳ Sedang membeli nomor...")
            order_id, nomor_raw = beli_nomor_api(pid, user_id, harga)
            if order_id:
                nomor_display = format_phone(nomor_raw)
                keyboard = [
                    [InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
                    [InlineKeyboardButton("🔄 Cek OTP Sekarang", callback_data=f"checkotp_{order_id}")],
                    [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
                ]
                teks = (f"🛒 <b>Nomor berhasil dibeli!</b>\n\n📱 Nomor: <code>{nomor_display}</code>\n🆔 ID: <code>{order_id}</code>\n\n"
                        f"📋 <b>Langkah selanjutnya:</b>\n1️⃣ Ketuk nomor untuk salin\n2️⃣ Masukkan ke aplikasi Gojek\n3️⃣ Ketuk <b>Cek OTP Sekarang</b>\n\n"
                        f"⚠️ OTP tidak masuk dalam 25 menit? Nomor otomatis dibatalkan.")
                await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display))
            else:
                keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')], [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
                await query.edit_message_text(f"❌ {nomor_raw}", reply_markup=InlineKeyboardMarkup(keyboard))

        elif data.startswith('checkotp_'):
            order_id = data.replace('checkotp_', '')
            cek = http_get(f"{BASE_URL}/orders/{order_id}")
            otp_sekarang = None; status_sekarang = None
            if cek.get("success"):
                otp_sekarang = cek["data"].get("otp_code")
                status_sekarang = cek["data"].get("status")
            if otp_sekarang:
                tmpl = get_template("otp_found", "🔑 <b>Kode OTP Ditemukan!</b>\n\nKode: <code>{otp}</code>")
                teks = tmpl.replace("{otp}", otp_sekarang)
                keyboard = [[InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")], [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
                await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            elif status_sekarang in ["CANCELLED", "EXPIRED", "COMPLETED"]:
                await query.edit_message_text(f"ℹ️ Order telah {status_sekarang}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]), parse_mode='HTML')
            else:
                keyboard = [[InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")], [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
                teks = (f"⏳ <b>Status:</b> Menunggu SMS masuk\n⏱️ Sisa waktu: <b>25:00</b>\n\nBot akan otomatis mengubah pesan ini menjadi kode OTP saat SMS masuk.\n⚠️ Jika 25 menit habis, nomor otomatis dibatalkan.")
                await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
                pending_messages[order_id] = query.message.message_id

        elif data.startswith('cancel_'):
            order_id = data.replace('cancel_', '')
            await query.edit_message_text(f"⏳ Membatalkan Order <code>{order_id}</code>...", parse_mode='HTML')
            hasil = batal_order_api(order_id)
            pending_messages.pop(order_id, None)
            await query.edit_message_text(hasil["message"], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]), parse_mode='HTML')

        elif data.startswith('finish_'):
            order_id = data.replace('finish_', '')
            await query.edit_message_text(f"⏳ Menyelesaikan Order <code>{order_id}</code>...", parse_mode='HTML')
            hasil = selesai_order_api(order_id)
            pending_messages.pop(order_id, None)
            await query.edit_message_text(hasil, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]), parse_mode='HTML')

    except Exception as e:
        await log_error(context, f"Button handler error: {str(e)[:200]}", user_id)
        try:
            await query.edit_message_text("⚠️ Terjadi kesalahan. Silakan coba lagi.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))
        except: pass

# --- COMMAND HANDLERS ---
async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks, saldo = cek_saldo_api()
    await update.message.reply_text(teks, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))
    if saldo >= 0: await check_balance_warning(context, update.effective_chat.id, saldo)

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
        await update.message.reply_text("📜 Belum ada riwayat transaksi.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))
        return
    teks = "📜 <b>10 Transaksi Terakhir:</b>\n\n"
    for oid, phone, status, otp, created in rows:
        emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else "⏳")
        teks += f"{emoji} <code>{format_phone(phone)}</code>\n   Status: {status} | OTP: {otp or '-'}\n   {created}\n\n"
    await update.message.reply_text(teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = db_get_stats()
    if not s:
        await update.message.reply_text("⚠️ Gagal mengambil statistik.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))
        return
    teks = (f"📊 <b>Statistik Bot</b>\n\n"
            f"📅 <b>Hari Ini:</b>\n   Order: {s['today'][0]} | Pengeluaran: Rp {s['today'][1]}\n\n"
            f"📆 <b>Bulan Ini:</b>\n   Order: {s['month'][0]} | Pengeluaran: Rp {s['month'][1]}\n\n"
            f"📈 <b>Total:</b>\n   Order: {s['total'][0]} | Pengeluaran: Rp {s['total'][1]}\n\n"
            f"📋 <b>Status Order:</b>\n")
    for status, count in s['statuses'].items():
        teks += f"   {status}: {count}\n"
    await update.message.reply_text(teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]))

async def setwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Akses ditolak."); return
    if not context.args:
        await update.message.reply_text("⚠️ Format: /setwelcome Teks sambutan baru Anda"); return
    teks = " ".join(context.args)
    if set_template("welcome", teks):
        await update.message.reply_text(f"✅ Sambutan berhasil diubah:\n\n{teks}")
    else:
        await update.message.reply_text("⚠️ Gagal menyimpan.")

async def setotp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Akses ditolak."); return
    if not context.args:
        await update.message.reply_text("⚠️ Format: /setotp Teks dengan {otp}\nContoh: /setotp 🔑 OTP: {otp}"); return
    teks = " ".join(context.args)
    if set_template("otp_found", teks):
        await update.message.reply_text(f"✅ Template OTP berhasil diubah.\n\nGunakan placeholder: {{otp}}, {{nomor}}, {{order_id}}")
    else:
        await update.message.reply_text("⚠️ Gagal menyimpan.")

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Akses ditolak."); return
    try:
        if os.path.exists(DB_PATH):
            with open(DB_PATH, 'rb') as f:
                await update.message.reply_document(document=f, filename=f"backup_{time.strftime('%Y%m%d_%H%M')}.db", caption="🗄️ Backup Manual Database")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Gagal backup: {str(e)[:100]}")

# --- MAIN ---
async def post_init(application):
    """Daftarkan commands otomatis (fitur #1) & mulai backup task (fitur #8)."""
    commands = [
        BotCommand("start", "🚀 Menu utama"),
        BotCommand("beli", "🛒 Beli nomor Gojek"),
        BotCommand("saldo", "💰 Cek saldo"),
        BotCommand("aktif", "📦 Nomor aktif"),
        BotCommand("riwayat", "📜 Riwayat transaksi"),
        BotCommand("stats", "📊 Statistik"),
        BotCommand("batal", "❌ Batalkan nomor"),
        BotCommand("selesai", "✅ Selesaikan nomor"),
        BotCommand("help", "❓ Bantuan"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception as e:
        print(f"Set commands error: {e}")
    # Mulai backup otomatis
    asyncio.create_task(auto_backup_task(application))

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("saldo", saldo_command))
    application.add_handler(CommandHandler("beli", beli_command))
    application.add_handler(CommandHandler("batal", batal_command))
    application.add_handler(CommandHandler("selesai", selesai_command))
    application.add_handler(CommandHandler("aktif", aktif_command))
    application.add_handler(CommandHandler("riwayat", riwayat_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("setwelcome", setwelcome_command))
    application.add_handler(CommandHandler("setotp", setotp_command))
    application.add_handler(CommandHandler("backup", backup_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    print("Bot berjalan...")
    application.run_polling()
