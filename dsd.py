import os
import logging
import asyncio
import aiohttp
from typing import Optional, Dict
from datetime import datetime
from pathlib import Path
import tempfile
import json

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, FSInputFile
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import yt_dlp
from urllib.parse import urlparse

# ==================== KONFIGURATSIYA ====================
API_TOKEN = "8273548883:AAFbWzOMIaKZbSe8KJNijfYiLKfDpn9i-Bs"
CHANNEL_USERNAME = "@BSONewsUZ"

# COOKIES fayli - YouTube dan blokdan chiqish uchun
COOKIES_FILE = "cookies.txt"  # Browserdan export qilingan cookies fayli

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('media_bot_fixed.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== BOT SOZLASH ====================
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ==================== HOLATLAR ====================
class UserStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_quality = State()

# ==================== COOKIES YARATISH ====================
def create_cookies_file():
    """Agar cookies fayli bo'lmasa, yaratish"""
    if not os.path.exists(COOKIES_FILE):
        try:
            # Oddiy cookies strukturasini yaratish
            cookies_data = [
                {
                    "domain": ".youtube.com",
                    "httpOnly": False,
                    "name": "PREF",
                    "path": "/",
                    "secure": False,
                    "value": "f1=50000000"
                }
            ]
            
            with open(COOKIES_FILE, 'w') as f:
                json.dump(cookies_data, f)
            
            logger.info(f"Cookies fayli yaratildi: {COOKIES_FILE}")
        except Exception as e:
            logger.error(f"Cookies fayl yaratish xatosi: {e}")

# Cookies faylini yaratish
create_cookies_file()

# ==================== YUKLASH VA YUBORISH KLASSI ====================
class SmartMediaDownloader:
    """Aqlli media yuklovchi - cookies va user agent bilan"""
    
    def __init__(self):
        self.timeout = 90  # 1.5 daqiqa
        self.max_file_size = 1.9 * 1024 * 1024 * 1024  # 1.9GB
        self.cookies_file = COOKIES_FILE if os.path.exists(COOKIES_FILE) else None
        
    def get_ydl_options(self, quality: str, url: str) -> dict:
        """Aqlli yt-dlp opsiyalari - platformaga qarab"""
        
        # Platformani aniqlash
        domain = urlparse(url).netloc.lower()
        
        # Sifat uchun format
        quality_map = {
            '360': 'bestvideo[height<=360]+bestaudio/best[height<=360]',
            '480': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
            '720': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
            '1080': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
            'best': 'best',
            'audio': 'bestaudio',
        }
        
        format_str = quality_map.get(quality, 'best')
        
        # Asosiy opsiyalar
        opts = {
            'quiet': True,
            'no_warnings': False,
            'format': format_str,
            'outtmpl': '%(title).100s.%(ext)s',
            'merge_output_format': 'mp4',
            'retries': 15,
            'fragment_retries': 15,
            'file_access_retries': 10,
            'ignoreerrors': False,
            'no_color': True,
            'restrictfilenames': True,
            'socket_timeout': 30,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'web'],
                    'skip': ['hls', 'dash'],
                }
            },
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'referer': 'https://www.youtube.com/',
            'cookiefile': self.cookies_file,
            'extract_flat': False,
            'progress_hooks': [self.progress_hook],
            'postprocessor_args': ['-threads', '4'],
        }
        
        # Agar cookies fayli bo'lsa
        if self.cookies_file and os.path.exists(self.cookies_file):
            opts['cookiefile'] = self.cookies_file
            logger.info(f"Cookies fayli ishlatilmoqda: {self.cookies_file}")
        else:
            # Agar cookies bo'lmasa, user-agent va referer bilan ishlash
            opts['http_headers'] = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://www.youtube.com/',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate',
            }
        
        # Instagram uchun maxsus sozlamalar
        if 'instagram.com' in domain:
            opts['extractor_args'] = {'instagram': {'shortcode': True}}
            opts['referer'] = 'https://www.instagram.com/'
        
        # TikTok uchun
        if 'tiktok.com' in domain:
            opts['referer'] = 'https://www.tiktok.com/'
            opts['http_headers']['User-Agent'] = 'Mozilla/5.0 (Linux; Android 10; SM-G981B) AppleWebKit/537.36'
        
        return opts
    
    def progress_hook(self, d):
        """Progressni log qilish"""
        if d['status'] == 'downloading':
            try:
                percent = d.get('_percent_str', '0%').strip()
                speed = d.get('_speed_str', 'N/A')
                eta = d.get('_eta_str', 'N/A')
                logger.info(f"Yuklanmoqda: {percent} | Tezlik: {speed} | Qolgan: {eta}")
            except:
                pass
    
    async def download_media(self, url: str, quality: str, progress_msg: Message = None) -> Optional[str]:
        """Media yuklash - cookies va user agent bilan"""
        temp_file = None
        try:
            ydl_opts = self.get_ydl_options(quality, url)
            
            # Temp file uchun yangi nom
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"downloads/media_{timestamp}"
            ydl_opts['outtmpl'] = filename + '.%(ext)s'
            
            if progress_msg:
                await progress_msg.edit_text(f"📥 Video yuklanmoqda...\n\n🔗 {urlparse(url).netloc}\n⚡ {quality}p")
            
            logger.info(f"Yuklash boshlanmoqda: {url}")
            logger.info(f"Cookies fayli: {self.cookies_file}")
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Video ma'lumotlarini olish
                info = ydl.extract_info(url, download=False)
                
                if not info:
                    if progress_msg:
                        await progress_msg.edit_text("❌ Video topilmadi yoki bloklangan!")
                    return None
                
                # Video hajmini tekshirish
                filesize = info.get('filesize') or info.get('filesize_approx')
                if filesize and filesize > self.max_file_size:
                    if progress_msg:
                        await progress_msg.edit_text(f"❌ Video juda katta ({filesize//(1024*1024)} MB)!")
                    return None
                
                # Yuklashni boshlash
                ydl.download([url])
                
                # Yuklangan faylni topish
                downloaded_file = ydl.prepare_filename(info)
                
                # Fayl mavjudligini tekshirish
                if os.path.exists(downloaded_file):
                    logger.info(f"✅ Video yuklandi: {downloaded_file}")
                    return downloaded_file
                else:
                    # Boshqa kengaytmalarda qidirish
                    possible_files = []
                    for ext in ['.mp4', '.mkv', '.webm', '.avi', '.mov', '.m4a', '.mp3']:
                        alt_file = downloaded_file.replace('.mp4', ext)
                        if os.path.exists(alt_file):
                            possible_files.append(alt_file)
                    
                    if possible_files:
                        logger.info(f"✅ Alternativ fayl topildi: {possible_files[0]}")
                        return possible_files[0]
                
                return None
                
        except yt_dlp.utils.DownloadError as e:
            logger.error(f"Yuklash xatosi: {e}")
            
            # Agar cookies bilan muammo bo'lsa, cookies siz urinib ko'rish
            if "cookies" in str(e).lower() and self.cookies_file:
                logger.warning("Cookies bilan muammo. Cookies siz urinib ko'ramiz...")
                if progress_msg:
                    await progress_msg.edit_text("🔄 Cookies bilan muammo. Alternativ usul ishlatilmoqda...")
                
                # Cookies siz yuklash
                return await self.download_without_cookies(url, quality, progress_msg)
            
            error_msg = str(e)
            if progress_msg:
                if "Sign in" in error_msg:
                    await progress_msg.edit_text("❌ YouTube bloklagan. Iltimos, boshqa video urinib ko'ring.")
                else:
                    await progress_msg.edit_text(f"❌ Yuklash xatosi: {error_msg[:100]}")
            
            return None
        except Exception as e:
            logger.error(f"Kutilmagan xatolik: {e}", exc_info=True)
            if progress_msg:
                await progress_msg.edit_text("❌ Server xatosi. Keyinroq urinib ko'ring.")
            return None
    
    async def download_without_cookies(self, url: str, quality: str, progress_msg: Message = None) -> Optional[str]:
        """Cookies siz yuklash - alternativ usul"""
        try:
            # Cookies siz oddiy opsiyalar
            simple_opts = {
                'quiet': True,
                'no_warnings': True,
                'format': 'best[height<=480]',  # Past sifat
                'outtmpl': 'downloads/simple_%(title).50s.%(ext)s',
                'merge_output_format': 'mp4',
                'retries': 5,
                'socket_timeout': 20,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'referer': 'https://www.google.com/',
                'extractor_args': {'youtube': {'player_client': 'android'}},
            }
            
            if progress_msg:
                await progress_msg.edit_text("🔄 Alternativ usul bilan yuklanmoqda...")
            
            with yt_dlp.YoutubeDL(simple_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                if not info:
                    return None
                
                ydl.download([url])
                downloaded_file = ydl.prepare_filename(info)
                
                if os.path.exists(downloaded_file):
                    return downloaded_file
            
            return None
            
        except Exception as e:
            logger.error(f"Alternativ yuklash xatosi: {e}")
            return None
    
    async def send_to_user(self, file_path: str, message: Message) -> bool:
        """Faylni foydalanuvchiga yuborish"""
        try:
            if not file_path or not os.path.exists(file_path):
                return False
            
            file_size = os.path.getsize(file_path)
            file_size_mb = file_size / (1024 * 1024)
            
            # Fayl nomini qisqartirish
            filename = os.path.basename(file_path)
            if len(filename) > 50:
                filename = filename[:50] + "..."
            
            logger.info(f"Yuborilmoqda: {filename} ({file_size_mb:.1f} MB)")
            
            # FSInputFile yaratish
            input_file = FSInputFile(file_path)
            
            # Video yuborish
            await message.answer_video(
                video=input_file,
                caption=f"🎬 {filename}\n\n"
                       f"📦 Hajmi: {file_size_mb:.1f} MB\n"
                       f"✅ @MediaDownloaderUZ_Bot",
                supports_streaming=True
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Yuborish xatosi: {e}", exc_info=True)
            return False
    
    async def cleanup(self, file_path: str):
        """Faylni o'chirish"""
        try:
            if file_path and os.path.exists(file_path):
                await asyncio.sleep(30)  # 30 soniya kutish
                os.remove(file_path)
                logger.info(f"Fayl o'chirildi: {file_path}")
        except Exception as e:
            logger.error(f"Fayl o'chirish xatosi: {e}")

# ==================== BOT HANDLERLARI ====================
downloader = SmartMediaDownloader()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await message.answer(
        "🎬 *Smart Media Downloader*\n\n"
        "✅ Cookies bilan ishlaydi\n"
        "⚡ Ko'p platforma qo'llab-quvvatlaydi\n"
        "🛡️ Blokdan himoyalangan\n\n"
        "🔗 *Video linkini yuboring:*",
        parse_mode="Markdown"
    )
    await state.set_state(UserStates.waiting_for_link)

def get_quality_buttons():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📹 360p", callback_data="quality_360"),
            InlineKeyboardButton(text="🎥 480p", callback_data="quality_480"),
        ],
        [
            InlineKeyboardButton(text="📺 720p (HD)", callback_data="quality_720"),
            InlineKeyboardButton(text="🎬 1080p", callback_data="quality_1080"),
        ],
        [
            InlineKeyboardButton(text="⚡ Eng yaxshi", callback_data="quality_best"),
            InlineKeyboardButton(text="🎵 Audio", callback_data="quality_audio"),
        ],
        [
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel"),
        ]
    ])

