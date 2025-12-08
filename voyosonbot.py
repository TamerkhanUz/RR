import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

API_TOKEN = "8273067986:AAGff5oZco9Isxa6IOQZ_PfQB2MLvEHbLHE"  # TOKENINGIZNI SHU YERGA QO'YING
CHANNEL_USERNAME = "@BSONewsUz"               # Majburiy obuna kanali

# Aiogram 3.7.0+ versiyasi uchun yangi usul
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# --- START ---
@dp.message(Command("start"))
async def start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga o‘tish", url=f"https://t.me/{CHANNEL_USERNAME[1:]}")],
        [InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub")]
    ])

    text = (
        "👋 Matematikani yech va har bir masalaga pul ishla.\n"
        "Har bir masala yechganga <b>5000 so'm</b> dan 💸\n\n"
        f"👉 Avval <b>{CHANNEL_USERNAME}</b> kanaliga obuna bo'ling!"
    )

    await message.answer(text, reply_markup=kb)

# --- OBUNANI TEKSHIRISH ---
@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    try:
        # Kanal a'zoligini tekshirish
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        
        if member.status in ["member", "administrator", "creator"]:
            # Obuna bo'lgan
            await callback.message.edit_text(
                "✅ <b>Obuna tasdiqlandi!</b>\n\n"
                "😊 Endi ishni boshlaymiz.\n"
                "Sizda <b>3 ta imkoniyat</b> bor yechish uchun, iltimos xato qilmang 😄\n\n"
                "<b>Masala:</b> 45 - 84 + (87 × 588) ÷ 2 + 0.5 - 0.5 + 2 × 0 + 2 = ???\n\n"
                "Javobingizni yozing 👇"
            )
        else:
            # Obuna bo'lmagan
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Kanalga o'tish", url=f"https://t.me/{CHANNEL_USERNAME[1:]}")],
                [InlineKeyboardButton(text="🔄 Obunani tekshirish", callback_data="check_sub")]
            ])
            await callback.message.edit_text(
                f"❌ <b>Obuna tasdiqlanmadi!</b>\n\n"
                f"❗️ Iltimos, avval {CHANNEL_USERNAME} kanaliga obuna bo'ling va keyin tekshirish tugmasini bosing.",
                reply_markup=kb
            )
    
    except Exception as e:
        print(f"Xatolik: {e}")
        await callback.message.edit_text(
            "⚠️ Kanalga ulanishda xatolik yuz berdi. Iltimos, keyinroq urinib ko'ring."
        )
    
    await callback.answer()

# --- FOYDALANUVCHI JAVOB YOZGANDA ---
@dp.message(F.text & ~F.text.startswith('/'))
async def any_message(message: Message):
    # Istalgan matn yozilganda (command emas)
    final_text = (
        "Do'stim hafa bo'lma sodda bo'lganing uchun shu botga kelding.\n"
        "Hamma narsa mehnat orqali bo'ladi.\n"
        "Bir ustozim aytgandi: <b>Tekin pishloq faqat qopqonda bo'ladi</b> deb... 😚\n"
        "Raxmat!"
    )
    await message.answer(final_text)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())