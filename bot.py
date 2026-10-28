import os
import requests
import telebot
from PIL import Image
from flask import Flask, request as flask_request

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)


def ask_gemini(prompt, image_path=None):
  url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
  headers = {"Content-Type": "application/json"}

  parts = [{"text": prompt}]

  if image_path and os.path.exists(image_path):
    import base64

    with open(image_path, "rb") as img_file:
      b64_data = base64.b64encode(img_file.read()).decode("utf-8")
    parts.append({
        "inline_data": {"mime_type": "image/jpeg", "data": b64_data}
    })

  data = {"contents": [{"parts": parts}]}

  try:
    response = requests.post(url, headers=headers, json=data, timeout=30)
    res_json = response.json()
    return res_json["candidates"][0]["content"]["parts"][0]["text"]
  except Exception as e:
    return f"Ошибка API: {e}"


@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
  if flask_request.headers.get("content-type") == "application/json":
    json_string = flask_request.get_data().decode("utf-8")
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
  answer = ask_gemini(message.text)
  bot.reply_to(message, answer)


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
  try:
    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    image_path = "image.jpg"
    with open(image_path, "wb") as new_file:
      new_file.write(downloaded_file)

    prompt = (
        message.caption
        if message.caption
        else "Опиши что на фото и ответь на русском"
    )
    answer = ask_gemini(prompt, image_path)

    bot.reply_to(message, answer)
  except Exception as e:
    bot.reply_to(message, f"Ошибка обработки фото: {e}")


if __name__ == "__main__":
  bot.remove_webhook()
  if RENDER_EXTERNAL_URL:
    bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/{TELEGRAM_TOKEN}")

  port = int(os.getenv("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
