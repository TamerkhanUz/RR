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
CHANNEL_USERNAME = "@BSONewsUZ"  # Yangilangan kanal

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
    waiting_for_quality = State()

# Foydalanuvchilarning obuna holati
user_submission_data: Dict[int, Dict] = {}  # {user_id: {"link": "", "quality": ""}}

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

# Sifat tanlash inline tugmalari
def get_quality_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📹 480p", callback_data="quality_480"),
            InlineKeyboardButton(text="🎥 720p", callback_data="quality_720"),
        ],
        [
            InlineKeyboardButton(text="🎬 1080p", callback_data="quality_1080"),
            InlineKeyboardButton(text="⚡ Eng Yuqori", callback_data="quality_best"),
        ],
        [
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data="quality_cancel"),
        ]
    ])

# Optimallashtirilgan MediaDownloader sinfi
class AsyncMediaDownloader:
    def __init__(self, quality: str = "best"):
        self.session = None
        self.quality = quality
        self.set_ydl_options()
    
    def set_ydl_options(self):
        """Sifatga qarab ydl opsiyalarini o'rnatish"""
        base_opts = {
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
            'concurrent_fragment_downloads': 3,
            'http_chunk_size': 10485760,  # 10MB chunks
            'progress_hooks': [self.progress_hook],
        }
        
        # Sifat tanloviga qarab formatni o'rnatish
        if self.quality == "480":
            base_opts['format'] = 'bestvideo[height<=480]+bestaudio/best[height<=480]'
        elif self.quality == "720":
            base_opts['format'] = 'bestvideo[height<=720]+bestaudio/best[height<=720]'
        elif self.quality == "1080":
            base_opts['format'] = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'
        else:  # best
            base_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        
        self.ydl_opts = base_opts
    
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
        """Turli platformalardan media yuklash"""
        try:
            # Cache ni tekshirish
            cache_key = f"{user_id}_{hash(link)}_{self.quality}"
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
            user_opts = self.ydl_opts.copy()
            user_opts['outtmpl'] = f'downloads/{user_id}_%(id)s_{self.quality}p.%(ext)s'
            
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
                download_pictures=True,
                download_videos=True,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False
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
            user_opts['outtmpl'] = f'downloads/{user_id}_tiktok_%(id)s_{self.quality}p.%(ext)s'
            
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
                filename = f"downloads/{user_id}_{timestamp}_{self.quality}p{ext}"
                
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
    
    if not is_subscribed:
        clean_username = CHANNEL_USERNAME.replace('@', '')
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Kanalga a'zo bo'lish", url=f"https://t.me/{clean_username}")],
            [InlineKeyboardButton(text="🔁 Tekshirish", callback_data="check_subscription")]
        ])
        await message.answer(
            f"👋 Salom {message.from_user.first_name}!\n\n"
            f"📢 Botdan foydalanish uchun avval kanalimizga obuna bo'ling:\n"
            f"🔗 {CHANNEL_USERNAME}\n\n"
            "Kanalga obuna bo'lgach, 'Tekshirish' tugmasini bosing.",
            reply_markup=keyboard
        )
    else:
        await message.answer(
            f"✅ Salom {message.from_user.first_name}!\n"
            f"🎉 Siz kanalga obuna bo'lgansiz!\n\n"
            "📥 Endi media linkini yuboring:\n"
            "• YouTube\n• Instagram\n• TikTok\n• Boshqa platformalar\n\n"
            "📎 Faqat link yuboring!",
            parse_mode="HTML"
        )
        await state.set_state(UserStates.waiting_for_link)

# Tekshirish tugmasi
@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    # Obunani tekshirish
    is_subscribed = await check_subscription(user_id)
    
    if is_subscribed:
        await callback.message.edit_text(
            "✅ Kanalga obuna bo'lgansiz!\n\n"
            "📥 Endi media linkini yuboring:\n"
            "• YouTube\n• Instagram\n• TikTok\n• Boshqa platformalar\n\n"
            "📎 Faqat link yuboring!",
            parse_mode="HTML"
        )
        await state.set_state(UserStates.waiting_for_link)
    else:
        clean_username = CHANNEL_USERNAME.replace('@', '')
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Kanalga a'zo bo'lish", url=f"https://t.me/{clean_username}")],
            [InlineKeyboardButton(text="🔁 Tekshirish", callback_data="check_subscription")]
        ])
        await callback.message.edit_text(
            f"❌ Siz hali kanalga obuna bo'lmagansiz!\n\n"
            f"📢 Kanal: {CHANNEL_USERNAME}\n\n"
            "Kanalga obuna bo'lgach, 'Tekshirish' tugmasini bosing.",
            reply_markup=keyboard
        )
        await callback.answer("❌ Siz hali kanalga obuna bo'lmagansiz!", show_alert=True)

