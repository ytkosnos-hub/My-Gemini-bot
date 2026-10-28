import os
import telebot
from PIL import Image
import google.generativeai as genai

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

bot = telebot.TeleBot(TELEGRAM_TOKEN)


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(
      message,
      "Здарова! Я твой личный Gemini. Напиши мне что-нибудь или отправь"
      " картинку :)",
  )


@bot.message_handler(content_types=["text"])
def handle_text(message):
  try:
    response = model.generate_content(message.text)
    bot.reply_to(message, response.text)
  except Exception as e:
    bot.reply_to(message, f"Ошибка: {e}")


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
  try:
    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    image_path = "image.jpg"
    with open(image_path, "wb") as new_file:
      new_file.write(downloaded_file)

    img = Image.open(image_path)
    prompt = (
        message.caption
        if message.caption
        else "Опиши что на фото и ответь на русском"
    )
    response = model.generate_content([prompt, img])

    bot.reply_to(message, response.text)
  except Exception as e:
    bot.reply_to(message, f"Ошибка обработки фото: {e}")


if __name__ == "__main__":
  bot.infinity_polling()
