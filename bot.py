import os
import base64
import threading
import requests
import telebot
from flask import Flask, request as flask_request

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

# Основная модель можно сменить в Render -> Environment -> GEMINI_MODEL, без правки кода
MODELS = [os.getenv("GEMINI_MODEL", "gemini-3.6-flash"), "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
app = Flask(__name__)


def ask_gemini(prompt, image_bytes=None):
    parts = [{"text": prompt}]
    if image_bytes:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(image_bytes).decode("utf-8"),
            }
        })
    data = {"contents": [{"parts": parts}]}
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    last_error = ""
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            r = requests.post(url, headers=headers, json=data, timeout=25)
            res = r.json()
        except Exception as e:
            last_error = f"{model}: {e}"
            print("REQUEST ERROR", last_error, flush=True)
            continue

        try:
            parts_out = res["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts_out).strip()
            if text:
                return text
        except (KeyError, IndexError, TypeError):
            pass

        err = res.get("error", {}) if isinstance(res, dict) else {}
        last_error = f"{model}: {err.get('code')} {err.get('message', res)}"
        print("GEMINI ERROR", last_error, flush=True)
        # 400/401/403 = проблема с ключом или запросом, перебирать модели бессмысленно
        # 404, 429, 500, 503, 504 и т.д. = модель недоступна или перегружена -> пробуем следующую
        if err.get("code") in (400, 401, 403):
            break

    return f"Ошибка Gemini: {last_error[:500]}"


@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    if flask_request.headers.get("content-type") == "application/json":
        update = telebot.types.Update.de_json(flask_request.get_data().decode("utf-8"))
        # отвечаем Telegram сразу, а сообщение обрабатываем в фоне (иначе он шлёт повторы)
        threading.Thread(target=bot.process_new_updates, args=([update],), daemon=True).start()
        return "", 200
    return "Forbidden", 403


@app.route("/")
def index():
    return "Bot is running perfectly!", 200


@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(message, "Здарова! Я твой личный Gemini. Напиши мне текст или отправь картинку :)")


@bot.message_handler(content_types=["text"])
def handle_text(message):
    bot.send_chat_action(message.chat.id, "typing")
    bot.reply_to(message, ask_gemini(message.text))


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        img = bot.download_file(file_info.file_path)
        prompt = message.caption or "Опиши что на фото и ответь на русском"
        bot.reply_to(message, ask_gemini(prompt, img))
    except Exception as e:
        print("PHOTO ERROR", e, flush=True)
        bot.reply_to(message, f"Ошибка обработки фото: {e}")


if RENDER_EXTERNAL_URL:
    bot.remove_webhook()
    bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/{TELEGRAM_TOKEN}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
