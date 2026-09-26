import logging
import os
import json
import urllib.request
import urllib.error
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
    "Content-Type": "application/json"
}

# ⚠️ NANTI GANTI ANGKA 142 INI DENGAN ID DARI PERINTAH /getid ⚠️
GOJEK_PRODUCT_ID = 142 

# --- FUNGSI BANTUAN (TANPA REQUESTS) ---
def http_get(url):
    req = urllib.request.Request(url, headers=HEADERS, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        return {"error": str(e)}

def http_post(url, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=HEADERS, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        return {"error": str(e)}

def is_admin(user_id):
    return str(user_id) == str(ADMIN_ID)

def cek_saldo_api():
    if not SMScode_API_KEY:
        return "❌ API Key belum diatur di Railway Variables."
    data = http_get(f"{BASE_URL}/balance")
    if "error" in data:
        return f"❌ Error koneksi: {data['error'][:50]}"
    if data.get("success"):
        saldo = data["data"].get("balance", 0)
        currency = data["data"].get("currency", "IDR")
        return f"💰 Saldo Anda saat ini: {saldo} {currency}"
    return f"⚠️ API Error: {data.get('error', {}).get('message', 'Unknown')}"

def beli_nomor_api():
    if not SMScode_API_KEY:
        return "❌ API Key belum diatur."
    data = http_post(f"{BASE_URL}/orders/create", {"product_id": GOJEK_PRODUCT_ID})
    if "error" in data:
        return f"❌ Error koneksi: {data['error'][:50]}"
    if data.get("success"):
        order_data = data["data"]["orders"][0]
        order_id = order_data["id"]
        nomor = order_data["phone_number"]
        return (f"🛒 Nomor berhasil dibeli: {nomor}\n"
                f"ID Order: {order_id}\n\n"
                f"Silakan masukkan nomor ini ke aplikasi Gojek.\n"
                f"Setelah itu, kirim perintah:\n"
                f"/otp {order_id} untuk mengecek kode OTP.")
    return f"⚠️ Gagal beli: {data.get('error', {}).get('message', 'Unknown')}"

def cek_otp_api(order_id):
    data = http_get(f"{BASE_URL}/orders/{order_id}")
    if "error" in data:
        return f"❌ Error koneksi: {data['error'][:50]}"
    if data.get("success"):
        order_data = data["data"]
        status = order_data.get("status")
        otp = order_data.get("otp_code")
        if otp:
            return f"🔑 Kode OTP ditemukan: `{otp}`\n\nSilakan masukkan ke aplikasi Gojek, lalu ketik `/selesai {order_id}`."
        elif status == "ACTIVE":
            return "⏳ Status: Menunggu SMS masuk. Coba cek lagi 10 detik kemudian."
        else:
            return f"ℹ️ Status Order: {status}. OTP belum tersedia."
    return f"⚠️ API Error: {data.get('error', {}).get('message', 'Unknown')}"

def selesai_order_api(order_id):
    data = http_post(f"{BASE_URL}/orders/finish", {"id": int(order_id)})
    if "error" in data:
        return f"❌ Error koneksi: {data['error'][:50]}"
    if data.get("success"):
        return f"✅ Order {order_id} berhasil diselesaikan."
    return f"⚠️ Gagal menyelesaikan order: {data.get('error', {}).get('message', 'Unknown')}"

async def get_products_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not SMScode_API_KEY:
        await update.message.reply_text("❌ API Key belum diatur.")
        return
    data = http_get(f"{BASE_URL}/catalog/products?country_id=6")
    if "error" in data:
        await update.message.reply_text(f"❌ Error: {data['error'][:50]}")
        return
    if data.get("success"):
        products = data.get("data", [])
        hasil = "Daftar Produk Gojek:\n"
        for p in products:
            if "Gojek" in p.get("name", ""):
                hasil += f"ID: {p['id']} | Harga: {p.get('price', {}).get('amount', '?')}\n"
        if hasil == "Daftar Produk Gojek:\n":
            await update.message.reply_text("Tidak ada produk Gojek ditemukan.")
        else:
            await update.message.reply_text(hasil[:4000])
    else:
        await update.message.reply_text("Gagal mengambil data dari API.")

# --- HANDLER TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💰 Cek Saldo", callback_data='cek_saldo'),
         InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🚀 Selamat datang di Bot Reivaldo Nokos!\n\n"
        "👋 Silakan pilih menu di bawah atau gunakan /help.",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ Daftar Perintah:\n"
        "🚀 /start - Mulai bot\n"
        "❓ /help - Bantuan\n"
        "💰 /saldo - Cek saldo\n"
        "🛒 /beli - Beli nomor\n"
        "🔑 /otp [ID] - Cek OTP\n"
        "✅ /selesai [ID] - Selesaikan order\n"
        "🔍 /getid - Lihat daftar ID produk"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.edit_message_text("🚫 Akses ditolak.")
        return

    keyboard = [[InlineKeyboardButton("📋 Menu Utama", callback_data='menu_utama')]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if query.data == 'cek_saldo':
        await query.edit_message_text(cek_saldo_api(), reply_markup=reply_markup)
    elif query.data == 'beli_nomor':
        await query.edit_message_text(beli_nomor_api(), reply_markup=reply_markup)
    elif query.data == 'menu_utama':
        keyboard = [
            [InlineKeyboardButton("💰 Cek Saldo", callback_data='cek_saldo'),
             InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🚀 Selamat datang di Bot Reivaldo Nokos!\n\n"
            "👋 Silakan pilih menu di bawah.",
            reply_markup=reply_markup
        )

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(cek_saldo_api())

async def beli_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(beli_nomor_api())

async def otp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Format salah. Gunakan: /otp 1001")
        return
    await update.message.reply_text(cek_otp_api(context.args[0]))

async def selesai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Format salah. Gunakan: /selesai 1001")
        return
    await update.message.reply_text(selesai_order_api(context.args[0]))

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