@router.message(UserStates.waiting_for_link)
async def handle_link(message: Message, state: FSMContext):
    link = message.text.strip()
    
    if not link or 'http' not in link:
        await message.answer("❗ Iltimos, to'g'ri video linkini yuboring!")
        return
    
    await state.update_data(link=link)
    
    await message.answer(
        f"✅ *Link qabul qilindi!*\n\n"
        f"🌐 *Platforma:* {urlparse(link).netloc}\n\n"
        f"📊 *Video sifatini tanlang:*",
        parse_mode="Markdown",
        reply_markup=get_quality_buttons()
    )
    
    await state.set_state(UserStates.waiting_for_quality)

@router.callback_query(F.data.startswith("quality_"))
async def handle_quality(callback: CallbackQuery, state: FSMContext):
    quality = callback.data.split("_")[1]
    
    if quality == "cancel":
        await callback.message.edit_text("❌ Yuklash bekor qilindi!\nYangi link yuboring:")
        await state.set_state(UserStates.waiting_for_link)
        return
    
    data = await state.get_data()
    link = data.get('link')
    
    if not link:
        await callback.answer("❌ Link topilmadi!", show_alert=True)
        return
    
    # Sifat nomlari
    quality_names = {
        '360': '360p',
        '480': '480p',
        '720': '720p (HD)',
        '1080': '1080p (FHD)',
        'best': 'Eng yaxshi',
        'audio': 'Audio (MP3)',
    }
    
    quality_name = quality_names.get(quality, '480p')
    
    # Progress xabari
    progress_msg = await callback.message.edit_text(
        f"✅ *{quality_name}* tanlandi!\n\n"
        f"⏳ *Yuklanmoqda...*\n\n"
        f"🔄 Platforma: {urlparse(link).netloc}\n"
        f"⚡ Sifat: {quality_name}\n"
        f"📊 Iltimos kuting...",
        parse_mode="Markdown"
    )
    
    # Video yuklash
    video_file = await downloader.download_media(
        url=link,
        quality=quality,
        progress_msg=progress_msg
    )
    
    if video_file:
        # Video yuborish
        await progress_msg.edit_text("✅ *Video yuklandi!*\n\n📤 Telegramga yuborilmoqda...", parse_mode="Markdown")
        
        send_success = await downloader.send_to_user(video_file, callback.message)
        
        if send_success:
            await progress_msg.edit_text(
                "🎉 *Video muvaffaqiyatli yuborildi!*\n\n"
                "🔗 Yangi video linkini yuboring:",
                parse_mode="Markdown"
            )
        else:
            await progress_msg.edit_text(
                "❌ *Video yuborish muvaffaqiyatsiz!*\n\n"
                "Iltimos, qayta urinib ko'ring:",
                parse_mode="Markdown"
            )
        
        # Faylni tozalash
        asyncio.create_task(downloader.cleanup(video_file))
    else:
        await progress_msg.edit_text(
            "❌ *Video yuklab bo'lmadi!*\n\n"
            "🔍 *Sabablar:*\n"
            "• Video bloklangan\n"
            "• Link noto'g'ri\n"
            "• Platforma qo'llab-quvvatlanmaydi\n"
            "• Internet muammosi\n\n"
            "🔄 Boshqa video linkini yuboring:",
            parse_mode="Markdown"
        )
    
    await state.set_state(UserStates.waiting_for_link)