# Media linkini qabul qilish
@router.message(UserStates.waiting_for_link)
async def handle_media_link(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Obunani tekshirish
    is_subscribed = await check_subscription(user_id)
    
    if not is_subscribed:
        clean_username = CHANNEL_USERNAME.replace('@', '')
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Kanalga a'zo bo'lish", url=f"https://t.me/{clean_username}")],
            [InlineKeyboardButton(text="🔁 Tekshirish", callback_data="check_subscription")]
        ])
        await message.answer(
            f"❌ Botdan foydalanish uchun kanalga obuna bo'lishingiz kerak!\n\n"
            f"📢 Kanal: {CHANNEL_USERNAME}",
            reply_markup=keyboard
        )
        return
    
    # Linkni tekshirish
    link = message.text
    if not link or 'http' not in link:
        await message.answer("❗ Iltimos, to'g'ri media linkini yuboring!\n\nMasalan: https://www.youtube.com/watch?v=...")
        return
    
    # Linkni saqlash va sifat tanlashni so'rash
    user_submission_data[user_id] = {"link": link}
    
    # Sifat tanlash inline tugmalarini yuborish
    await message.answer(
        "📊 *Medianing sifatini tanlang* 👇\n\n"
        "📹 480p - Past sifat (kichik hajm)\n"
        "🎥 720p - O'rta sifat\n"
        "🎬 1080p - Yuqori sifat\n"
        "⚡ Eng Yuqori - Maksimal sifat",
        parse_mode="Markdown",
        reply_markup=get_quality_keyboard()
    )
    
    await state.set_state(UserStates.waiting_for_quality)

