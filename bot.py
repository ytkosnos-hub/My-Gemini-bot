# -*- coding: utf-8 -*-
"""
Mewyto AI — Telegram-бот (Flask + webhook, как у тебя в проде на Render).

Разделы (единственные кнопки верхнего уровня):
  📚 Учёба — контекст, свой промт, режимы Обычный/Высокий. Без картинок и поиска.
             Если ответ длиннее лимита Telegram — уходит автоматически PDF-файлом.
  🏠 Быт   — пока пустой, заглушка под будущие функции.
"""

import os
import threading
from datetime import datetime
from io import BytesIO

import requests
import telebot
from telebot import types
from flask import Flask, request

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

# ──────────────────────────── Ключи — только из Render → Environment ────────────────────────────
# В коде ключей нет и быть не должно.

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

GEMINI_API_KEYS = [
    k.strip()
    for k in os.environ.get("GEMINI_API_KEYS", os.environ.get("GEMINI_API_KEY", "")).split(",")
    if k.strip()
]
if not GEMINI_API_KEYS:
    raise RuntimeError("Задай GEMINI_API_KEY (или несколько через запятую в GEMINI_API_KEYS) в Render → Environment")

MODEL_NAME = os.environ.get("TEXT_MODEL", "gemini-3.5-flash")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "mewyto-hook")

# ──────────────────────────── Экономия токенов ────────────────────────────
# Коротние промты + жёсткий потолок на длину ответа + небольшая память диалога.
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "2048"))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "20"))  # сообщений (user+model) в памяти на пользователя

TELEGRAM_MAX_MESSAGE = 4096
CURRENT_DATE = datetime.now().strftime("%d.%m.%Y")

SYSTEM_PROMPT_NORMAL = (
    f"Ты — Mewyto AI, точный помощник. Сегодня {CURRENT_DATE}. "
    "Никогда не выдумывай цифры, курсы валют и факты — если не уверен, так и скажи. "
    "Отвечай по делу, без воды, обычным текстом без Markdown (без ** и #)."
)

SYSTEM_PROMPT_ADVANCED = (
    f"Ты — Mewyto AI в режиме Высокий (Эксперт). Сегодня {CURRENT_DATE}. "
    "Давай развёрнутый, но по делу разбор, пошагово, без воды. Никаких выдуманных фактов. "
    "Обычный текст без Markdown (без ** и #)."
)

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None, threaded=True)
app = Flask(__name__)

_key_index = 0
_key_lock = threading.Lock()

# Память в оперативке — пропадает при перезапуске Render (как и раньше в этом проекте)
user_modes: dict[int, str] = {}       # "normal" | "advanced"
user_sections: dict[int, str] = {}    # "study" | "home" | None
user_prompts: dict[int, str] = {}     # свой промт пользователя
gemini_history: dict[int, list] = {}  # история диалога для Gemini


# ──────────────────────────── Шрифт для PDF (кириллица) ────────────────────────────

FONT_PATH = "/tmp/DejaVuSans.ttf"
FONT_NAME = "Helvetica"  # запасной вариант без кириллицы


def _ensure_font() -> None:
    global FONT_NAME
    try:
        if not os.path.exists(FONT_PATH):
            url = "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans.ttf"
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            with open(FONT_PATH, "wb") as f:
                f.write(r.content)
        pdfmetrics.registerFont(TTFont("DejaVuSans", FONT_PATH))
        FONT_NAME = "DejaVuSans"
    except Exception as e:
        print("Шрифт с кириллицей не загрузился, PDF будет латиницей:", e)


_ensure_font()


def make_pdf(text: str, title: str = "Ответ Mewyto AI") -> bytes:
    import textwrap

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    margin = 18 * mm
    y = height - margin

    c.setFont(FONT_NAME, 12)
    c.drawString(margin, y, title)
    y -= 10 * mm
    c.setFont(FONT_NAME, 10)

    for paragraph in text.split("\n"):
        lines = textwrap.wrap(paragraph, width=95) or [""]
        for line in lines:
            if y < margin:
                c.showPage()
                c.setFont(FONT_NAME, 10)
                y = height - margin
            c.drawString(margin, y, line)
            y -= 5.5 * mm

    c.save()
    return buf.getvalue()


