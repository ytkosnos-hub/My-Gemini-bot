import os
import base64
import threading
import requests
import telebot
from telebot import types
from flask import Flask, request as flask_request

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

# Текстовые модели: по очереди, если первая перегружена или недоступна
MODELS = [os.getenv("GEMINI_MODEL", "gemini-3.6-flash"), "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]
# Модели для рисования картинок
IMAGE_MODELS = [os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image"), "gemini-3.1-flash-lite-image"]

MAX_HISTORY = 30                      # сколько последних сообщений помнит бот (вопросы + ответы)
RESET_BUTTON = "🔄 Сбросить контекст"

histories = {}                        # память: номер чата -> список сообщений
lock = threading.Lock()

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
app = Flask(__name__)


# ---------- Вспомогательное ----------

def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton(RESET_BUTTON))
    return kb


def send_long(message, text):
    # Telegram не принимает сообщения длиннее 4096 символов, режем на части
    for i in range(0, len(text), 4000):
        bot.reply_to(message, text[i:i + 4000], reply_markup=main_keyboard())


def get_history(chat_id):
    with lock:
        return list(histories.get(chat_id, []))


def save_turn(chat_id, user_text, model_text):
    with lock:
        h = histories.get(chat_id, [])
        h = h + [
            {"role": "user", "parts": [{"text": user_text}]},
            {"role": "model", "parts": [{"text": model_text}]},
        ]
        h = h[-MAX_HISTORY:]
        if h and h[0]["role"] == "model":   # история обязана начинаться с вопроса
            h = h[1:]
        histories[chat_id] = h


def headers():
    return {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}


# ---------- Текст ----------

def ask_gemini(contents):
    """Возвращает (успех, текст)."""
    data = {"contents": contents}
    last_error = ""
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            r = requests.post(url, headers=headers(), json=data, timeout=25)
            res = r.json()
        except Exception as e:
            last_error = f"{model}: {e}"
            print("REQUEST ERROR", last_error, flush=True)
            continue

        try:
            parts = res["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return True, text
        except (KeyError, IndexError, TypeError):
            pass

        err = res.get("error", {}) if isinstance(res, dict) else {}
        last_error = f"{model}: {err.get('code')} {err.get('message', res)}"
        print("GEMINI ERROR", last_error, flush=True)
        if err.get("code") in (400, 401, 403):
            break
    return False, f"Ошибка Gemini: {last_error[:500]}"


# ---------- Картинки ----------

def generate_image(prompt):
    """Возвращает (байты картинки или None, текст/ошибка)."""
    data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    last_error = ""
    for model in IMAGE_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            r = requests.post(url, headers=headers(), json=data, timeout=60)
            res = r.json()
        except Exception as e:
            last_error = f"{model}: {e}"
            print("IMAGE REQUEST ERROR", last_error, flush=True)
            continue

        try:
            parts = res["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
            for p in parts:
                blob = p.get("inlineData") or p.get("inline_data")
                if blob and blob.get("data"):
                    return base64.b64decode(blob["data"]), text
        except (KeyError, IndexError, TypeError):
            pass

        err = res.get("error", {}) if isinstance(res, dict) else {}
        last_error = f"{model}: {err.get('code')} {err.get('message', res)}"
        print("IMAGE ERROR", last_error, flush=True)
        if err.get("code") in (400, 401, 403):
            break
    return None, (
        "Не получилось нарисовать. Возможно, генерация картинок недоступна на бесплатном ключе.\n"
        f"Подробности: {last_error[:300]}"
    )


# ---------- Telegram: веб-часть ----------

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    if flask_request.headers.get("content-type") == "application/json":
        update = telebot.types.Update.de_json(flask_request.get_data().decode("utf-8"))
        threading.Thread(target=bot.process_new_updates, args=([update],), daemon=True).start()
        return "", 200
    return "Forbidden", 403


@app.route("/")
def index():
    return "Bot is running perfectly!", 200


# ---------- Telegram: команды и сообщения ----------

@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(
        message,
        "Здарова! Я твой личный Gemini. Я помню наш разговор.\n\n"
        "• Просто пиши текст или присылай фото\n"
        "• /img описание — нарисую картинку\n"
        "• Кнопка «Сбросить контекст» внизу — забуду всё и начнём с нуля",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["reset"])
@bot.message_handler(func=lambda m: m.text == RESET_BUTTON, content_types=["text"])
def reset_context(message):
    with lock:
        histories.pop(message.chat.id, None)
    bot.reply_to(message, "Готово, контекст сброшен. Начинаем с чистого листа.", reply_markup=main_keyboard())


@bot.message_handler(commands=["img"])
def handle_img(message):
    prompt = message.text.partition(" ")[2].strip()
    if not prompt:
        bot.reply_to(message, "Напиши после команды, что нарисовать. Например:\n/img рыжий кот в космосе")
        return
    bot.send_chat_action(message.chat.id, "upload_photo")
    image, text = generate_image(prompt)
    if image:
        bot.send_photo(message.chat.id, image, caption=(text[:1000] or None), reply_to_message_id=message.message_id)
    else:
        bot.reply_to(message, text)


@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    bot.send_chat_action(chat_id, "typing")
    contents = get_history(chat_id) + [{"role": "user", "parts": [{"text": message.text}]}]
    ok, answer = ask_gemini(contents)
    if ok:
        save_turn(chat_id, message.text, answer)
    send_long(message, answer)


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    chat_id = message.chat.id
    try:
        bot.send_chat_action(chat_id, "typing")
        file_info = bot.get_file(message.photo[-1].file_id)
        img = bot.download_file(file_info.file_path)
        prompt = message.caption or "Опиши что на фото и ответь на русском"
        user_turn = {"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode("utf-8")}},
        ]}
        ok, answer = ask_gemini(get_history(chat_id) + [user_turn])
        if ok:
            # картинку в памяти не храним, только пометку, чтобы не раздувать историю
            save_turn(chat_id, f"[Я отправил фото] {prompt}", answer)
        send_long(message, answer)
    except Exception as e:
        print("PHOTO ERROR", e, flush=True)
        bot.reply_to(message, f"Ошибка обработки фото: {e}")


# ---------- Запуск ----------

if RENDER_EXTERNAL_URL:
    bot.remove_webhook()
    bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/{TELEGRAM_TOKEN}")
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Начать"),
            types.BotCommand("reset", "Сбросить контекст"),
            types.BotCommand("img", "Нарисовать картинку"),
        ])
    except Exception as e:
        print("COMMANDS ERROR", e, flush=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
