import os
import json
import time
import sqlite3
import urllib.request
import urllib.error
import asyncio
import traceback
import html
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import RetryAfter, BadRequest, Forbidden

# ==========================================
# KONFIGURASI UTAMA
# ==========================================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = os.environ.get("ADMIN_TELEGRAM_ID", "7854456597").strip()
_raw_key = os.environ.get("SMScode_API_KEY") or os.environ.get("SMSCODE_API_KEY") or ""
SMScode_API_KEY = _raw_key.strip()

BOT_VERSION = "26.1 Admin Pro"
START_TIME = time.time()
MAX_BULK = 100
BATCH_SIZE = 10
TIMEOUT_MINUTES = 25
TIMEOUT_SECONDS = TIMEOUT_MINUTES * 60

BASE_URL = "https://api.smscode.gg/v1"
HEADERS = {
    "Authorization": f"Bearer {SMScode_API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")
pending_messages = {}
active_bulk_data = {}
_processing_users = {}
_user_last_action = {}
_semaphore = asyncio.Semaphore(5)

# ==========================================
# HELPER & UTILITY
# ==========================================
def format_rupiah(n):
    try: return f"{int(n):,}".replace(",", ".")
    except: return str(n)

def format_countdown(seconds):
    if seconds < 0: seconds = 0
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{m:02d}:{s:02d}"

def format_phone(p):
    if not p: return "-"
    p_str = str(p).strip()
    if p_str in ("None", "", "-"): return "-"
    if p_str.startswith("62"): return p_str[2:]
    return p_str

def is_admin(uid): return str(uid) == str(ADMIN_ID)

def try_acquire_lock(uid):
    now = time.time()
    for k in list(_processing_users.keys()):
        if now - _processing_users[k] > 60:
            del _processing_users[k]
    if now - _processing_users.get(uid, 0) < 5:
        return False
    _processing_users[uid] = now
    return True

def release_lock(uid): _processing_users.pop(uid, None)

def check_rate_limit(uid, interval=0.7):
    now = time.time()
    if now - _user_last_action.get(uid, 0) < interval:
        return False
    _user_last_action[uid] = now
    return True