# ==================== BOTNI ISHGA TUSHIRISH ====================
async def on_startup():
    """Bot ishga tushganda"""
    # Papkalarni yaratish
    os.makedirs('downloads', exist_ok=True)
    
    bot_info = await bot.get_me()
    logger.info(f"🎬 Smart Media Bot ishga tushdi: @{bot_info.username}")
    
    print("=" * 60)
    print("🎬 SMART MEDIA DOWNLOADER BOT")
    print("=" * 60)
    print(f"🤖 Bot: @{bot_info.username}")
    print(f"⚡ Version: 3.0 (Cookies bilan)")
    print(f"🛡️ Holat: Cookies {'FAOL' if os.path.exists(COOKIES_FILE) else 'O\'CHIRILGAN'}")
    print(f"📁 Downloads: {os.path.abspath('downloads')}")
    print("=" * 60)
    
    # Agar cookies bo'lmasa, ogohlantirish
    if not os.path.exists(COOKIES_FILE):
        print("⚠️  Diqqat: Cookies fayli topilmadi!")
        print("📋 YouTube blokdan saqlanish uchun cookies yarating:")
        print("1. yt-dlp --cookies-from-browser chrome")
        print("2. yt-dlp --cookies-from-browser firefox")
        print("=" * 60)

async def main():
    """Asosiy funksiya"""
    dp.startup.register(on_startup)
    
    try:
        await dp.start_polling(bot, skip_updates=True)
    except KeyboardInterrupt:
        print("\n\n🛑 Bot to'xtatildi.")
    except Exception as e:
        logger.error(f"Xatolik: {e}", exc_info=True)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())
