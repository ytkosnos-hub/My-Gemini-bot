import asyncio
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
import google.generativeai as genai
from PIL import Image
import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Здарова! Я твой личный Gemini. Отправляй мне текст или фотографии, и я отвечу :)")

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    file_path = file_info.file_path
    downloaded_file = await bot.download_file(file_path)
    image = Image.open(downloaded_file)
    
    prompt = message.caption if message.caption else "Опиши это изображение подробно."
    status_msg = await message.answer("Думаю над фото...")
    
    try:
        response = model.generate_content([image, prompt])
        await message.answer(response.text)
    except Exception as e:
        await message.answer(f"Ошибка при обработке: {e}")
    finally:
        await status_msg.delete()

@dp.message(F.text)
async def handle_text(message: types.Message):
    status_msg = await message.answer("Думаю...")
    try:
        response = model.generate_content(message.text)
        await message.answer(response.text)
    except Exception as e:
        await message.answer(f"Ошибка при обработке: {e}")
    finally:
        await status_msg.delete()

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
