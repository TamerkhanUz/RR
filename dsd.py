import os
import logging
import asyncio
import aiohttp
from typing import Optional, Dict
from datetime import datetime
from pathlib import Path
import tempfile

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile, FSInputFile
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import yt_dlp
from urllib.parse import urlparse

# ==================== KONFIGURATSIYA ====================
API_TOKEN = "8273548883:AAFbWzOMIaKZbSe8KJNijfYiLKfDpn9i-Bs"  # <-- TOKENNI TEKSHIRING!
CHANNEL_USERNAME = "@BSONewsUZ"

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('media_bot.log', encoding='utf-8'),
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

# ==================== YUKLASH VA YUBORISH KLASSI ====================
class MediaDownloader:
    """Media yuklash va yuborish"""
    
    def __init__(self):
        self.session = None
        self.timeout = 60  # 1 daqiqa
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    def get_ydl_options(self, quality: str) -> dict:
        """Yt-dlp opsiyalarini olish"""
        quality_map = {
            '360': 'bestvideo[height<=360]+bestaudio/best[height<=360]',
            '480': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
            '720': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
            '1080': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
            'best': 'bestvideo+bestaudio/best',
            'audio': 'bestaudio/best',
        }
        
        return {
            'quiet': False,
            'no_warnings': False,
            'format': quality_map.get(quality, 'best'),
            'outtmpl': '%(title)s.%(ext)s',
            'merge_output_format': 'mp4',
            'retries': 10,
            'fragment_retries': 10,
            'ignoreerrors': False,
            'no_color': True,
            'restrictfilenames': True,
            'progress_hooks': [self.progress_hook],
        }
    
    def progress_hook(self, d):
        """Progressni log qilish"""
        if d['status'] == 'downloading':
            try:
                percent = d.get('_percent_str', '0%').strip()
                speed = d.get('_speed_str', 'N/A')
                eta = d.get('_eta_str', 'N/A')
                logger.info(f"Download: {percent} at {speed}, ETA: {eta}")
            except:
                pass
        elif d['status'] == 'finished':
            logger.info("Yuklash tugadi")
    
    async def download_video(self, url: str, quality: str, progress_msg: Message = None) -> Optional[str]:
        """Video yuklash - ishonchli usul"""
        temp_file = None
        try:
            ydl_opts = self.get_ydl_options(quality)
            
            # Temp file uchun yangi nom
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            temp_file = tempfile.mktemp(suffix='.mp4', prefix=f'media_{timestamp}_')
            ydl_opts['outtmpl'] = temp_file.replace('.mp4', '.%(ext)s')
            
            if progress_msg:
                await progress_msg.edit_text(f"📥 Video yuklanmoqda (sifat: {quality}p)...\n\n⏳ Iltimos kuting...")
            
            logger.info(f"Yuklash boshlanmoqda: {url}")
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Video ma'lumotlarini olish
                info = ydl.extract_info(url, download=False)
                
                if not info:
                    if progress_msg:
                        await progress_msg.edit_text("❌ Video topilmadi!")
                    return None
                
                # Video hajmini tekshirish
                filesize = info.get('filesize') or info.get('filesize_approx')
                if filesize and filesize > 1.9 * 1024 * 1024 * 1024:  # 1.9GB
                    if progress_msg:
                        await progress_msg.edit_text("❌ Video juda katta (1.9GB limit)!")
                    return None
                
                # Yuklashni boshlash
                ydl.download([url])
                
                # Yuklangan faylni topish
                downloaded_file = ydl.prepare_filename(info)
                
                # Fayl mavjudligini tekshirish
                if os.path.exists(downloaded_file):
                    logger.info(f"Video yuklandi: {downloaded_file}")
                    return downloaded_file
                else:
                    # Boshqa kengaytmalarda qidirish
                    for ext in ['.mp4', '.mkv', '.webm', '.avi', '.mov']:
                        alt_file = downloaded_file.replace('.mp4', ext).replace('.mkv', ext).replace('.webm', ext)
                        if os.path.exists(alt_file):
                            logger.info(f"Alternativ fayl topildi: {alt_file}")
                            return alt_file
                
                return None
                
        except yt_dlp.utils.DownloadError as e:
            logger.error(f"Yuklash xatosi: {e}")
            if progress_msg:
                await progress_msg.edit_text(f"❌ Yuklash xatosi: {str(e)[:100]}")
            return None
        except Exception as e:
            logger.error(f"Kutilmagan xatolik: {e}", exc_info=True)
            if progress_msg:
                await progress_msg.edit_text("❌ Kutilmagan xatolik yuz berdi!")
            return None
    
    async def send_video_to_user(self, video_path: str, original_url: str, message: Message) -> bool:
        """Video faylini foydalanuvchiga yuborish"""
        try:
            if not video_path or not os.path.exists(video_path):
                return False
            
            file_size = os.path.getsize(video_path)
            file_size_mb = file_size / (1024 * 1024)
            
            logger.info(f"Video yuborilmoqda: {video_path} ({file_size_mb:.1f} MB)")
            
            # Video ma'lumotlarini olish
            title = os.path.basename(video_path)
            if len(title) > 50:
                title = title[:50] + "..."
            
            # FSInputFile yaratish
            input_file = FSInputFile(video_path)
            
            # Video yuborish
            await message.answer_video(
                video=input_file,
                caption=f"🎬 {title}\n\n"
                       f"📦 Hajmi: {file_size_mb:.1f} MB\n"
                       f"✅ @MediaDownloaderUZ_Bot",
                supports_streaming=True,
                width=1280,
                height=720
            )
            
            logger.info("Video muvaffaqiyatli yuborildi")
            return True
            
        except Exception as e:
            logger.error(f"Video yuborish xatosi: {e}", exc_info=True)
            return False
    
    async def cleanup_file(self, file_path: str):
        """Faylni o'chirish"""
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Fayl o'chirildi: {file_path}")
        except Exception as e:
            logger.error(f"Fayl o'chirish xatosi: {e}")

