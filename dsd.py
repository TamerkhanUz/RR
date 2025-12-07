import os
import logging
import asyncio
import aiohttp
import aiofiles
from typing import Optional, Dict, Union, BinaryIO
from datetime import datetime
from pathlib import Path
import tempfile
import subprocess

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import yt_dlp
import requests
import instaloader
import re

# Konfiguratsiya
API_TOKEN = "8273548883:AAFbWzOMIaKZbSe8KJNijfYiLKfDpn9i-Bs"
CHANNEL_USERNAME = "@BSOmediaBOT"

# Logging sozlash
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Bot va Dispatcher yaratish
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Foydalanuvchi holatlari
class UserStates(StatesGroup):
    waiting_for_link = State()

# Foydalanuvchilarning obuna holati
user_subscription_status: Dict[int, bool] = {}

# Cache va optimallashtirish
DOWNLOAD_CACHE = {}
DOWNLOAD_TIMEOUT = 300  # 5 minut
MAX_FILE_SIZE = 1900 * 1024 * 1024  # 1.9GB (Telegram limiti 2GB)

# FFmpeg mavjudligini tekshirish
def check_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        logger.info("FFmpeg topildi")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.warning("FFmpeg topilmadi. Video formatlarni qayta ishlash cheklangan.")
        return False

HAS_FFMPEG = check_ffmpeg()

# Obunani tekshirish
async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Obunani tekshirish xatosi: {e}")
        return False

