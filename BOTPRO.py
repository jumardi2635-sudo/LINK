# -*- coding: utf-8 -*-
import asyncio
import threading
import os
import time
import random
import glob
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, session, flash

from telethon import TelegramClient, events, Button
from telethon.errors import (
    SessionPasswordNeededError, FloodWaitError, PhoneNumberInvalidError,
    UserPrivacyRestrictedError, UserAlreadyParticipantError, PeerFloodError
)
from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently, InputPeerChannel, InputPeerUser
from telethon.tl.functions.channels import JoinChannelRequest, InviteToChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.users import GetFullUserRequest

# --- KONFIGURASI ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(BASE_DIR, 'sessions')
SCRAPE_DIR = os.path.join(BASE_DIR, 'scraped_data')

for folder in [SESSION_DIR, SCRAPE_DIR]:
    if not os.path.exists(folder): os.makedirs(folder)

USER_SESSION = os.path.join(SESSION_DIR, 'user_session')
BOT_SESSION = os.path.join(SESSION_DIR, 'bot_session')

API_ID = 31266539
API_HASH = '789ff283ac1198d83da1cbec30e883d6'
BOT_TOKEN = '8379629732:AAFYfuQl3IFArWETZ6vT5Vi7lMTJGktBOp0'
ADMIN_IDS = {8079515800}

# Global Variables
TARGET_GROUP_BOT = "" 
MULTI_CLIENTS = []   
IS_PROCESS_RUNNING = False
LOGIN_STATES = {} 

INVITE_CONFIG = {
    "min": 10,
    "max": 20
}

app = Flask(__name__)
app.secret_key = 'super_secret_key_pro_v5'
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# --- FITUR SESSION PERMANENT & AUTO CLEANUP ---
def cleanup_corrupt_sessions():
    journals = glob.glob(os.path.join(SESSION_DIR, "*.session-journal"))
    for j in journals:
        try: os.remove(j)
        except: pass

    sessions = glob.glob(os.path.join(SESSION_DIR, "*.session"))
    for s in sessions:
        try:
            conn = sqlite3.connect(s, timeout=10)
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA integrity_check;")
            res = cursor.fetchone()
            conn.close()
            if res[0] != "ok": raise sqlite3.DatabaseError("Corrupted")
        except:
            try: os.remove(s)
            except: pass

def create_permanent_client(session_path):
    return TelegramClient(
        session_path, API_ID, API_HASH, loop=loop,
        connection_retries=None, retry_delay=5, auto_reconnect=True, receive_updates=True
    )

def load_multi_sessions():
    global MULTI_CLIENTS
    MULTI_CLIENTS = []
    cleanup_corrupt_sessions()
    session_files = glob.glob(os.path.join(SESSION_DIR, "*.session"))
    valid_sessions = [f for f in session_files if 'bot_session' not in f and 'user_session' not in f]
    if os.path.exists(USER_SESSION + ".session"): valid_sessions.append(USER_SESSION + ".session")
    for sess in valid_sessions:
        cl = create_permanent_client(sess.replace(".session", ""))
        MULTI_CLIENTS.append(cl)

def is_admin(user_id): return user_id in ADMIN_IDS

def send_log(msg):
    async def _send():
        for admin in ADMIN_IDS:
            try: await bot.send_message(admin, f"🤖 **LOG:**\n{msg}")
            except: pass
    asyncio.run_coroutine_threadsafe(_send(), loop)

def is_user_active(user):
    if isinstance(user.status, UserStatusOnline): return True
    if isinstance(user.status, UserStatusRecently): return True
    if isinstance(user.status, UserStatusOffline):
        now = datetime.now(timezone.utc)
        was_online = user.status.was_online
        if was_online and (now - was_online).days <= 3: return True
    return False

