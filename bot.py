import os
import telebot
from PIL import Image
import google.generativeai as genai
from flask import Flask, request

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)


@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
  if request.headers.get("content-type") == "application/json":
    json_string = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "", 200
  else:
    return "Forbidden", 403


@app.route("/")
def index():
  return "Bot is running perfectly!", 200


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(
      message,
      "Здарова! Я твой личный Gemini. Напиши мне текст или отправь картинку :)",
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
  bot.remove_webhook()
  if RENDER_EXTERNAL_URL:
    bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/{TELEGRAM_TOKEN}")

  port = int(os.getenv("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