# Optimallashtirilgan MediaDownloader sinfi
class AsyncMediaDownloader:
    def __init__(self):
        self.session = None
        self.ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': 'downloads/%(title).50s.%(ext)s',
            'quiet': False,
            'no_warnings': False,
            'extract_flat': False,
            'fragment_retries': 10,
            'retries': 10,
            'file_access_retries': 5,
            'ignoreerrors': False,
            'no_color': True,
            'restrictfilenames': True,
            'merge_output_format': 'mp4',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }] if HAS_FFMPEG else [],
            'concurrent_fragment_downloads': 5,  # Parallel yuklash
            'http_chunk_size': 10485760,  # 10MB chunks
            'progress_hooks': [self.progress_hook],
        }
    
    async def progress_hook(self, d):
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '0%').strip()
            speed = d.get('_speed_str', 'N/A')
            eta = d.get('_eta_str', 'N/A')
            logger.debug(f"Downloading: {percent} at {speed}, ETA: {eta}")
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def download_media(self, link: str, user_id: int) -> Optional[str]:
        """Turli platformalardan media yuklash (async versiya)"""
        try:
            # Cache ni tekshirish
            cache_key = f"{user_id}_{hash(link)}"
            if cache_key in DOWNLOAD_CACHE:
                cached_file = DOWNLOAD_CACHE[cache_key]
                if os.path.exists(cached_file):
                    logger.info(f"Cache'dan fayl topildi: {cached_file}")
                    return cached_file
            
            if 'youtube.com' in link or 'youtu.be' in link:
                return await self.download_youtube(link, user_id)
            elif 'instagram.com' in link:
                return await self.download_instagram(link, user_id)
            elif 'tiktok.com' in link:
                return await self.download_tiktok(link, user_id)
            else:
                return await self.download_generic(link, user_id)
                
        except Exception as e:
            logger.error(f"Yuklashda xatolik: {e}", exc_info=True)
            return None
    
    async def download_youtube(self, link: str, user_id: int) -> Optional[str]:
        """YouTube dan video yuklash"""
        try:
            # Optimallashtirilgan ydl_opts
            user_opts = self.ydl_opts.copy()
            user_opts['outtmpl'] = f'downloads/{user_id}_%(id)s.%(ext)s'
            
            # Streaming formatlarni cheklash
            user_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
            
            with yt_dlp.YoutubeDL(user_opts) as ydl:
                # Video ma'lumotlarini olish
                info = ydl.extract_info(link, download=False)
                
                # Video hajmini tekshirish
                filesize = info.get('filesize') or info.get('filesize_approx')
                if filesize and filesize > MAX_FILE_SIZE:
                    logger.warning(f"Video hajmi juda katta: {filesize}")
                    return None
                
                # Yuklash
                ydl.download([link])
                
                # Faylni topish
                filename = ydl.prepare_filename(info)
                
                # FFmpeg bilan optimallashtirish
                if HAS_FFMPEG and os.path.exists(filename):
                    await self.optimize_video(filename)
                
                return filename if os.path.exists(filename) else None
                
        except Exception as e:
            logger.error(f"YouTube yuklash xatosi: {e}", exc_info=True)
            return None
    
    async def download_instagram(self, link: str, user_id: int) -> Optional[str]:
        """Instagram dan media yuklash"""
        try:
            L = instaloader.Instaloader(
                dirname_pattern='downloads',
                filename_pattern=f'{user_id}_%(date_utc)s',
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                download_pictures=True,
                download_videos=True,
                download_video_thumbnails=False,
            )
            
            shortcode = link.strip('/').split('/')[-1]
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            L.download_post(post, target='downloads')
            
            # Faylni topish
            pattern = f"{user_id}_*"
            for file in Path('downloads').glob(pattern):
                if file.suffix in ['.mp4', '.jpg', '.jpeg', '.png']:
                    return str(file)
                    
        except Exception as e:
            logger.error(f"Instagram yuklash xatosi: {e}", exc_info=True)
        
        return None
    
    async def download_tiktok(self, link: str, user_id: int) -> Optional[str]:
        """TikTok dan video yuklash"""
        try:
            user_opts = self.ydl_opts.copy()
            user_opts['outtmpl'] = f'downloads/{user_id}_tiktok_%(id)s.%(ext)s'
            user_opts['format'] = 'best[ext=mp4]/best'
            
            with yt_dlp.YoutubeDL(user_opts) as ydl:
                info = ydl.extract_info(link, download=False)
                
                # Video hajmini tekshirish
                filesize = info.get('filesize') or info.get('filesize_approx')
                if filesize and filesize > MAX_FILE_SIZE:
                    logger.warning(f"TikTok video hajmi juda katta: {filesize}")
                    return None
                
                ydl.download([link])
                filename = ydl.prepare_filename(info)
                
                return filename if os.path.exists(filename) else None
                
        except Exception as e:
            logger.error(f"TikTok yuklash xatosi: {e}", exc_info=True)
            return None
    
    async def download_generic(self, link: str, user_id: int) -> Optional[str]:
        """Boshqa saytlardan media yuklash"""
        try:
            if not self.session:
                self.session = aiohttp.ClientSession()
            
            async with self.session.get(link, timeout=aiohttp.ClientTimeout(total=300)) as response:
                if response.status != 200:
                    return None
                
                # Content type aniqlash
                content_type = response.headers.get('Content-Type', '')
                ext = self.get_extension_from_content_type(content_type)
                
                # Fayl nomi
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                filename = f"downloads/{user_id}_{timestamp}{ext}"
                
                # Yuklash
                async with aiofiles.open(filename, 'wb') as f:
                    total_size = 0
                    async for chunk in response.content.iter_chunked(8192):
                        if chunk:
                            await f.write(chunk)
                            total_size += len(chunk)
                            
                            # Fayl hajmini tekshirish
                            if total_size > MAX_FILE_SIZE:
                                await f.close()
                                os.remove(filename)
                                logger.warning("Fayl hajmi limitdan oshdi")
                                return None
                
                return filename
                
        except Exception as e:
            logger.error(f"Generic yuklash xatosi: {e}", exc_info=True)
            return None
    
    def get_extension_from_content_type(self, content_type: str) -> str:
        """Content-Type dan fayl kengaytmasini aniqlash"""
        content_type = content_type.lower()
        
        if 'video/mp4' in content_type:
            return '.mp4'
        elif 'video/webm' in content_type:
            return '.webm'
        elif 'video/x-matroska' in content_type:
            return '.mkv'
        elif 'image/jpeg' in content_type:
            return '.jpg'
        elif 'image/png' in content_type:
            return '.png'
        elif 'image/gif' in content_type:
            return '.gif'
        elif 'audio/mpeg' in content_type:
            return '.mp3'
        elif 'audio/mp4' in content_type:
            return '.m4a'
        else:
            return '.mp4'  # default
    
    async def optimize_video(self, filepath: str) -> None:
        """FFmpeg bilan videoni optimallashtirish"""
        try:
            if not HAS_FFMPEG:
                return
            
            temp_file = f"{filepath}.optimized.mp4"
            
            # Video sifati va hajmini optimallashtirish
            cmd = [
                'ffmpeg', '-i', filepath,
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-movflags', '+faststart',
                '-y', temp_file
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0 and os.path.exists(temp_file):
                # Asl faylni optimallashtirilgan fayl bilan almashtirish
                os.replace(temp_file, filepath)
                logger.info(f"Video optimallashtirildi: {filepath}")
            else:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                logger.warning(f"Video optimallashtirish muvaffaqiyatsiz: {stderr.decode()}")
                
        except Exception as e:
            logger.error(f"Optimizatsiya xatosi: {e}")

# Start komandasi
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Obunani tekshirish
    is_subscribed = await check_subscription(user_id)
    user_subscription_status[user_id] = is_subscribed
    
    if not is_subscribed:
        clean_username = CHANNEL_USERNAME.replace('@', '')
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Kanalga a'zo bo'lish", url=f"https://t.me/{clean_username}")],
            [InlineKeyboardButton(text="🔁 Tekshirish", callback_data="check_subscription")]
        ])
        await message.answer("Botdan foydalanish uchun avval kanalga obuna bo'ling.", reply_markup=keyboard)
    else:
        await message.answer("✅ Kanalga obuna bo'lgansiz!\n\nEndi media linkini yuboring.")
        await state.set_state(UserStates.waiting_for_link)