# Sifat tanlash callback handlerlari
@router.callback_query(F.data.startswith("quality_"))
async def handle_quality_selection(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    # Foydalanuvchi ma'lumotlarini tekshirish
    if user_id not in user_submission_data or "link" not in user_submission_data[user_id]:
        await callback.answer("❌ Xatolik: Link topilmadi!", show_alert=True)
        await callback.message.delete()
        await state.set_state(UserStates.waiting_for_link)
        return
    
    # Sifatni olish
    quality_data = callback.data.split("_")[1]
    
    if quality_data == "cancel":
        await callback.message.delete()
        await callback.message.answer("❌ Yuklash bekor qilindi!\n\nYangi link yuboring:")
        await state.set_state(UserStates.waiting_for_link)
        user_submission_data.pop(user_id, None)
        return
    
    # Sifat nomlarini belgilash
    quality_names = {
        "480": "480p (SD)",
        "720": "720p (HD)",
        "1080": "1080p (Full HD)",
        "best": "Eng Yuqori Sifat"
    }
    
    quality_name = quality_names.get(quality_data, "Eng Yuqori Sifat")
    
    # Sifatni saqlash
    user_submission_data[user_id]["quality"] = quality_data
    
    # Foydalanuvchiga xabar
    await callback.message.edit_text(
        f"✅ *{quality_name}* tanlandi!\n\n"
        f"⏳ Media yuklanmoqda...\n"
        f"📥 Bu biroz vaqt olishi mumkin. Iltimos kuting! ⏱️",
        parse_mode="Markdown"
    )
    
    # Media yuklashni boshlash
    asyncio.create_task(
        download_and_send_media(
            user_id,
            user_submission_data[user_id]["link"],
            quality_data,
            callback.message,
            state
        )
    )

# Media yuklash va yuborish funksiyasi
async def download_and_send_media(user_id: int, link: str, quality: str, message: Message, state: FSMContext):
    try:
        # Yuklashni boshlash
        async with AsyncMediaDownloader(quality=quality) as downloader:
            file_path = await downloader.download_media(link, user_id)
            
            if file_path and os.path.exists(file_path):
                # Fayl hajmini tekshirish
                file_size = os.path.getsize(file_path)
                if file_size > MAX_FILE_SIZE:
                    await message.edit_text("❌ Fayl hajmi juda katta!")
                    os.remove(file_path)
                    return
                
                # Reklama matni
                caption = (
                    "∧,,,,,∧    ~    ┏━━━━━━━━━━━━━━┓\n"
                    "(  • · •  )   👉 @userMediaBOT ♦️\n"
                    "/       づ  ~   ┗━━━━━━━━━━━━━┛\n\n"
                    "✅ Media muvaffaqiyatli yuklandi!"
                )
                
                # InputFile yaratish
                input_file = InputFile(file_path)
                
                # Fayl turini aniqlash va yuborish
                try:
                    if file_path.endswith(('.mp4', '.avi', '.mov', '.mkv', '.webm')):
                        await message.answer_video(
                            video=input_file,
                            caption=caption
                        )
                    elif file_path.endswith(('.mp3', '.m4a', '.wav', '.ogg')):
                        await message.answer_audio(
                            audio=input_file,
                            caption=caption
                        )
                    elif file_path.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                        await message.answer_photo(
                            photo=input_file,
                            caption=caption
                        )
                    else:
                        await message.answer_document(
                            document=input_file,
                            caption=caption
                        )
                    
                    # Muvaffaqiyatli yuklash xabari
                    quality_names = {
                        "480": "480p",
                        "720": "720p",
                        "1080": "1080p",
                        "best": "Eng Yuqori"
                    }
                    
                    quality_name = quality_names.get(quality, "Eng Yuqori")
                    
                    await message.edit_text(
                        f"✅ Media muvaffaqiyatli yuklandi! ({quality_name})\n\n"
                        f"📎 Yangi link yuboring yoki /start ni bosing."
                    )
                    
                    # Cache ga saqlash
                    cache_key = f"{user_id}_{hash(link)}_{quality}"
                    DOWNLOAD_CACHE[cache_key] = file_path
                    
                except Exception as send_error:
                    logger.error(f"Media yuborish xatosi: {send_error}")
                    await message.edit_text("❌ Media yuborishda xatolik yuz berdi!")
                
                # Faylni keyinroq o'chirish
                asyncio.create_task(cleanup_file(file_path))
                
            else:
                await message.edit_text(
                    "❌ Media yuklab olinmadi!\n"
                    "Iltimos, quyidagilarni tekshiring:\n"
                    "1. Link to'g'ri ekanligi\n"
                    "2. Internet ulanishi\n"
                    "3. Boshqa link yuborib ko'ring\n\n"
                    "📎 Yangi link yuboring:"
                )
                await state.set_state(UserStates.waiting_for_link)
    
    except Exception as e:
        logger.error(f"Yuklash jarayonida xatolik: {e}", exc_info=True)
        await message.edit_text("❌ Xatolik yuz berdi! Iltimos, qayta urinib ko'ring.")
    
    finally:
        # Ma'lumotlarni tozalash
        user_submission_data.pop(user_id, None)

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

# Help komandasi
@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "🤖 *Media Downloader Bot - Yordam*\n\n"
        "🔹 *Buyruqlar:*\n"
        "/start - Botni ishga tushirish\n"
        "/help - Yordam olish\n\n"
        "🔹 *Qo'llanma:*\n"
        "1. Botni ishga tushiring (/start)\n"
        "2. Kanalga obuna bo'ling\n"
        "3. Media linkini yuboring\n"
        "4. Sifatni tanlang (480p/720p/1080p)\n"
        "5. Yuklangan faylni oling\n\n"
        "🔹 *Qo'llab-quvvatlanadigan platformalar:*\n"
        "• YouTube\n• Instagram\n• TikTok\n• Boshqa saytlar\n\n"
        "⚠️ *Eslatma:* Ba'zi platformalar yuklashni cheklashi mumkin."
    )
    
    await message.answer(help_text, parse_mode="Markdown")

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
        logger.warning("Bot kanalga admin qilinganligiga ishonch hosil qiling!")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    print("🚀 Media Downloader Bot ishga tushmoqda...")
    print(f"📢 Majburiy obuna kanali: {CHANNEL_USERNAME}")
    print(f"🤖 Bot: @UserMediaBot")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⛔ Bot to'xtatildi.")
