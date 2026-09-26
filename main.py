import logging
import os
import json
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

# --- FUNGSI BANTUAN ---
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

def get_gojek_products():
    """Mengambil dan mengurutkan produk Gojek dari termurah."""
    data = http_get(f"{BASE_URL}/catalog/products?country_id=7")
    if "error" in data:
        return None, data["error"]
    if not data.get("success"):
        return None, "Gagal mengambil data dari API."
    products = data.get("data", [])
    if not isinstance(products, list):
        return None, "Format data dari API tidak valid."
    gojek = [p for p in products if "Gojek" in p.get("name", "")]
    gojek.sort(key=lambda x: x.get('price', 999999))
    return gojek, None

def cek_saldo_api():
    if not SMScode_API_KEY:
        return "❌ API Key belum diatur di Railway Variables."
    data = http_get(f"{BASE_URL}/balance")
    if "error" in data:
        return f"❌ Error: {data['error'][:200]}"
    if data.get("success"):
        saldo = data["data"].get("balance", 0)
        currency = data["data"].get("currency", "IDR")
        return f"💰 Saldo Anda saat ini: {saldo} {currency}"
    return f"⚠️ API Error: {data.get('error', {}).get('message', 'Unknown')}"

def beli_nomor_api(product_id):
    if not SMScode_API_KEY:
        return None, "❌ API Key belum diatur."
    data = http_post(f"{BASE_URL}/orders/create", {"product_id": product_id})
    if "error" in data:
        return None, f"❌ Error: {data['error'][:200]}"
    if data.get("success"):
        order_data = data["data"]["orders"][0]
        order_id = str(order_data["id"])
        nomor = order_data["phone_number"]
        return order_id, nomor
    return None, f"⚠️ Gagal beli: {data.get('error', {}).get('message', 'Unknown')}"

def cek_otp_api(order_id):
    data = http_get(f"{BASE_URL}/orders/{order_id}")
    if "error" in data:
        return f"❌ Error: {data['error'][:200]}"
    if data.get("success"):
        order_data = data["data"]
        status = order_data.get("status")
        otp = order_data.get("otp_code")
        if otp:
            return f"🔑 <b>Kode OTP ditemukan:</b> <code>{otp}</code>\n\nSilakan masukkan ke aplikasi Gojek, lalu ketik /selesai {order_id}."
        elif status == "ACTIVE":
            return "⏳ Status: Menunggu SMS masuk."
        else:
            return f"ℹ️ Status Order: {status}. OTP belum tersedia."
    return f"⚠️ API Error: {data.get('error', {}).get('message', 'Unknown')}"

def selesai_order_api(order_id):
    data = http_post(f"{BASE_URL}/orders/finish", {"id": int(order_id)})
    if "error" in data:
        return f"❌ Error: {data['error'][:200]}"
    if data.get("success"):
        return f"✅ Order {order_id} berhasil diselesaikan."
    return f"⚠️ Gagal menyelesaikan order: {data.get('error', {}).get('message', 'Unknown')}"

async def auto_poll_otp(chat_id: int, context: ContextTypes.DEFAULT_TYPE, order_id: str):
    """Tugas latar belakang memantau OTP hingga 25 menit (300 iterasi x 5 detik)."""
    for i in range(300):
        await asyncio.sleep(5)
        data = http_get(f"{BASE_URL}/orders/{order_id}")
        if data.get("success"):
            order_data = data["data"]
            otp = order_data.get("otp_code")
            status = order_data.get("status")
            if otp:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(f"🔑 <b>Kode OTP Ditemukan!</b>\n\n"
                              f"Kode: <code>{otp}</code>\n"
                              f"ID Order: <code>{order_id}</code>\n\n"
                              f"Silakan masukkan kode di atas ke aplikasi Gojek.\n"
                              f"Setelah selesai, ketik /selesai {order_id}."),
                        parse_mode='HTML'
                    )
                except Exception:
                    pass
                return
            elif status in ["CANCELLED", "EXPIRED"]:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Order {order_id} telah {status}.")
                except Exception:
                    pass
                return
    # Jika 25 menit habis
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"⏰ Pemantauan OTP untuk Order {order_id} telah berakhir (25 menit).\n\n"
                  f"Jika SMS baru masuk, Anda bisa cek manual dengan perintah:\n"
                  f"/otp {order_id}")
        )
    except Exception:
        pass

