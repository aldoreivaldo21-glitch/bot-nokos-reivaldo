import logging
import os
import json
import time
import urllib.request
import urllib.error
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# --- KONFIGURASI DARI RAILWAY VARIABLES ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597")
SMScode_API_KEY = os.environ.get("SMScode_API_KEY")

# --- KONFIGURASI API SMScode (V1 - IDR) ---
BASE_URL = "https://api.smscode.gg/v1"
HEADERS = {
    "Authorization": f"Bearer {SMScode_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# --- PENYIMPANAN MESSAGE ID UNTUK PESAN "STATUS: MENUNGGU" ---
# Format: {order_id: message_id}
pending_messages = {}

# --- FUNGSI BANTUAN HTTP ---
def http_get(url):
    req = urllib.request.Request(url, headers=HEADERS, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode()
            return {"error": f"HTTP {e.code}: {error_body[:200]}"}
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
            error_body = e.read().decode()
            return {"error": f"HTTP {e.code}: {error_body[:200]}"}
        except:
            return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}

def is_admin(user_id):
    return str(user_id) == str(ADMIN_ID)

def format_phone(phone):
    if phone.startswith("62"):
        return phone[2:]
    return phone

# --- FUNGSI API SMScode ---
def get_gojek_products():
    data = http_get(f"{BASE_URL}/catalog/products?country_id=7")
    if "error" in data: return None, data["error"]
    if not data.get("success"): return None, "Gagal mengambil data dari API."
    products = data.get("data", [])
    if not isinstance(products, list): return None, "Format data dari API tidak valid."
    gojek = [p for p in products if "Gojek" in p.get("name", "")]
    gojek.sort(key=lambda x: x.get('price', 999999))
    return gojek, None

def get_active_orders():
    data = http_get(f"{BASE_URL}/orders/active")
    if "error" in data: return None, data["error"]
    if not data.get("success"): return None, "Gagal mengambil daftar order aktif."
    orders = data.get("data", [])
    if not isinstance(orders, list): return None, "Format data order tidak valid."
    return orders, None

def cek_saldo_api():
    if not SMScode_API_KEY: return "❌ API Key belum diatur."
    data = http_get(f"{BASE_URL}/balance")
    if "error" in data: return f"❌ Error: {data['error'][:200]}"
    if data.get("success"):
        saldo = data["data"].get("balance", 0)
        currency = data["data"].get("currency", "IDR")
        return f"💰 Saldo Anda: {saldo} {currency}"
    return f"⚠️ API Error: {data.get('error', {}).get('message', 'Unknown')}"

def beli_nomor_api(product_id):
    if not SMScode_API_KEY: return None, "❌ API Key belum diatur."
    data = http_post(f"{BASE_URL}/orders/create", {"product_id": product_id})
    if "error" in data: return None, f"❌ Error: {data['error'][:200]}"
    if data.get("success"):
        order_data = data["data"]["orders"][0]
        return str(order_data["id"]), order_data["phone_number"]
    return None, f"⚠️ Gagal beli: {data.get('error', {}).get('message', 'Unknown')}"

def batal_order_api(order_id):
    data = http_post(f"{BASE_URL}/orders/cancel", {"id": int(order_id)})
    if "error" in data: return f"❌ Error: {data['error'][:200]}"
    if data.get("success"): return f"✅ Order <code>{order_id}</code> berhasil dibatalkan.\n💰 Saldo telah dikembalikan."
    return f"⚠️ Gagal batalkan: {data.get('error', {}).get('message', 'Unknown')}"

def selesai_order_api(order_id):
    data = http_post(f"{BASE_URL}/orders/finish", {"id": int(order_id)})
    if "error" in data: return f"❌ Error: {data['error'][:200]}"
    if data.get("success"): return f"✅ Order <code>{order_id}</code> berhasil diselesaikan."
    return f"⚠️ Gagal menyelesaikan: {data.get('error', {}).get('message', 'Unknown')}"

# --- AUTO POLLING OTP (MENGEDIT PESAN "STATUS: MENUNGGU") ---
async def auto_poll_otp(chat_id: int, context: ContextTypes.DEFAULT_TYPE, order_id: str, nomor_display: str):
    total_seconds = 25 * 60  # 25 menit

    for i in range(total_seconds // 5):
        await asyncio.sleep(5)
        elapsed = (i + 1) * 5
        remaining = total_seconds - elapsed

        # Cek OTP ke API
        data = http_get(f"{BASE_URL}/orders/{order_id}")
        if not data.get("success"):
            continue

        order_data = data["data"]
        otp = order_data.get("otp_code")
        status = order_data.get("status")

        # Ambil message_id pesan "Status: Menunggu" jika ada
        msg_id = pending_messages.get(order_id)

        if otp:
            # === OTP DITEMUKAN: EDIT PESAN "STATUS: MENUNGGU" ===
            if msg_id:
                try:
                    keyboard = [
                        [InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")],
                        [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
                    ]
                    teks = (f"🔑 <b>Kode OTP Ditemukan!</b>\n\n"
                            f"Kode: <code>{otp}</code>\n\n"
                            f"Masukkan kode di atas ke aplikasi Gojek, lalu klik tombol ✅ Selesai.")
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id, text=teks,
                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
                    )
                    # Hapus dari penyimpanan karena sudah selesai
                    pending_messages.pop(order_id, None)
                except Exception:
                    pass
            else:
                # Fallback: Jika user belum klik "Cek OTP Sekarang", kirim pesan baru
                try:
                    keyboard = [
                        [InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")],
                        [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
                    ]
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(f"🔑 <b>Kode OTP Ditemukan!</b>\n\n"
                              f"Nomor: <code>{nomor_display}</code>\n"
                              f"Kode: <code>{otp}</code>\n\n"
                              f"Masukkan kode ke aplikasi Gojek, lalu klik ✅ Selesai."),
                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
                    )
                except Exception:
                    pass
            return

        elif status in ["CANCELLED", "EXPIRED", "COMPLETED"]:
            if msg_id:
                try:
                    keyboard = [[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id,
                        text=f"ℹ️ Order <code>{order_id}</code> telah {status}.",
                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
                    )
                    pending_messages.pop(order_id, None)
                except Exception:
                    pass
            return

        elif status == "ACTIVE" and remaining > 0:
            # Update countdown di pesan "Status: Menunggu" (jika user sudah klik)
            if msg_id:
                mins = remaining // 60
                secs = remaining % 60
                try:
                    keyboard = [
                        [InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
                        [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
                    ]
                    teks = (f"⏳ <b>Status:</b> Menunggu SMS masuk\n"
                            f"⏱️ Sisa waktu: <b>{mins:02d}:{secs:02d}</b>\n\n"
                            f"Bot akan otomatis mengirim kode OTP saat SMS masuk.")
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id, text=teks,
                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
                    )
                except Exception:
                    pass

    # Waktu habis (25 menit)
    if msg_id:
        try:
            keyboard = [[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=f"⏰ <b>Waktu Habis</b>\n\nPemantauan OTP untuk Order <code>{order_id}</code> telah berakhir.\nJika SMS baru masuk, cek manual dari menu /aktif.",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
            )
            pending_messages.pop(order_id, None)
        except Exception:
            pass

# --- TAMPILAN MENU ---
async def send_products_menu(chat_id, context, query=None):
    products, err = get_gojek_products()
    if err:
        text = f"❌ {err}"
        if query: await query.edit_message_text(text)
        else: await context.bot.send_message(chat_id, text)
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
        if harga in seen: continue
        seen.add(harga)
        keyboard.append([InlineKeyboardButton(f"💵 Rp {harga}", callback_data=f"buy_{pid}")])
        if len(keyboard) >= 10: break
    keyboard.append([InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')])

    text = ("🛒 <b>Pilih Harga Nomor Gojek</b>\n\n"
            "Ketuk salah satu tombol untuk membeli.\n"
            "Bot akan otomatis mengirim OTP saat SMS masuk (maks. 25 menit).")
    reply_markup = InlineKeyboardMarkup(keyboard)
    if query: await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')
    else: await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode='HTML')

async def send_active_orders_menu(chat_id, context, action, query=None):
    orders, err = get_active_orders()
    if err:
        text = f"❌ {err}"
        if query: await query.edit_message_text(text)
        else: await context.bot.send_message(chat_id, text)
        return
    if not orders:
        text = "📭 Tidak ada nomor aktif saat ini."
        if query: await query.edit_message_text(text)
        else: await context.bot.send_message(chat_id, text)
        return

    if action == 'cancel':
        judul = "❌ <b>Pilih Nomor yang Ingin Dibatalkan</b>"
        prefix = "cancel_"
        keterangan = "Klik tombol di bawah untuk membatalkan. Saldo akan dikembalikan."
    else:
        judul = "✅ <b>Pilih Nomor yang Ingin Diselesaikan</b>"
        prefix = "finish_"
        keterangan = "Klik tombol di bawah untuk melepas nomor setelah OTP digunakan."

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

    text = f"{judul}\n\n{keterangan}\n\nTotal: {len(orders)} nomor"
    reply_markup = InlineKeyboardMarkup(keyboard)
    if query: await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')
    else: await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode='HTML')

# --- HANDLER TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')],
        [InlineKeyboardButton("💰 Cek Saldo", callback_data='cek_saldo'),
         InlineKeyboardButton("📦 Nomor Aktif", callback_data='aktif')]
    ]
    await update.message.reply_text(
        "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>\n\n"
        "👋 Silakan pilih menu di bawah.\n\n"
        "💡 <b>Tips Cepat:</b>\n"
        "• /beli untuk membeli nomor\n"
        "• /batal untuk membatalkan\n"
        "• /selesai untuk melepas nomor\n"
        "• /aktif untuk melihat nomor aktif",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ <b>Daftar Perintah:</b>\n\n"
        "🚀 /start - Menu utama\n"
        "🛒 /beli - Beli nomor Gojek\n"
        "💰 /saldo - Cek saldo\n"
        "📦 /aktif - Lihat nomor aktif\n"
        "❌ /batal - Batalkan nomor\n"
        "✅ /selesai - Selesaikan nomor\n\n"
        "💡 <b>Tidak perlu ketik ID, semua lewat tombol!</b>",
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
             InlineKeyboardButton("📦 Nomor Aktif", callback_data='aktif')]
        ]
        await query.edit_message_text(
            "🚀 <b>Menu Utama</b>\n\n👋 Silakan pilih menu di bawah.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )

    elif data == 'cek_saldo':
        keyboard = [[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
        await query.edit_message_text(cek_saldo_api(), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == 'beli_nomor':
        await send_products_menu(query.message.chat_id, context, query=query)

    elif data == 'aktif':
        await send_active_orders_menu(query.message.chat_id, context, 'cancel', query=query)

    elif data.startswith('buy_'):
        product_id = int(data.replace('buy_', ''))
        await query.edit_message_text("⏳ Sedang membeli nomor...")
        order_id, nomor_raw = beli_nomor_api(product_id)
        if order_id:
            nomor_display = format_phone(nomor_raw)
            keyboard = [
                [InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
                [InlineKeyboardButton("🔄 Cek OTP Sekarang", callback_data=f"checkotp_{order_id}")],
                [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
            ]
            # Pesan ini TIDAK akan diubah oleh auto-poll. Tetap utuh.
            teks = (f"🛒 <b>Nomor berhasil dibeli!</b>\n\n"
                    f"📱 Nomor: <code>{nomor_display}</code>\n"
                    f"🆔 ID: <code>{order_id}</code>\n\n"
                    f"📋 <b>Langkah selanjutnya:</b>\n"
                    f"1️⃣ Ketuk nomor di atas untuk salin\n"
                    f"2️⃣ Masukkan ke aplikasi Gojek\n"
                    f"3️⃣ Ketuk <b>Cek OTP Sekarang</b> untuk memantau SMS masuk\n\n"
                    f"⚠️ Tidak jadi pakai? Ketuk tombol ❌ Batalkan Order.")
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            # Jalankan auto-poll di background (akan mengedit pesan "Status: Menunggu" jika ada)
            asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id, nomor_display))
        else:
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]]
            await query.edit_message_text(f"❌ {nomor_raw}", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith('checkotp_'):
        # === INI PESAN "STATUS: MENUNGGU SMS MASUK" YANG AKAN DI-EDIT ===
        order_id = data.replace('checkotp_', '')
        # Cek dulu apakah OTP sudah ada
        cek = http_get(f"{BASE_URL}/orders/{order_id}")
        otp_sekarang = None
        if cek.get("success"):
            otp_sekarang = cek["data"].get("otp_code")

        if otp_sekarang:
            # OTP sudah ada, langsung tampilkan
            keyboard = [
                [InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")],
                [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
            ]
            await query.edit_message_text(
                f"🔑 <b>Kode OTP Ditemukan!</b>\n\nKode: <code>{otp_sekarang}</code>\n\nMasukkan ke aplikasi Gojek, lalu klik ✅ Selesai.",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
            )
        else:
            # OTP belum ada, tampilkan "Status: Menunggu SMS masuk"
            keyboard = [
                [InlineKeyboardButton("❌ Batalkan Order", callback_data=f"cancel_{order_id}")],
                [InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]
            ]
            teks = (f"⏳ <b>Status:</b> Menunggu SMS masuk\n"
                    f"⏱️ Sisa waktu: <b>25:00</b>\n\n"
                    f"Bot akan otomatis mengubah pesan ini menjadi kode OTP saat SMS masuk.")
            # Kirim pesan baru "Status: Menunggu" (pesan terpisah dari "Nomor berhasil dibeli")
            try:
                # Edit pesan yang sama (pesan tombol) menjadi "Status: Menunggu"
                await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            except Exception:
                pass
            # Simpan message_id pesan ini untuk diedit oleh auto-poll
            # Karena kita menggunakan edit_message_text, message_id tetap sama dengan pesan tombol sebelumnya
            # Kita perlu mengambil message_id dari pesan yang sedang di-edit
            # Sayangnya, query.message.message_id adalah ID pesan asli sebelum diedit
            # Jadi kita simpan ID pesan yang diklik
            pending_messages[order_id] = query.message.message_id

    elif data.startswith('cancel_'):
        order_id = data.replace('cancel_', '')
        await query.edit_message_text(f"⏳ Membatalkan Order <code>{order_id}</code>...", parse_mode='HTML')
        hasil = batal_order_api(order_id)
        # Hapus dari pending messages karena sudah dibatalkan
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

async def get_products_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products, err = get_gojek_products()
    if err:
        await update.message.reply_text(f"❌ {err}")
        return
    if not products:
        await update.message.reply_text("Tidak ada produk Gojek ditemukan.")
        return
    hasil = "📋 <b>Daftar Produk Gojek (Termurah):</b>\n\n"
    for p in products[:15]:
        hasil += f"• Rp {p.get('price', '?')} (ID: <code>{p.get('id')}</code>)\n"
    await update.message.reply_text(hasil, parse_mode='HTML')

# --- MAIN PROGRAM ---
if __name__ == '__main__':
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("saldo", saldo_command))
    application.add_handler(CommandHandler("beli", beli_command))
    application.add_handler(CommandHandler("batal", batal_command))
    application.add_handler(CommandHandler("selesai", selesai_command))
    application.add_handler(CommandHandler("aktif", aktif_command))
    application.add_handler(CommandHandler("getid", get_products_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    print("Bot berjalan...")
    application.run_polling()