# Tekshirish tugmasi
@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    is_subscribed = await check_subscription(user_id)
    user_subscription_status[user_id] = is_subscribed
    
    if is_subscribed:
        await callback.message.edit_text("✅ Kanalga obuna bo'lgansiz!\n\nEndi media linkini yuboring.")
        await state.set_state(UserStates.waiting_for_link)
    else:
        await callback.answer("❌ Hali kanalga obuna bo'lmagansiz!", show_alert=True)

# Media linkini qabul qilish
@router.message(UserStates.waiting_for_link)
async def handle_media_link(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Obunani tekshirish
    if not user_subscription_status.get(user_id, False):
        is_subscribed = await check_subscription(user_id)
        if not is_subscribed:
            await message.answer("Avval kanalga obuna bo'ling!")
            return
    
    # Linkni tekshirish
    link = message.text
    if not link or 'http' not in link:
        await message.answer("Iltimos, to'g'ri media linkini yuboring!")
        return
    
    # Yuklashni boshlash
    processing_msg = await message.answer("⏳ Media yuklanmoqda...\nBu bir necha daqiqa vaqt olishi mumkin. ⏱️")
    
    try:
        async with AsyncMediaDownloader() as downloader:
            # Media yuklash
            file_path = await downloader.download_media(link, user_id)
            
            if file_path and os.path.exists(file_path):
                # Fayl hajmini tekshirish
                file_size = os.path.getsize(file_path)
                if file_size > MAX_FILE_SIZE:
                    await message.answer("❌ Fayl hajmi juda katta!")
                    os.remove(file_path)
                    return
                
                # InputFile yaratish
                caption = (
                    "∧,,,,,∧    ~   ┏━━━━━━━━━┓\n"
                    "(  • · •  )   👉  @userMediaBot ♦️\n"
                    "/       づ  ~    ┗━━━━━━━━━┛"
                )
                
                # InputFile dan foydalanish
                input_file = InputFile(file_path)
                
                # Fayl turini aniqlash va yuborish
                if file_path.endswith(('.mp4', '.avi', '.mov', '.mkv', '.webm')):
                    await message.answer_video(video=input_file, caption=caption)
                elif file_path.endswith(('.mp3', '.m4a', '.wav', '.ogg')):
                    await message.answer_audio(audio=input_file, caption=caption)
                elif file_path.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                    await message.answer_photo(photo=input_file, caption=caption)
                else:
                    await message.answer_document(document=input_file, caption=caption)
                
                # Cache ga saqlash
                cache_key = f"{user_id}_{hash(link)}"
                DOWNLOAD_CACHE[cache_key] = file_path
                
                # Faylni keyinroq o'chirish
                asyncio.create_task(cleanup_file(file_path))
                
            else:
                await message.answer("❌ Media yuklab olinmadi. Linkni tekshiring!")
    
    except Exception as e:
        logger.error(f"Xatolik: {e}", exc_info=True)
        await message.answer("❌ Xatolik yuz berdi. Qayta urinib ko'ring!")
    
    finally:
        try:
            await processing_msg.delete()
        except:
            pass

# Faylni tozalash
async def cleanup_file(file_path: str, delay: int = 60):
    """Faylni kechiktirilgan holda o'chirish"""
    await asyncio.sleep(delay)
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Fayl o'chirildi: {file_path}")
    except Exception as e:
        logger.error(f"Fayl o'chirishda xatolik: {e}")

# Downloads papkasini yaratish
if not os.path.exists('downloads'):
    os.makedirs('downloads')

# Botni ishga tushirish
async def main():
    # Bot haqida ma'lumot
    bot_info = await bot.get_me()
    logger.info(f"🤖 Bot ishga tushdi: @{bot_info.username}")
    
    # Kanalni tekshirish
    try:
        chat = await bot.get_chat(CHANNEL_USERNAME)
        logger.info(f"📢 Kanal: {chat.title} (@{chat.username})")
    except Exception as e:
        logger.error(f"⚠️ Kanalni topishda xatolik: {e}")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    print("🚀 UserMedia Bot ishga tushmoqda...")
    print(f"📢 Majburiy obuna kanali: {CHANNEL_USERNAME}")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⛔ Bot to'xtatildi.")