async def send_products_menu(chat_id, context, query=None):
    """Menampilkan tombol pilihan harga produk Gojek."""
    products, err = get_gojek_products()
    if err:
        text = f"❌ {err}"
        if query:
            await query.edit_message_text(text)
        else:
            await context.bot.send_message(chat_id, text)
        return

    if not products:
        text = "Tidak ada produk Gojek tersedia saat ini."
        if query:
            await query.edit_message_text(text)
        else:
            await context.bot.send_message(chat_id, text)
        return

    # Ambil 10 harga termurah (unik) sebagai tombol
    keyboard = []
    seen = set()
    for p in products:
        harga = p.get('price', '?')
        pid = p.get('id')
        if harga in seen:
            continue
        seen.add(harga)
        keyboard.append([InlineKeyboardButton(f"💵 Rp {harga}", callback_data=f"buy_{pid}")])
        if len(keyboard) >= 10:
            break

    keyboard.append([InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = ("🛒 <b>Pilih Harga Nomor Gojek</b>\n\n"
            "Silakan ketuk salah satu tombol di bawah untuk membeli nomor dengan harga tersebut.\n"
            "Bot akan otomatis mengirimkan kode OTP saat SMS masuk (maks. 25 menit).")

    if query:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode='HTML')

# --- HANDLER TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💰 Cek Saldo", callback_data='cek_saldo')],
        [InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🚀 <b>Selamat datang di Bot Reivaldo Nokos!</b>\n\n"
        "👋 Silakan pilih menu di bawah atau gunakan /help.",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ <b>Daftar Perintah:</b>\n"
        "🚀 /start - Mulai bot\n"
        "❓ /help - Bantuan\n"
        "💰 /saldo - Cek saldo\n"
        "🛒 /beli - Menu pilih harga nomor\n"
        "🔑 /otp [ID] - Cek OTP manual\n"
        "✅ /selesai [ID] - Selesaikan order\n"
        "🔍 /getid - Lihat semua ID produk",
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

    if data == 'cek_saldo':
        keyboard = [[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(cek_saldo_api(), reply_markup=reply_markup)

    elif data == 'beli_nomor':
        await send_products_menu(query.message.chat_id, context, query=query)

    elif data.startswith('buy_'):
        product_id = int(data.replace('buy_', ''))
        await query.edit_message_text("⏳ Sedang membeli nomor, mohon tunggu...")
        order_id, result = beli_nomor_api(product_id)
        if order_id:
            teks = (f"🛒 <b>Nomor berhasil dibeli!</b>\n\n"
                    f"Nomor: <code>{result}</code>\n"
                    f"ID Order: <code>{order_id}</code>\n\n"
                    f"📋 <b>Langkah selanjutnya:</b>\n"
                    f"1. Salin nomor di atas (ketuk untuk copy)\n"
                    f"2. Masukkan ke aplikasi Gojek\n"
                    f"3. Bot akan otomatis mengirim kode OTP saat SMS masuk (maks. 25 menit)\n"
                    f"4. Setelah selesai, ketik /selesai {order_id}")
            keyboard = [[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(teks, reply_markup=reply_markup, parse_mode='HTML')
            asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id))
        else:
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data='beli_nomor')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(f"❌ {result}", reply_markup=reply_markup)

    elif data == 'menu_utama':
        keyboard = [
            [InlineKeyboardButton("💰 Cek Saldo", callback_data='cek_saldo')],
            [InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🚀 <b>Menu Utama</b>\n\n"
            "👋 Silakan pilih menu di bawah.",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(cek_saldo_api())

async def beli_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_products_menu(update.effective_chat.id, context)

async def otp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Format salah. Gunakan: /otp 15595084")
        return
    await update.message.reply_text(cek_otp_api(context.args[0]), parse_mode='HTML')

async def selesai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Format salah. Gunakan: /selesai 15595084")
        return
    await update.message.reply_text(selesai_order_api(context.args[0]))

async def get_products_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products, err = get_gojek_products()
    if err:
        await update.message.reply_text(f"❌ {err}")
        return
    if not products:
        await update.message.reply_text("Tidak ada produk Gojek ditemukan.")
        return
    hasil = "Daftar Produk Gojek Indonesia (Termurah):\n"
    count = 0
    for p in products:
        hasil += f"ID: {p.get('id')} | Harga: {p.get('price', '?')}\n"
        count += 1
        if count >= 15:
            break
    await update.message.reply_text(hasil)

# --- MAIN PROGRAM ---
if __name__ == '__main__':
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("saldo", saldo_command))
    application.add_handler(CommandHandler("beli", beli_command))
    application.add_handler(CommandHandler("otp", otp_command))
    application.add_handler(CommandHandler("selesai", selesai_command))
    application.add_handler(CommandHandler("getid", get_products_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    print("Bot berjalan...")
    application.run_polling()