# --- FITUR AUTO RESOLVE BERULANG (STRICT) ---
async def strict_recursive_resolve(client, user_str, retries=2):
    """Memaksa sesi mengenali user dengan GetFullUserRequest sebelum invite"""
    for i in range(retries):
        try:
            # Langkah 1: Get Entity dasar
            user_entity = await client.get_entity(user_str)
            # Langkah 2: Paksa sinkronisasi cache dengan GetFullUser
            await client(GetFullUserRequest(user_entity))
            # Langkah 3: Kembalikan InputPeer
            return await client.get_input_entity(user_entity)
        except Exception as e:
            if i < retries - 1: await asyncio.sleep(2)
            continue
    return None

async def force_join_retry(client, entity, client_idx):
    max_retries = 3
    for i in range(max_retries):
        try:
            await client(JoinChannelRequest(entity))
            return True
        except UserAlreadyParticipantError: return True
        except:
            await asyncio.sleep(2)
    return False

# --- LOGIKA ROTASI & INVITE (DELAY OPTIMIZED) ---
async def process_inviting(target_group, source_file="members.txt"):
    global IS_PROCESS_RUNNING
    if IS_PROCESS_RUNNING: return
    IS_PROCESS_RUNNING = True
    
    path = os.path.join(SCRAPE_DIR, source_file)
    if not os.path.exists(path): return 
    
    with open(path, "r") as f:
        users = [line.strip() for line in f.readlines() if line.strip()]

    client_limits = {}
    client_counters = {}
    active_indices = []

    for idx, cl in enumerate(MULTI_CLIENTS):
        limit_random = random.randint(INVITE_CONFIG['min'], INVITE_CONFIG['max'])
        client_limits[idx] = limit_random
        client_counters[idx] = 0
        active_indices.append(idx)

    send_log(f"🚀 **MULAI INVITE (STRICT MODE)**\nTarget: `{target_group}`")

    current_pool_index = 0 
    success_count = 0

    for user_str in users:
        if not IS_PROCESS_RUNNING or not active_indices: break
        
        # LOGIKA GANTI AKUN
        prev_client_idx = active_indices[current_pool_index]
        current_pool_index = (current_pool_index + 1) % len(active_indices)
        real_client_idx = active_indices[current_pool_index]
        current_client = MULTI_CLIENTS[real_client_idx]

        # DELAY GANTI AKUN (8 Detik jika akun berbeda)
        if prev_client_idx != real_client_idx:
            await asyncio.sleep(8)

        if client_counters[real_client_idx] >= client_limits[real_client_idx]:
            active_indices.pop(current_pool_index)
            if not active_indices: break
            continue

        try:
            if not current_client.is_connected(): await current_client.connect()
            if not await current_client.is_user_authorized():
                active_indices.pop(current_pool_index)
                continue

            # Join Target
            try: entity = await current_client.get_entity(target_group)
            except:
                try:
                    if "+" in target_group:
                        await current_client(ImportChatInviteRequest(target_group.split('+')[1]))
                    entity = await current_client.get_entity(target_group)
                    await force_join_retry(current_client, entity, real_client_idx)
                except:
                    active_indices.pop(current_pool_index)
                    continue

            # AUTO RESOLVE BERULANG
            user_to_add = await strict_recursive_resolve(current_client, user_str)
            if not user_to_add: continue

            # EXECUTE INVITE
            await current_client(InviteToChannelRequest(entity, [user_to_add]))
            success_count += 1
            client_counters[real_client_idx] += 1
            
            if success_count % 5 == 0:
                send_log(f"✅ **PROGRES**\nSukses: `{success_count}`\nAkun Aktif: `{len(active_indices)}`")

            # DELAY INVITE (15 Detik)
            await asyncio.sleep(15)

        except FloodWaitError as e:
            active_indices.pop(current_pool_index) 
        except Exception:
            await asyncio.sleep(2)

    IS_PROCESS_RUNNING = False
    send_log(f"🏁 **SELESAI**\nTotal Berhasil: `{success_count}`")