async def send_temp_msg(context, chat_id, text, delay=4):
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
        await asyncio.sleep(delay)
        await msg.delete()
    except: pass

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("🛒 Beli Nomor"), KeyboardButton("📦 Beli Banyak (Bulk)")],
        [KeyboardButton("💰 Cek Saldo"), KeyboardButton("📜 Riwayat")],
        [KeyboardButton("📦 Order Aktif"), KeyboardButton("ℹ️ Bantuan")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

def get_admin_keyboard():
    keyboard = [
        [KeyboardButton("👑 Panel Admin"), KeyboardButton("📢 Broadcast")],
        [KeyboardButton("🔧 Maintenance"), KeyboardButton("🔙 Menu Utama")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

# ==========================================
# DATABASE
# ==========================================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY, user_id INTEGER, phone TEXT, product_id INTEGER,
        price INTEGER, status TEXT, otp TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    conn.commit(); conn.close()

def db_save_order(order_id, user_id, phone, product_id, price):
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO orders (order_id, user_id, phone, product_id, price, status) VALUES (?, ?, ?, ?, ?, 'ACTIVE')",
                     (str(order_id), user_id, phone, product_id, price))
        conn.commit(); conn.close()
    except Exception as e: print(f"DB Error: {e}")

def db_update_order(order_id, status, otp=None):
    try:
        conn = get_db()
        if otp: conn.execute("UPDATE orders SET status=?, otp=? WHERE order_id=?", (status, otp, str(order_id)))
        else: conn.execute("UPDATE orders SET status=? WHERE order_id=?", (status, str(order_id)))
        conn.commit(); conn.close()
    except Exception as e: print(f"DB Error: {e}")

def db_get_history(user_id, limit=15):
    try:
        conn = get_db()
        q = "SELECT order_id, phone, status, otp, datetime(created_at, '+7 hours') FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?"
        rows = conn.execute(q, (user_id, limit)).fetchall()
        conn.close(); return rows
    except: return []

def get_setting(key, default=None):
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close(); return row[0] if row else default
    except: return default

def set_setting(key, value):
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit(); conn.close()
    except Exception as e: print(f"Setting error: {e}")

def db_bot_stats():
    try:
        conn = get_db()
        total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        active_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE status='ACTIVE'").fetchone()[0]
        conn.close()
        return total_orders, active_orders
    except: return 0, 0

# ==========================================
# HTTP ASYNC (DENGAN AUTO RETRY 502)
# ==========================================
def _http_get(url, retries=3):
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=HEADERS, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in [502, 503, 504] and attempt < retries - 1:
                print(f"⚠️ Server API Error {e.code}. Mencoba lagi ({attempt+1}/{retries})...")
                time.sleep(2)
                continue
            if e.code == 401: return {"error": "API Key salah."}
            try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
            except: return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return {"error": str(e)}

def _http_post(url, payload, retries=3):
    data = json.dumps(payload).encode('utf-8')
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=HEADERS, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in [502, 503, 504] and attempt < retries - 1:
                print(f"⚠️ Server API Error {e.code}. Mencoba lagi ({attempt+1}/{retries})...")
                time.sleep(2)
                continue
            if e.code == 401: return {"error": "API Key salah."}
            try: return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
            except: return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return {"error": str(e)}

async def http_get(url):
    async with _semaphore:
        return await asyncio.to_thread(_http_get, url)

async def http_post(url, payload):
    async with _semaphore:
        return await asyncio.to_thread(_http_post, url, payload)

def translate_error(err_msg):
    e = str(err_msg).lower()
    if "unauthorized" in e: return "🔐 API Key salah."
    if "insufficient_balance" in e: return "💸 Saldo tidak cukup."
    if "no numbers available" in e: return "📭 Stok habis."
    if "cancel_too_early" in e: return "⏳ Tunggu 2 menit sebelum batal."
    if "cannot cancel order" in e: return "⚠️ Order sudah OTP/selesai."
    if "timeout" in e: return "🌐 Koneksi timeout."
    return f"⚠️ {err_msg[:100]}"

async def get_balance():
    data = await http_get(f"{BASE_URL}/balance")
    if data.get("success"): return data["data"].get("balance", 0)
    return -1

# ==========================================
# API SMScode
# ==========================================
async def get_gojek_products():
    data = await http_get(f"{BASE_URL}/catalog/products?country_id=7")
    if "error" in data: return None, translate_error(data["error"])
    products = data.get("data", [])
    if not isinstance(products, list): return None, "Format katalog tidak valid."
    gojek = [p for p in products if "Gojek" in p.get("name", "")]
    gojek.sort(key=lambda x: x.get('price', 999999))
    return gojek, None

async def get_active_orders():
    data = await http_get(f"{BASE_URL}/orders/active")
    if "error" in data: return None, translate_error(data["error"])
    raw = data.get("data", [])
    if isinstance(raw, dict) and "orders" in raw: orders = raw["orders"]
    elif isinstance(raw, list): orders = raw
    else: orders = []
    return orders, None

async def cek_saldo_api():
    if not SMScode_API_KEY: return "❌ API Key belum diatur.", -1
    data = await http_get(f"{BASE_URL}/balance")
    if "error" in data: return translate_error(data["error"]), -1
    if data.get("success"):
        saldo = data["data"].get("balance", 0)
        return f"💰 Saldo Saya: <b>Rp {format_rupiah(saldo)}</b>", saldo
    return "⚠️ Gagal ambil saldo.", -1

async def beli_nomor_api(product_id, user_id, price):
    saldo = await get_balance()
    if saldo >= 0 and saldo < price:
        return None, f"❌ Saldo tidak cukup (Butuh: Rp {format_rupiah(price)})."
    data = await http_post(f"{BASE_URL}/orders/create", {"product_id": product_id})
    if "error" in data: return None, translate_error(data["error"])
    if data.get("success"):
        od = data["data"]["orders"][0]
        oid = str(od["id"]); nomor = od.get("phone_number")
        if not nomor:
            detail = await http_get(f"{BASE_URL}/orders/{oid}")
            if detail.get("success"): nomor = detail["data"].get("phone_number")
        nomor = nomor or "-"
        db_save_order(oid, user_id, nomor, product_id, price)
        return oid, nomor
    return None, "⚠️ Gagal membeli."

async def beli_banyak_api_async(product_id, user_id, price, quantity):
    total_cost = price * quantity
    saldo = await get_balance()
    if saldo >= 0 and saldo < total_cost:
        return None, 0, f"❌ Saldo tidak cukup (Butuh: Rp {format_rupiah(total_cost)})."

    all_results = []
    total_failed = 0
    remaining = quantity

    while remaining > 0:
        this_batch = min(BATCH_SIZE, remaining)
        success = False

        for attempt in range(3):
            res = await http_post(f"{BASE_URL}/orders/create", {"product_id": product_id, "quantity": this_batch})
            parsed = []
            raw_data = {}
            if isinstance(res, dict) and res.get("success"):
                raw_data = res.get("data", {})
                if isinstance(raw_data, dict) and "orders" in raw_data: parsed = raw_data["orders"]
                elif isinstance(raw_data, list): parsed = raw_data

            if parsed:
                for od in parsed:
                    oid = str(od.get("id")); nomor = od.get("phone_number")
                    if not nomor:
                        detail = await http_get(f"{BASE_URL}/orders/{oid}")
                        if detail.get("success"): nomor = detail["data"].get("phone_number")
                    nomor = nomor or "-"
                    db_save_order(oid, user_id, nomor, product_id, price)
                    all_results.append((oid, nomor))
                failed = raw_data.get("failed_count", 0) if isinstance(raw_data, dict) else 0
                total_failed += failed
                remaining -= (len(parsed) + failed)
                success = True
                break
            await asyncio.sleep(3)

        if not success: break
        if remaining <= 0: break
        await asyncio.sleep(1.5)

    if not all_results:
        return None, 0, "⚠️ Tidak ada order yang berhasil (Server API sibuk)."
    return all_results, total_failed, None

async def batal_order_api(order_id):
    data = await http_post(f"{BASE_URL}/orders/cancel", {"id": int(order_id)})
    if "error" in data: return {"success": False, "message": translate_error(data["error"])}
    if data.get("success"):
        db_update_order(order_id, "CANCELLED")
        return {"success": True, "message": "✅ Berhasil Dibatalkan"}
    return {"success": False, "message": "⚠️ Gagal Batal"}

async def selesai_order_api(order_id):
    data = await http_post(f"{BASE_URL}/orders/finish", {"id": int(order_id)})
    if "error" in data: return {"success": False, "message": translate_error(data["error"])}
    if data.get("success"):
        db_update_order(order_id, "COMPLETED")
        return {"success": True, "message": "✅ Selesai"}
    return {"success": False, "message": "⚠️ Gagal Selesai"}

# ==========================================
# POLLING OTP (Single Order)
# ==========================================
async def auto_poll_otp(chat_id, context, order_id, nomor_display, pid, price):
    start_time = time.time()
    msg_id = None

    while (time.time() - start_time) < TIMEOUT_SECONDS:
        await asyncio.sleep(2)
        remaining = TIMEOUT_SECONDS - int(time.time() - start_time)

        if order_id not in pending_messages and msg_id is not None:
            return

        data = await http_get(f"{BASE_URL}/orders/{order_id}")
        if not data.get("success"): continue
        od = data["data"]; otp = od.get("otp_code"); status = od.get("status")

        if otp:
            db_update_order(order_id, "OTP_RECEIVED", otp)
            teks = f"🔑 <b>OTP:</b> <code>{otp}</code>\n📱 <b>Nomor:</b> <code>{nomor_display}</code>"
            kb = [[InlineKeyboardButton("✅ Selesai & Lepas Nomor", callback_data=f"finish_{order_id}")]]
            if msg_id:
                try:
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=teks,
                        reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                    pending_messages.pop(order_id, None)
                except BadRequest:
                    try: await context.bot.send_message(chat_id=chat_id, text=teks,
                        reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                    except: pass
                except: pass
            else:
                try: await context.bot.send_message(chat_id=chat_id, text=teks,
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                except: pass
            return
        elif status in ["CANCELLED", "EXPIRED", "COMPLETED"]:
            db_update_order(order_id, status)
            if msg_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except: pass
                pending_messages.pop(order_id, None)
            return
        elif status == "ACTIVE" and not msg_id:
            try:
                kb = [
                    [InlineKeyboardButton("🔄 Ganti Nomor", callback_data=f"replace_{order_id}_{pid}_{price}")],
                    [InlineKeyboardButton("❌ Batalkan", callback_data=f"cancel_{order_id}")]
                ]
                msg = await context.bot.send_message(chat_id=chat_id,
                    text=f"⏳ <b>Menunggu SMS...</b>\n📱 <code>{nomor_display}</code>\n⏱️ Sisa: <b>{format_countdown(remaining)}</b>",
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                msg_id = msg.message_id
                pending_messages[order_id] = msg_id
            except: pass
        elif status == "ACTIVE" and msg_id and (int(time.time() - start_time) % 10) == 0:
            try:
                kb = [
                    [InlineKeyboardButton("🔄 Ganti Nomor", callback_data=f"replace_{order_id}_{pid}_{price}")],
                    [InlineKeyboardButton("❌ Batalkan", callback_data=f"cancel_{order_id}")]
                ]
                await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                    text=f"⏳ <b>Menunggu SMS...</b>\n📱 <code>{nomor_display}</code>\n⏱️ Sisa: <b>{format_countdown(remaining)}</b>",
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
            except: pass

    await batal_order_api(order_id)
    if msg_id:
        try: await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except: pass
        await send_temp_msg(context, chat_id, "⌛ Waktu habis, nomor dibatalkan.")

# ==========================================
# BULK SUMMARY & MONITOR
# ==========================================
def build_bulk_summary(ordered_list, received_set, price_per_unit, remaining_seconds):
    total = len(ordered_list)
    header = (f"🛒 <b>Bulk Order ({total} Nomor)</b>\n"
              f"📊 Progress: <b>{len(received_set)}/{total}</b>\n"
              f"⏱️ Sisa: <b>{format_countdown(remaining_seconds)}</b>\n"
              f"💰 Total: <b>Rp {format_rupiah(price_per_unit * total)}</b>\n\n")
    lines = []
    for i, (oid, phone) in enumerate(ordered_list, 1):
        check = " ✅" if oid in received_set else ""
        lines.append(f"{i}. <code>{phone}</code>{check}")
    body = "\n".join(lines)
    full = header + body
    if len(full) <= 4000:
        return full
    compact_lines = []
    for i, (oid, phone) in enumerate(ordered_list, 1):
        check = "✅" if oid in received_set else ""
        compact_lines.append(f"{i}.{phone}{check}")
    compact_body = " ".join(compact_lines)
    compact_full = header + compact_body
    if len(compact_full) <= 4000:
        return compact_full
    return compact_full[:3950] + "\n<i>...(terlalu panjang)</i>"

async def bulk_monitor_task(context, chat_id, user_id, batch_id, ordered_list, price_per_unit, summary_msg_id, pid):
    pending = {oid: phone for oid, phone in ordered_list}
    received_set = set()
    start_time = time.time()
    active_bulk_data[user_id] = {"chat_id": chat_id, "pending": pending, "pid": pid,
                                  "price": price_per_unit, "batch_id": batch_id}

    async def update_summary():
        try:
            if active_bulk_data.get(user_id, {}).get("batch_id") != batch_id: return
            remaining = max(0, TIMEOUT_SECONDS - int(time.time() - start_time))
            teks = build_bulk_summary(ordered_list, received_set, price_per_unit, remaining)
            kb = []
            if len(pending) > 0:
                kb.append([InlineKeyboardButton(f"🔄 Ganti Semua ({len(pending)})",
                                                callback_data=f"replaceall_{pid}_{price_per_unit}")])
            await context.bot.edit_message_text(chat_id=chat_id, message_id=summary_msg_id,
                text=teks, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb) if kb else None)
        except: pass

    try:
        await update_summary()
        while pending and (time.time() - start_time) < TIMEOUT_SECONDS:
            if active_bulk_data.get(user_id, {}).get("batch_id") != batch_id: return
            await asyncio.sleep(3)
            if not pending: break
            data = await http_get(f"{BASE_URL}/orders/active")
            active_map = {str(o.get("id")): o for o in data.get("data", [])} \
                if data.get("success") and isinstance(data.get("data"), list) else {}
            to_remove = []
            for oid, phone in list(pending.items()):
                o = active_map.get(oid)
                if o is None:
                    cek = await http_get(f"{BASE_URL}/orders/{oid}")
                    if cek.get("success"): o = cek["data"]
                    else: continue
                otp = o.get("otp_code"); status = o.get("status")
                if otp and oid not in received_set:
                    received_set.add(oid)
                    db_update_order(oid, "OTP_RECEIVED", otp)
                    try:
                        await asyncio.sleep(0.3)
                        await context.bot.send_message(chat_id=chat_id,
                            text=f"🔑 <b>OTP:</b> <code>{otp}</code>\n📱 <b>Nomor:</b> <code>{phone}</code>",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Selesai", callback_data=f"finish_{oid}")]]),
                            parse_mode='HTML')
                    except RetryAfter as e: await asyncio.sleep(e.retry_after)
                    except: pass
                    to_remove.append(oid)
                elif status in ["CANCELLED", "EXPIRED", "COMPLETED"]:
                    db_update_order(oid, status); to_remove.append(oid)
            for oid in to_remove: pending.pop(oid, None)
            await update_summary()

        try: await context.bot.delete_message(chat_id=chat_id, message_id=summary_msg_id)
        except: pass
        if pending:
            for oid, _ in list(pending.items()): await batal_order_api(oid)
            await send_temp_msg(context, chat_id, "⌛ Waktu Bulk habis, sisa nomor dibatalkan.")
    finally:
        if active_bulk_data.get(user_id, {}).get("batch_id") == batch_id:
            active_bulk_data.pop(user_id, None)

# ==========================================
# USER MENU HANDLERS (KHUSUS ADMIN)
# ==========================================
async def send_premium_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    user = html.escape(update.effective_user.first_name or "Bos")
    _, saldo = await cek_saldo_api()
    maintenance = "\n🔧 <b>MAINTENANCE AKTIF</b>" if get_setting('maintenance') == 'on' else ""
    teks = (f"✨ <b>REIVALDO NOKOS</b> ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👋 Halo, <b>{user}</b>! [👑 ADMIN]{maintenance}\n"
            f"💰 <b>Saldo:</b> Rp {format_rupiah(saldo) if saldo >= 0 else '-'}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <i>Proses OTP super cepat (cek 2 detik)</i>\n"
            f"👇 <b>Silakan pilih menu:</b>")
    if update.message:
        await update.message.reply_text(teks, reply_markup=get_main_keyboard(), parse_mode='HTML')
    elif update.callback_query:
        try: await update.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text=teks,
                                       reply_markup=get_main_keyboard(), parse_mode='HTML')

async def send_beli_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products, err = await get_gojek_products()
    if err: return await send_temp_msg(context, update.effective_chat.id, f"❌ {err}")
    keyboard = []; seen = set()
    for p in products:
        harga = p.get('price', '?'); pid = p.get('id'); stok = p.get('available', 0)
        if harga in seen: continue
        seen.add(harga)
        if stok and stok > 0:
            keyboard.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(harga)} | 📦 {stok}", callback_data=f"buy_{pid}_{harga}")])
        else:
            keyboard.append([InlineKeyboardButton(f"❌ Rp {format_rupiah(harga)} (Habis)", callback_data=f"watch_{pid}_{harga}")])
        if len(keyboard) >= 8: break
    keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')])
    if update.message:
        await update.message.reply_text("🛒 <b>Pilih Harga:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    elif update.callback_query:
        try: await update.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🛒 <b>Pilih Harga:</b>",
                                       reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def send_bulk_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products, err = await get_gojek_products()
    if err: return await send_temp_msg(context, update.effective_chat.id, f"❌ {err}")
    keyboard = []; seen = set()
    for p in products:
        harga = p.get('price', '?'); pid = p.get('id'); stok = p.get('available', 0)
        if harga in seen: continue
        seen.add(harga)
        if stok and stok > 0:
            keyboard.append([InlineKeyboardButton(f"💵 Rp {format_rupiah(harga)} | 📦 {stok}", callback_data=f"bulkprice_{pid}_{harga}")])
        if len(keyboard) >= 8: break
    keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')])
    if update.message:
        await update.message.reply_text("📦 <b>Pilih Harga (Bulk):</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    elif update.callback_query:
        try: await update.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text="📦 <b>Pilih Harga (Bulk):</b>",
                                       reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def msg_beli(update, context):
    if not is_admin(update.effective_user.id): return
    if not check_rate_limit(update.effective_user.id): return
    try: await send_beli_menu(update, context)
    except: await send_temp_msg(context, update.effective_chat.id, "⚠️ Error sistem.")

async def msg_bulk(update, context):
    if not is_admin(update.effective_user.id): return
    if not check_rate_limit(update.effective_user.id): return
    try: await send_bulk_menu(update, context)
    except: await send_temp_msg(context, update.effective_chat.id, "⚠️ Error sistem.")

async def msg_saldo(update, context):
    if not is_admin(update.effective_user.id): return
    if not check_rate_limit(update.effective_user.id): return
    try:
        teks, _ = await cek_saldo_api()
        await update.message.reply_text(teks, parse_mode='HTML')
    except: await send_temp_msg(context, update.effective_chat.id, "⚠️ Error sistem.")

async def msg_riwayat(update, context):
    if not is_admin(update.effective_user.id): return
    if not check_rate_limit(update.effective_user.id): return
    try:
        rows = db_get_history(update.effective_user.id, 15)
        if not rows: return await send_temp_msg(context, update.effective_chat.id, "Belum ada transaksi.")
        teks = "📜 <b>15 Transaksi Terakhir:</b>\n\n"
        for oid, phone, status, otp, created in rows:
            emoji = "✅" if status == "COMPLETED" else ("🔑" if otp else ("❌" if status == "CANCELLED" else "⏳"))
            teks += f"{emoji} <code>{format_phone(phone)}</code> | {status} | {otp or '-'}\n"
        await update.message.reply_text(teks, parse_mode='HTML')
    except: await send_temp_msg(context, update.effective_chat.id, "⚠️ Error sistem.")

async def msg_aktif(update, context):
    if not is_admin(update.effective_user.id): return
    if not check_rate_limit(update.effective_user.id): return
    try:
        user_id = update.effective_user.id
        conn = get_db()
        db_orders = conn.execute(
            "SELECT order_id, phone FROM orders WHERE user_id=? AND status='ACTIVE'", (user_id,)
        ).fetchall()
        conn.close()
        if not db_orders:
            return await send_temp_msg(context, update.effective_chat.id, "📭 Tidak ada order aktif.")
        kb = []
        for oid, phone in db_orders[:15]:
            nomor = format_phone(phone)
            kb.append([InlineKeyboardButton(f"❌ Batalkan {nomor}", callback_data=f"cancel_{oid}")])
        kb.append([InlineKeyboardButton("🗑️ Batalkan Semua", callback_data='cancel_all_active')])
        kb.append([InlineKeyboardButton("🔙 Tutup", callback_data='menu_utama')])
        await update.message.reply_text("📦 <b>Order Aktif:</b>",
                                        reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    except: await send_temp_msg(context, update.effective_chat.id, "⚠️ Error sistem.")

async def msg_bantuan(update, context):
    if not is_admin(update.effective_user.id): return
    try:
        teks = (f"ℹ️ <b>Bantuan</b>\n\n"
                f"Gunakan tombol di bawah layar.\n"
                f"Auto-refund dalam {TIMEOUT_MINUTES} menit jika OTP tidak masuk.\n\n"
                f"🆔 ID Telegram Anda: <code>{update.effective_user.id}</code>")
        await update.message.reply_text(teks, parse_mode='HTML')
    except: pass

async def msg_unknown(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("⚠️ <b>Gunakan tombol di bawah layar.</b>", parse_mode='HTML',
                                    reply_markup=get_main_keyboard())

# ==========================================
# ADMIN HANDLERS
# ==========================================
async def cmd_broadcast(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        return await update.message.reply_text("❌ Format: `/broadcast <pesan>`", parse_mode='Markdown')
    msg = " ".join(context.args)
    try:
        await context.bot.send_message(int(ADMIN_ID), f"📢 <b>Broadcast:</b>\n\n{html.escape(msg)}", parse_mode='HTML')
        await update.message.reply_text("✅ Pesan broadcast terkirim ke diri sendiri (fitur multi-user telah dihapus).")
    except: pass

async def cmd_maintenance(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args or context.args[0].lower() not in ('on', 'off'):
        status = get_setting('maintenance', 'off')
        return await update.message.reply_text(
            f"🔧 Status saat ini: <b>{status.upper()}</b>\n\n"
            f"Gunakan: `/maintenance on` atau `/maintenance off`",
            parse_mode='Markdown')
    state = context.args[0].lower()
    set_setting('maintenance', state)
    icon = "🔧" if state == 'on' else "✅"
    await update.message.reply_text(f"{icon} Maintenance mode: <b>{state.upper()}</b>", parse_mode='HTML')

async def cmd_admin(update, context):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Khusus admin.")
    total_orders, active_orders = db_bot_stats()
    _, saldo = await cek_saldo_api()
    maint = get_setting('maintenance', 'off').upper()
    uptime = int(time.time() - START_TIME)
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    teks = (f"👑 <b>PANEL ADMIN</b> 👑\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Total Order: <b>{total_orders}</b>\n"
            f"🟢 Order Aktif: <b>{active_orders}</b>\n"
            f"💰 Saldo: Rp {format_rupiah(saldo) if saldo >= 0 else '-'}\n"
            f"🔧 Maintenance: <b>{maint}</b>\n"
            f"⏱️ Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}\n"
            f"🤖 Versi: {BOT_VERSION}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Perintah:</b>\n"
            f"/broadcast &lt;msg&gt; · /maintenance on|off")
    await update.message.reply_text(teks, parse_mode='HTML', reply_markup=get_admin_keyboard())

# ==========================================
# CALLBACK HANDLER
# ==========================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        return await query.answer("🚫 Akses ditolak. Bot ini khusus Admin.", show_alert=True)

    data = query.data or ""
    if not data:
        try: await query.answer("Invalid", show_alert=True)
        except: pass
        return

    try:
        if data.startswith(('cancel_', 'finish_', 'replace_', 'buy_', 'bulk', 'watch_', 'menu_')):
            try: await query.answer()
            except: pass

        is_transaction = data.startswith(('bulkqty_', 'buy_', 'replaceall_', 'replace_'))
        if is_transaction:
            if not try_acquire_lock(user_id):
                return await query.answer("⏳ Tunggu, transaksi sebelumnya sedang diproses.", show_alert=True)

        if data == 'menu_utama':
            await send_premium_menu(update, context)
        elif data == 'menu_beli':
            await send_beli_menu(update, context)
        elif data == 'menu_bulk':
            await send_bulk_menu(update, context)

        elif data == 'cancel_all_active':
            if user_id in active_bulk_data:
                active_bulk_data[user_id]["batch_id"] = "CANCELLED"
                active_bulk_data.pop(user_id, None)
            await query.edit_message_text("⏳ <b>Membatalkan order aktif...</b>", parse_mode='HTML')
            await asyncio.sleep(1.5)
            conn = get_db()
            db_orders = conn.execute(
                "SELECT order_id FROM orders WHERE user_id=? AND status='ACTIVE'", (user_id,)
            ).fetchall()
            conn.close()
            to_cancel = [str(o[0]) for o in db_orders]
            if not to_cancel:
                return await query.edit_message_text("✅ Tidak ada order aktif.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data='menu_utama')]]))
            tasks = [batal_order_api(oid) for oid in to_cancel]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            success = sum(1 for r in results if isinstance(r, dict) and r.get('success'))
            await query.edit_message_text(
                f"✅ <b>Batalkan Massal Selesai</b>\n\n"
                f"Total dibatalkan: <b>{success}</b> nomor.\n"
                f"Saldo telah dikembalikan.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Utama", callback_data='menu_utama')]]),
                parse_mode='HTML')

        elif data.startswith('bulkprice_'):
            pid = int(data.replace('bulkprice_', '', 1).split('_')[0])
            harga = int(data.replace('bulkprice_', '', 1).split('_')[1])
            _, saldo = await cek_saldo_api()
            teks = (f"📦 <b>Beli Banyak — Rp {format_rupiah(harga)}</b>\n"
                    f"💰 Saldo: <b>Rp {format_rupiah(saldo) if saldo >= 0 else '?'}</b>\n\n"
                    f"Pilih jumlah:")
            
            # === PENAMBAHAN 30 & 40 NOMOR ===
            kb = [
                [InlineKeyboardButton("📦 5 Nomor", callback_data=f"bulkqty_{pid}_{harga}_5"),
                 InlineKeyboardButton("📦 10 Nomor", callback_data=f"bulkqty_{pid}_{harga}_10")],
                [InlineKeyboardButton("📦 20 Nomor", callback_data=f"bulkqty_{pid}_{harga}_20"),
                 InlineKeyboardButton("📦 30 Nomor", callback_data=f"bulkqty_{pid}_{harga}_30")],
                [InlineKeyboardButton("📦 40 Nomor", callback_data=f"bulkqty_{pid}_{harga}_40"),
                 InlineKeyboardButton("📦 50 Nomor", callback_data=f"bulkqty_{pid}_{harga}_50")],
                [InlineKeyboardButton("📦 100 Nomor", callback_data=f"bulkqty_{pid}_{harga}_100")],
                [InlineKeyboardButton("🔙 Kembali", callback_data='menu_bulk')]
            ]
            await query.edit_message_text(teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

        elif data.startswith('bulkqty_'):
            body = data.replace('bulkqty_', '', 1)
            parts = body.split('_')
            if len(parts) != 3: return await query.answer("Data error.", show_alert=True)
            pid, harga, qty = int(parts[0]), int(parts[1]), int(parts[2])
            try: await query.message.delete()
            except: pass
            temp = await context.bot.send_message(query.message.chat_id,
                f"⏳ Membeli <b>{qty} nomor</b>...\n\n<i>Mohon tunggu...</i>", parse_mode='HTML')
            results, failed_count, err = await beli_banyak_api_async(pid, user_id, harga, qty)
            try: await temp.delete()
            except: pass
            if results:
                ordered_list = [(oid, format_phone(n)) for oid, n in results]
                summary = build_bulk_summary(ordered_list, set(), harga, TIMEOUT_SECONDS)
                msg = await context.bot.send_message(query.message.chat_id, summary, parse_mode='HTML')
                batch_id = f"{user_id}_{int(time.time())}"
                asyncio.create_task(bulk_monitor_task(context, query.message.chat_id, user_id,
                                                      batch_id, ordered_list, harga, msg.message_id, pid))
            else:
                await send_temp_msg(context, query.message.chat_id, f"❌ {err}")

        elif data.startswith('watch_'):
            await query.answer("🔔 Produk dipantau!", show_alert=True)
            try: await query.message.delete()
            except: pass

        elif data.startswith('buy_'):
            body = data.replace('buy_', '', 1)
            parts = body.split('_')
            if len(parts) < 2: return await query.answer("Data error.", show_alert=True)
            pid, harga = int(parts[0]), int(parts[1])
            try: await query.message.delete()
            except: pass
            temp = await context.bot.send_message(query.message.chat_id, "⏳ Sedang membeli...", parse_mode='HTML')
            order_id, nomor_raw = await beli_nomor_api(pid, user_id, harga)
            try: await temp.delete()
            except: pass
            if order_id:
                nomor_display = format_phone(nomor_raw)
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id,
                                                  nomor_display, pid, harga))
            else:
                await send_temp_msg(context, query.message.chat_id, f"❌ {nomor_raw}")

        elif data.startswith('replaceall_'):
            body = data.replace('replaceall_', '', 1)
            parts = body.split('_')
            if len(parts) < 2: return await query.answer("Data error.", show_alert=True)
            pid, harga = int(parts[0]), int(parts[1])
            chat_id = query.message.chat_id
            if user_id not in active_bulk_data or not active_bulk_data[user_id]["pending"]:
                return await query.answer("❌ Tidak ada nomor.", show_alert=True)
            await query.answer("🔄 Membatalkan nomor lama...")
            active_bulk_data[user_id]["batch_id"] = "CANCELLED"
            pending_olds = list(active_bulk_data[user_id]["pending"].keys())
            tasks = [batal_order_api(oid) for oid in pending_olds]
            cancel_results = await asyncio.gather(*tasks, return_exceptions=True)
            success_cancel = sum(1 for r in cancel_results if isinstance(r, dict) and r.get("success"))
            if success_cancel == 0:
                return await send_temp_msg(context, chat_id, "❌ Gagal batal (mungkin OTP sudah masuk).")
            await query.edit_message_text(f"♻️ Batalkan {success_cancel} nomor. Membeli pengganti...", parse_mode='HTML')
            results, failed_count, err = await beli_banyak_api_async(pid, user_id, harga, success_cancel)
            if results:
                ordered_list = [(oid, format_phone(n)) for oid, n in results]
                active_bulk_data.pop(user_id, None)
                summary = build_bulk_summary(ordered_list, set(), harga, TIMEOUT_SECONDS)
                msg = await query.edit_message_text(summary, parse_mode='HTML')
                batch_id = f"{user_id}_{int(time.time())}"
                asyncio.create_task(bulk_monitor_task(context, chat_id, user_id, batch_id,
                                                      ordered_list, harga, msg.message_id, pid))
            else:
                await send_temp_msg(context, chat_id, f"❌ Gagal beli pengganti: {err}")

        elif data.startswith('replace_'):
            body = data.replace('replace_', '', 1)
            parts = body.rsplit('_', 2)
            if len(parts) < 3: return await query.answer("Data error.", show_alert=True)
            old_oid, pid, harga = parts[0], int(parts[1]), int(parts[2])
            await query.answer("🔄 Ganti nomor...")
            res, msg = await batal_order_api(old_oid)
            if not res: return await query.answer(f"❌ Gagal: {str(msg)[:30]}", show_alert=True)
            pending_messages.pop(old_oid, None)
            try: await query.message.delete()
            except: pass
            order_id, nomor_raw = await beli_nomor_api(pid, user_id, harga)
            if order_id:
                nomor_display = format_phone(nomor_raw)
                asyncio.create_task(auto_poll_otp(query.message.chat_id, context, order_id,
                                                  nomor_display, pid, harga))
            else:
                await send_temp_msg(context, query.message.chat_id, f"❌ Gagal: {nomor_raw}")

        elif data.startswith('cancel_'):
            order_id = data.replace('cancel_', '', 1)
            try: await query.message.delete()
            except: pass
            hasil = await batal_order_api(order_id)
            pending_messages.pop(order_id, None)
            if hasil["success"]:
                await query.answer("✅ Dibatalkan", show_alert=True)
            else:
                await query.answer(f"❌ Gagal: {str(hasil['message'])[:30]}", show_alert=True)

        elif data.startswith('finish_'):
            order_id = data.replace('finish_', '', 1)
            try: await query.message.delete()
            except: pass
            hasil = await selesai_order_api(order_id)
            pending_messages.pop(order_id, None)
            if hasil["success"]:
                await query.answer("✅ Selesai & Lepas Nomor", show_alert=True)
            else:
                await query.answer(f"❌ Gagal: {str(hasil['message'])[:30]}", show_alert=True)

    except Exception as e:
        print(traceback.format_exc())
        try: await query.answer("⚠️ Kesalahan sistem.", show_alert=True)
        except: pass
    finally:
        if is_transaction:
            release_lock(user_id)

# ==========================================
# AUTO RECOVERY
# ==========================================
async def recover_pending_orders(application):
    await asyncio.sleep(5)
    try:
        conn = get_db()
        rows = conn.execute("SELECT order_id, user_id, phone, product_id, price FROM orders WHERE status='ACTIVE'").fetchall()
        conn.close()
        if not rows:
            print("ℹ️ Tidak ada order aktif untuk direcover.")
            return
        recovered = 0
        for oid, user_id, phone, pid, price in rows:
            data = await http_get(f"{BASE_URL}/orders/{oid}")
            if data.get("success"):
                od = data["data"]
                if od.get("status") == "ACTIVE":
                    recovered += 1
                    asyncio.create_task(auto_poll_otp(int(user_id), application, str(oid),
                                                      format_phone(phone), pid or 0, price or 0))
                else:
                    db_update_order(oid, od.get("status"))
        if recovered > 0:
            try:
                await application.bot.send_message(
                    chat_id=int(ADMIN_ID),
                    text=f"🔄 <b>Auto-Recovery</b>\n\nBerhasil melanjutkan pantauan OTP untuk <b>{recovered}</b> order aktif.",
                    parse_mode='HTML')
            except: pass
    except Exception as e:
        print(f"Recovery error: {e}")

# ==========================================
# /start & POST_INIT
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Maaf, bot ini bersifat Private (Khusus Admin).")
    await send_premium_menu(update, context)

async def post_init(application):
    try:
        await application.bot.set_my_commands([
            ("start", "Mulai bot"),
            ("admin", "Panel admin"),
            ("help", "Bantuan"),
        ])
    except: pass
    asyncio.create_task(recover_pending_orders(application))

# ==========================================
# MAIN RUNNER
# ==========================================
if __name__ == '__main__':
    if not TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN tidak diatur!")
        exit(1)
    init_db()
    print(f"✅ Bot v{BOT_VERSION} starting...")
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", msg_bantuan))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("maintenance", cmd_maintenance))

    application.add_handler(MessageHandler(filters.Regex(r'^🛒 Beli Nomor$'), msg_beli))
    application.add_handler(MessageHandler(filters.Regex(r'^📦 Beli Banyak \(Bulk\)$'), msg_bulk))
    application.add_handler(MessageHandler(filters.Regex(r'^💰 Cek Saldo$'), msg_saldo))
    application.add_handler(MessageHandler(filters.Regex(r'^📜 Riwayat$'), msg_riwayat))
    application.add_handler(MessageHandler(filters.Regex(r'^📦 Order Aktif$'), msg_aktif))
    application.add_handler(MessageHandler(filters.Regex(r'^ℹ️ Bantuan$'), msg_bantuan))

    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_unknown))

    port = int(os.environ.get("PORT", 8080))
    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url:
        print(f"🚀 Webhook on {port} -> {webhook_url}")
        application.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN,
                                webhook_url=f"{webhook_url}/{TOKEN}")
    else:
        print("⚠️ WEBHOOK_URL tidak diatur, fallback ke polling...")
        application.run_polling()