# ==================== BOT HANDLERLARI ====================
media_downloader = MediaDownloader()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await message.answer(
        "🎬 *Media Downloader Bot*\n\n"
        "📥 Videolarni turli platformalardan yuklash\n"
        "⚡ Tezkor va ishonchli\n"
        "🎯 Turli sifatlar: 360p, 480p, 720p, 1080p\n\n"
        "🔗 *Video linkini yuboring:*\n"
        "• YouTube\n• Instagram\n• TikTok\n• Facebook\n• Boshqa saytlar",
        parse_mode="Markdown"
    )
    await state.set_state(UserStates.waiting_for_link)

@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "🤖 *Media Downloader Bot - Yordam*\n\n"
        "🔹 *Qanday ishlatish:*\n"
        "1. /start - Botni ishga tushirish\n"
        "2. Video linkini yuboring\n"
        "3. Sifatni tanlang\n"
        "4. Videoni oling\n\n"
        "🔹 *Qo'llab-quvvatlanadigan saytlar:*\n"
        "• YouTube (eng yaxshi)\n"
        "• Instagram (reels, posts)\n"
        "• TikTok\n"
        "• Facebook\n"
        "• Ko'pgina boshqa saytlar\n\n"
        "🔹 *Maslahatlar:*\n"
        "• Youtube uchun 720p tavsiya etiladi\n"
        "• Tezkor yuklash uchun 480p tanlang\n"
        "• Agar video yuklanmasa, boshqa link urinib ko'ring\n\n"
        "⚠️ *Eslatma:* Ba'zi videolar bloklangan bo'lishi mumkin"
    )
    
    await message.answer(help_text, parse_mode="Markdown")