# --- BOT ADMIN HANDLERS ---
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if not is_admin(event.sender_id): return
    load_multi_sessions()
    buttons = [
        [Button.inline("➕ Tambah Akun", b"add_account_wizard"), Button.inline("🗑️ Hapus Sesi", b"menu_delete_session")],
        [Button.inline(f"⚙️ Limit: {INVITE_CONFIG['min']}-{INVITE_CONFIG['max']}", b"set_limit")],
        [Button.inline("🚀 Mulai Invite", b"start_rotate"), Button.inline("🛑 Stop", b"stop_process")],
        [Button.inline("🕵️ Scrape Menu", b"menu_scrape"), Button.inline("📂 Cek Database", b"cek_db")],
        [Button.inline("⚙️ Set Target", b"set_target")]
    ]
    await event.reply(f"🔥 **PANEL PRO PERMANENT**\n\nAkun Terdeteksi: `{len(MULTI_CLIENTS)}`", buttons=buttons)

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    if not is_admin(event.sender_id): return
    global TARGET_GROUP_BOT, IS_PROCESS_RUNNING, INVITE_CONFIG, LOGIN_STATES
    data = event.data
    sender_id = event.sender_id

    if data == b"menu_delete_session":
        files = glob.glob(os.path.join(SESSION_DIR, "*.session"))
        valid_files = [os.path.basename(f) for f in files if 'bot_session' not in f and 'user_session' not in f]
        if not valid_files: return await event.answer("⚠️ Kosong", alert=True)
        buttons = [[Button.inline(f"❌ {f}", f"del_{f}".encode())] for f in valid_files]
        buttons.append([Button.inline("⚠️ HAPUS SEMUA ⚠️", b"confirm_delete_all")])
        buttons.append([Button.inline("🔙 Kembali", b"back_home")])
        await event.edit("🛠️ **HAPUS SESI**", buttons=buttons)

    elif data.startswith(b"del_"):
        filename = data.decode().split("del_")[1]
        try:
            os.remove(os.path.join(SESSION_DIR, filename))
            await event.answer("✅ Dihapus")
            load_multi_sessions()
            return await callback_handler(event.edit(data=b"menu_delete_session"))
        except: pass

    elif data == b"confirm_delete_all":
        await event.edit("🚨 **HAPUS SEMUA?**", buttons=[[Button.inline("✅ YA", b"do_delete_all_sessions")], [Button.inline("❌ TIDAK", b"menu_delete_session")]])

    elif data == b"do_delete_all_sessions":
        files = glob.glob(os.path.join(SESSION_DIR, "*.session"))
        for f in files:
            if 'bot_session' not in f and 'user_session' not in f:
                try: os.remove(f)
                except: pass
        load_multi_sessions()
        await start_handler(event)

    elif data == b"add_account_wizard":
        LOGIN_STATES[sender_id] = {'step': 'wait_phone', 'client': None}
        await event.edit("📞 **NOMOR (+62):**", buttons=[Button.inline("🔙 Batal", b"cancel_login")])
    
    elif data == b"cancel_login":
        if sender_id in LOGIN_STATES:
            cl = LOGIN_STATES[sender_id].get('client')
            if cl: await cl.disconnect()
            del LOGIN_STATES[sender_id]
        await start_handler(event) 

    elif data == b"set_limit":
        async with bot.conversation(event.sender_id) as conv:
            await conv.send_message("🔢 Masukkan Limit (Min-Max):")
            res = await conv.get_response()
            try:
                mn, mx = map(int, res.text.strip().split('-'))
                INVITE_CONFIG.update({'min': mn, 'max': mx})
                await conv.send_message("✅ Tersimpan!")
            except: await conv.send_message("❌ Error")

    elif data == b"menu_scrape":
        await event.edit("🕵️ **SCRAPE**", buttons=[[Button.inline("Public", b"scrape_public"), Button.inline("Private", b"scrape_private")], [Button.inline("🔙 Utama", b"back_home")]])

    elif data == b"scrape_public" or data == b"scrape_private":
        mode = "Private" if data == b"scrape_private" else "Public"
        async with bot.conversation(sender_id) as conv:
            await conv.send_message(f"🔗 Link {mode}:")
            res = await conv.get_response()
            source = res.text.strip()
            if not client_web.is_connected(): await client_web.connect()
            try:
                if mode == "Private":
                    try: await client_web(ImportChatInviteRequest(source.split('+')[1]))
                    except: pass
                entity = await client_web.get_entity(source)
                users = []
                async for u in client_web.iter_participants(entity, aggressive=True):
                    if not u.bot and not u.deleted and is_user_active(u):
                        users.append(u.username or f"+{u.phone}")
                with open(os.path.join(SCRAPE_DIR, "members.txt"), "w") as f:
                    f.write("\n".join([str(u) for u in users if u]))
                await conv.send_message(f"✅ Dapat {len(users)} user.")
            except Exception as e: await conv.send_message(f"❌ Error: {e}")

    elif data == b"set_target":
        async with bot.conversation(sender_id) as conv:
            await conv.send_message("🎯 Link Grup Tujuan:")
            res = await conv.get_response()
            TARGET_GROUP_BOT = res.text.strip()
            await conv.send_message("✅ Diset.")

    elif data == b"start_rotate":
        if not TARGET_GROUP_BOT: return await event.answer("Set Target!", alert=True)
        asyncio.create_task(process_inviting(TARGET_GROUP_BOT))
        await event.edit("🚀 Berjalan...")

    elif data == b"stop_process":
        IS_PROCESS_RUNNING = False
        await event.answer("🛑 Stop!", alert=True)

    elif data == b"cek_db":
        path = os.path.join(SCRAPE_DIR, "members.txt")
        c = len(open(path).readlines()) if os.path.exists(path) else 0
        await event.answer(f"DB: {c} user", alert=True)

    elif data == b"back_home":
        await start_handler(event)