# ──────────────────────────── Gemini ────────────────────────────

def remember(uid: int, user_text: str, answer: str) -> None:
    g = gemini_history.setdefault(uid, [])
    g.append({"role": "user", "parts": [{"text": user_text}]})
    g.append({"role": "model", "parts": [{"text": answer}]})
    del g[:-MAX_HISTORY]


def ask_gemini(uid: int, text: str) -> str:
    global _key_index

    mode = user_modes.get(uid, "normal")
    base_prompt = SYSTEM_PROMPT_NORMAL if mode == "normal" else SYSTEM_PROMPT_ADVANCED
    custom = user_prompts.get(uid)
    sys_prompt = base_prompt + (f"\n\nИнструкция от пользователя: {custom}" if custom else "")

    history = list(gemini_history.get(uid, []))
    contents = history + [{"role": "user", "parts": [{"text": text}]}]

    payload = {
        "system_instruction": {"parts": [{"text": sys_prompt}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
    }

    last_error = ""
    for _ in range(len(GEMINI_API_KEYS)):
        with _key_lock:
            key = GEMINI_API_KEYS[_key_index]

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"
        try:
            resp = requests.post(url, json=payload, headers={"x-goog-api-key": key}, timeout=90)
        except requests.RequestException as e:
            last_error = str(e)
            with _key_lock:
                _key_index = (_key_index + 1) % len(GEMINI_API_KEYS)
            continue

        if resp.status_code in (429, 503):
            last_error = resp.text[:300]
            with _key_lock:
                _key_index = (_key_index + 1) % len(GEMINI_API_KEYS)
            continue

        if resp.status_code != 200:
            raise RuntimeError(f"Код {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        texts = []
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if part.get("text"):
                    texts.append(part["text"])

        if not texts:
            raise RuntimeError("Модель не вернула текст")

        return "".join(texts).replace("**", "").replace("##", "")

    raise RuntimeError(f"Все ключи исчерпали лимит. Детали: {last_error[:300]}")


# ──────────────────────────── Меню ────────────────────────────

BTN_STUDY = "📚 Учёба"
BTN_HOME = "🏠 Быт"
BTN_BACK = "⬅ Назад"
BTN_PROMPT = "📝 Промт"
BTN_CONTEXT = "🔄 Контекст"


def main_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    kb.row(BTN_STUDY, BTN_HOME)
    return kb


def mode_label(uid: int) -> str:
    mode = user_modes.get(uid, "normal")
    target = "Высокий" if mode == "normal" else "Обычный"
    return f"🧠 Режим: {target}"


def study_menu(uid: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    kb.row(mode_label(uid))
    kb.row(BTN_PROMPT, BTN_CONTEXT)
    kb.row(BTN_BACK)
    return kb


# ──────────────────────────── Хендлеры ────────────────────────────

@bot.message_handler(commands=["start", "cancel"])
def cmd_start(m: types.Message):
    uid = m.from_user.id
    user_sections[uid] = None
    bot.send_message(
        m.chat.id,
        f"Привет! Я Mewyto AI.\nСегодня {CURRENT_DATE}.\n\nВыбери раздел:",
        reply_markup=main_menu(),
    )


@bot.message_handler(func=lambda m: m.text == BTN_STUDY)
def open_study(m: types.Message):
    uid = m.from_user.id
    user_sections[uid] = "study"
    bot.send_message(m.chat.id, "Раздел «Учёба». Пиши вопрос — отвечу.", reply_markup=study_menu(uid))


@bot.message_handler(func=lambda m: m.text == BTN_HOME)
def open_home(m: types.Message):
    user_sections[m.from_user.id] = "home"
    bot.send_message(
        m.chat.id,
        "Раздел «Быт» пока пустой — скоро добавим сюда функции.",
        reply_markup=main_menu(),
    )


@bot.message_handler(func=lambda m: m.text == BTN_BACK)
def go_back(m: types.Message):
    user_sections[m.from_user.id] = None
    bot.send_message(m.chat.id, "Главное меню. Выбери раздел:", reply_markup=main_menu())


@bot.message_handler(func=lambda m: bool(m.text) and m.text.startswith("🧠 Режим:"))
def toggle_mode(m: types.Message):
    uid = m.from_user.id
    cur = user_modes.get(uid, "normal")
    user_modes[uid] = "advanced" if cur == "normal" else "normal"
    readable = "Высокий" if user_modes[uid] == "advanced" else "Обычный"
    bot.send_message(m.chat.id, f"✅ Режим: {readable}", reply_markup=study_menu(uid))


@bot.message_handler(func=lambda m: m.text == BTN_CONTEXT)
def clear_context(m: types.Message):
    gemini_history.pop(m.from_user.id, None)
    bot.send_message(m.chat.id, "🔄 Контекст диалога очищен.", reply_markup=study_menu(m.from_user.id))


@bot.message_handler(func=lambda m: m.text == BTN_PROMPT)
def ask_prompt(m: types.Message):
    msg = bot.send_message(
        m.chat.id,
        "Напиши свою инструкцию для бота (как ему отвечать на твои вопросы).\n"
        "Чтобы убрать текущую — пришли «сброс».",
    )
    bot.register_next_step_handler(msg, _save_prompt)


def _save_prompt(m: types.Message):
    uid = m.from_user.id
    text = (m.text or "").strip()
    if text.lower() in ("сброс", "очистить"):
        user_prompts.pop(uid, None)
        bot.send_message(m.chat.id, "Промт сброшен.", reply_markup=study_menu(uid))
    else:
        user_prompts[uid] = text[:1000]
        bot.send_message(m.chat.id, "✅ Промт сохранён.", reply_markup=study_menu(uid))


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(m: types.Message):
    uid = m.from_user.id
    section = user_sections.get(uid)

    if section != "study":
        bot.send_message(m.chat.id, "Сначала выбери раздел «📚 Учёба» в меню.", reply_markup=main_menu())
        return

    thinking = bot.send_message(m.chat.id, "💭 Думаю...")

    try:
        answer = ask_gemini(uid, m.text)
        remember(uid, m.text, answer)
    except Exception as e:
        try:
            bot.edit_message_text(f"Ошибка при обращении к ИИ: {e}"[:300], m.chat.id, thinking.message_id)
        except Exception:
            pass
        return

    try:
        bot.delete_message(m.chat.id, thinking.message_id)
    except Exception:
        pass

    if len(answer) > TELEGRAM_MAX_MESSAGE:
        pdf_bytes = make_pdf(answer)
        bot.send_document(
            m.chat.id,
            (("answer.pdf", BytesIO(pdf_bytes))),
            caption="Ответ получился длинным — сделал PDF-файл.",
            reply_markup=study_menu(uid),
        )
    else:
        bot.send_message(m.chat.id, answer, reply_markup=study_menu(uid))


# ──────────────────────────── Flask / webhook ────────────────────────────

@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def telegram_webhook():
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "ok", 200


@app.route("/")
def health():
    return "Mewyto AI жив", 200


def _setup_webhook():
    external = os.environ.get("RENDER_EXTERNAL_URL")
    if not external:
        print("RENDER_EXTERNAL_URL не найден — webhook не выставлен (норм для локального запуска).")
        return
    bot.remove_webhook()
    bot.set_webhook(url=f"{external}/webhook/{WEBHOOK_SECRET}")
    print("Webhook выставлен:", f"{external}/webhook/{WEBHOOK_SECRET}")


_setup_webhook()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