def get_quality_keyboard():
    """Sifat tanlash tugmalari"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📹 360p", callback_data="quality_360"),
            InlineKeyboardButton(text="🎥 480p", callback_data="quality_480"),
        ],
        [
            InlineKeyboardButton(text="📺 720p (HD)", callback_data="quality_720"),
            InlineKeyboardButton(text="🎬 1080p (FHD)", callback_data="quality_1080"),
        ],
        [
            InlineKeyboardButton(text="⚡ Eng yaxshi", callback_data="quality_best"),
            InlineKeyboardButton(text="🎵 Faqat audio", callback_data="quality_audio"),
        ],
        [
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data="quality_cancel"),
        ]
    ])

@router.message(UserStates.waiting_for_link)
async def handle_link(message: Message, state: FSMContext):
    """Video linkini qabul qilish"""
    link = message.text.strip()
    
    # Linkni tekshirish
    if not link or 'http' not in link:
        await message.answer(
            "❗ *Iltimos, to'g'ri video linkini yuboring!*\n\n"
            "Masalan:\n"
            "• https://www.youtube.com/watch?v=...\n"
            "• https://youtu.be/...\n"
            "• https://www.instagram.com/reel/...\n"
            "• https://www.tiktok.com/@.../video/...",
            parse_mode="Markdown"
        )
        return
    
    # Linkni saqlash
    await state.update_data(link=link)
    
    # Platformani aniqlash
    domain = urlparse(link).netloc.lower()
    platform = "noma'lum"
    
    if 'youtube' in domain:
        platform = "YouTube"
    elif 'instagram' in domain:
        platform = "Instagram"
    elif 'tiktok' in domain:
        platform = "TikTok"
    elif 'facebook' in domain:
        platform = "Facebook"
    
    await message.answer(
        f"✅ *{platform} linki qabul qilindi!*\n\n"
        f"📊 *Video sifatini tanlang:*\n\n"
        f"💡 *Tavsiyalar:*\n"
        f"• 480p - Tezkor yuklash\n"
        f"• 720p - Yaxshi sifat (tavsiya etiladi)\n"
        f"• 1080p - Yuqori sifat\n"
        f"• Eng yaxshi - Maksimal sifat",
        parse_mode="Markdown",
        reply_markup=get_quality_keyboard()
    )
    
    await state.set_state(UserStates.waiting_for_quality)

@router.callback_query(F.data.startswith("quality_"))
async def handle_quality_selection(callback: CallbackQuery, state: FSMContext):
    """Sifat tanlash"""
    user_id = callback.from_user.id
    quality = callback.data.split("_")[1]
    
    if quality == "cancel":
        await callback.message.delete()
        await callback.message.answer(
            "❌ Yuklash bekor qilindi!\n\n"
            "Yangi video linkini yuboring:"
        )
        await state.set_state(UserStates.waiting_for_link)
        return
    
    # Foydalanuvchi ma'lumotlarini olish
    data = await state.get_data()
    link = data.get('link')
    
    if not link:
        await callback.answer("❌ Link topilmadi!", show_alert=True)
        await state.set_state(UserStates.waiting_for_link)
        return
    
    # Sifat nomini aniqlash
    quality_names = {
        '360': '360p',
        '480': '480p',
        '720': '720p (HD)',
        '1080': '1080p (Full HD)',
        'best': 'Eng yaxshi sifat',
        'audio': 'Faqat audio (MP3)',
    }
    
    quality_name = quality_names.get(quality, '480p')
    
    # Progress xabarini yangilash
    progress_msg = await callback.message.edit_text(
        f"✅ *{quality_name}* tanlandi!\n\n"
        f"⏳ *Video yuklanmoqda...*\n\n"
        f"📊 Platforma: {urlparse(link).netloc}\n"
        f"⚡ Sifat: {quality_name}\n"
        f"🔄 Iltimos, sabr qiling...",
        parse_mode="Markdown"
    )
    
    # Video yuklash va yuborish
    success = await download_and_send_video(
        link=link,
        quality=quality,
        message=callback.message,
        progress_msg=progress_msg,
        state=state
    )
    
    if not success:
        await state.set_state(UserStates.waiting_for_link)

async def download_and_send_video(
    link: str,
    quality: str,
    message: Message,
    progress_msg: Message,
    state: FSMContext
) -> bool:
    """Video yuklash va yuborish jarayoni"""
    video_file = None
    try:
        async with MediaDownloader() as downloader:
            # Videoni yuklash
            video_file = await downloader.download_video(
                url=link,
                quality=quality,
                progress_msg=progress_msg
            )
            
            if not video_file:
                await progress_msg.edit_text(
                    "❌ *Video yuklab bo'lmadi!*\n\n"
                    "Iltimos, quyidagilarni tekshiring:\n"
                    "1. Link to'g'riligi\n"
                    "2. Video mavjudligi\n"
                    "3. Internet ulanishi\n"
                    "4. Boshqa link urinib ko'ring\n\n"
                    "🔗 Yangi link yuboring:",
                    parse_mode="Markdown"
                )
                return False
            
            # Video yuborish
            await progress_msg.edit_text(
                "✅ *Video yuklandi!*\n\n"
                "📤 Telegramga yuborilmoqda...",
                parse_mode="Markdown"
            )
            
            send_success = await downloader.send_video_to_user(
                video_path=video_file,
                original_url=link,
                message=message
            )
            
            if send_success:
                await progress_msg.edit_text(
                    "✅ *Video muvaffaqiyatli yuborildi!*\n\n"
                    "🎉 Endi yangi video linkini yuboring:",
                    parse_mode="Markdown"
                )
            else:
                await progress_msg.edit_text(
                    "❌ *Video yuborish muvaffaqiyatsiz!*\n\n"
                    "Iltimos, qayta urinib ko'ring yoki\n"
                    "boshqa video linkini yuboring:",
                    parse_mode="Markdown"
                )
            
            return send_success
            
    except Exception as e:
        logger.error(f"Video jarayonida xatolik: {e}", exc_info=True)
        await progress_msg.edit_text(
            "❌ *Xatolik yuz berdi!*\n\n"
            "Iltimos, quyidagilarni tekshiring:\n"
            "• Internet ulanishi\n"
            "• Video mavjudligi\n"
            "• Link to'g'riligi\n\n"
            "Keyin qayta urinib ko'ring:",
            parse_mode="Markdown"
        )
        return False
        
    finally:
        # Faylni tozalash
        if video_file and os.path.exists(video_file):
            try:
                # Kechiktirilgan holda o'chirish
                asyncio.create_task(downloader.cleanup_file(video_file))
            except:
                pass
        
        # Holatni qayta o'rnatish
        await state.set_state(UserStates.waiting_for_link)

# ==================== BOTNI ISHGA TUSHIRISH ====================
async def on_startup():
    """Bot ishga tushganda"""
    try:
        bot_info = await bot.get_me()
        logger.info(f"🎬 Media Bot ishga tushdi: @{bot_info.username}")
        print("=" * 50)
        print("🎬 MEDIA DOWNLOADER BOT")
        print("=" * 50)
        print(f"🤖 Bot: @{bot_info.username}")
        print(f"⚡ Version: 2.0")
        print(f"📊 Holat: Faol")
        print("=" * 50)
    except Exception as e:
        logger.error(f"Bot ishga tushirishda xatolik: {e}")

async def on_shutdown():
    """Bot to'xtaganda"""
    logger.info("🛑 Media Bot to'xtatildi")

async def main():
    """Asosiy funksiya"""
    try:
        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)
        
        # Downloads papkasini yaratish
        os.makedirs('downloads', exist_ok=True)
        
        # Botni ishga tushirish
        await dp.start_polling(bot, skip_updates=True)
        
    except KeyboardInterrupt:
        print("\n\n🛑 Bot to'xtatildi.")
    except Exception as e:
        logger.error(f"Bot ishga tushirishda xatolik: {e}", exc_info=True)
        print(f"\n\n❌ Xatolik: {e}")
    finally:
        # Bot sessionini yopish
        await bot.session.close()

if __name__ == "__main__":
    # Windows uchun asyncio policy
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # Asosiy dasturni ishga tushirish
    asyncio.run(main())