# --- MESSAGE HANDLER WIZARD LOGIN ---
@bot.on(events.NewMessage)
async def wizard_input_handler(event):
    sender_id = event.sender_id
    if sender_id not in LOGIN_STATES or not is_admin(sender_id): return
    text = event.text.strip()
    if text.lower() == 'batal':
        cl = LOGIN_STATES[sender_id].get('client')
        if cl: await cl.disconnect()
        del LOGIN_STATES[sender_id]
        return await event.reply("❌ Batal.")

    state = LOGIN_STATES[sender_id]
    step = state['step']
    try:
        if step == 'wait_phone':
            phone = text.replace(" ", "").replace("-", "")
            session_name = f"user_{phone.replace('+', '')}"
            new_client = create_permanent_client(os.path.join(SESSION_DIR, session_name))
            await new_client.connect()
            sent = await new_client.send_code_request(phone)
            LOGIN_STATES[sender_id] = {'step': 'wait_otp', 'client': new_client, 'phone': phone, 'hash': sent.phone_code_hash}
            await event.reply(f"✅ OTP Terkirim. Masukkan kode:")
        elif step == 'wait_otp':
            cl = state['client']
            await cl.sign_in(phone=state['phone'], code=text, phone_code_hash=state['hash'])
            await cl.disconnect()
            del LOGIN_STATES[sender_id]
            load_multi_sessions()
            await event.reply("✅ Login Berhasil!")
    except SessionPasswordNeededError:
        LOGIN_STATES[sender_id]['step'] = 'wait_pwd'
        await event.reply("🔒 Password 2FA:")
    except Exception as e: await event.reply(f"❌ Error: {e}")

# --- RUNNER ---
def start_bot_loop():
    asyncio.set_event_loop(loop)
    cleanup_corrupt_sessions()
    load_multi_sessions()
    async def main_runner():
        await bot.start(bot_token=BOT_TOKEN)
        print("✅ Bot Admin Aktif & Permanen...")
        await bot.run_until_disconnected()
    loop.run_until_complete(main_runner())

if __name__ == '__main__':
    t = threading.Thread(target=start_bot_loop, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=5003, debug=False, use_reloader=False)