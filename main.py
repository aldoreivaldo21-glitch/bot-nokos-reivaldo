import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

def is_admin(user_id):
    return str(user_id) == str(ADMIN_ID)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💰 Cek Saldo", callback_data='cek_saldo'),
         InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🚀 *Selamat datang di Bot Reivaldo Nokos!*\n\n"
        "👋 Silakan pilih menu di bawah atau gunakan /help untuk melihat perintah yang tersedia.",
        reply_markup=reply_markup,
        parse_mode='MarkdownV2'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Daftar Perintah:*\n"
        "🚀 /start - Mulai bot dan sambutan\n"
        "❓ /help - Menampilkan bantuan\n"
        "💰 /saldo - Cek saldo akun\n"
        "🛒 /beli - Beli nomor virtual",
        parse_mode='MarkdownV2'
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
        await query.edit_message_text("💰 Fitur saldo belum tersedia.", reply_markup=reply_markup)
    elif query.data == 'beli_nomor':
        await query.edit_message_text("🛒 Fitur beli belum tersedia.", reply_markup=reply_markup)
    elif query.data == 'menu_utama':
        keyboard = [
            [InlineKeyboardButton("💰 Cek Saldo", callback_data='cek_saldo'),
             InlineKeyboardButton("🛒 Beli Nomor", callback_data='beli_nomor')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🚀 *Selamat datang di Bot Reivaldo Nokos!*\n\n"
            "👋 Silakan pilih menu di bawah atau gunakan /help untuk melihat perintah yang tersedia.",
            reply_markup=reply_markup,
            parse_mode='MarkdownV2'
        )

if __name__ == '__main__':
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    print("Bot berjalan...")
    application.run_polling()
