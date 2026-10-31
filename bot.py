# -*- coding: utf-8 -*-
"""
Mewyto AI — Telegram-бот (Flask + webhook).

Разделы (единственные кнопки верхнего уровня):
  📚 Учёба — контекст (со счётчиком), свой промт, режимы Обычный/Высокий.
             Понимает фото (решает задания), умеет сама присылать график
             функции и таблицу картинкой, если это уместно по ответу.
             Если ответ длиннее лимита Telegram — уходит PDF-файлом.
  🏠 Быт   — пока пустой, заглушка под будущие функции.
"""

import base64
import os
import re
import threading
from datetime import datetime
from io import BytesIO

import requests
import telebot
from telebot import types
from flask import Flask, request

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

# ──────────────────────────── Ключи — только из Render → Environment ────────────────────────────
# В коде ключей нет. TELEGRAM_TOKEN обязателен; ключей Gemini можно сколько угодно:
# GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3, ... — бот сам их все найдёт.

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]


def _collect_gemini_keys() -> list[str]:
    keys = []
    if os.environ.get("GEMINI_API_KEY"):
        keys.append(os.environ["GEMINI_API_KEY"])
    i = 2
    while True:
        val = os.environ.get(f"GEMINI_API_KEY_{i}")
        if not val:
            break
        keys.append(val)
        i += 1
    return keys


GEMINI_API_KEYS = _collect_gemini_keys()
if not GEMINI_API_KEYS:
    raise RuntimeError("Задай GEMINI_API_KEY (и при желании GEMINI_API_KEY_2, _3, ...) в Render → Environment")

# Модель для обычного режима (дешёвая и быстрая) и для высокого (самая умная,
# с автоматическим откатом на более простую, если у конкретного ключа нет к ней доступа).
NORMAL_MODEL = os.environ.get("NORMAL_MODEL", "gemini-3.5-flash-lite")
HIGH_MODEL = os.environ.get("HIGH_MODEL", "gemini-3.1-pro-preview")
HIGH_MODEL_FALLBACK = os.environ.get("HIGH_MODEL_FALLBACK", "gemini-3.5-flash")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "mewyto-hook")

# ──────────────────────────── Экономия токенов ────────────────────────────
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "2048"))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "20"))  # сообщений (user+model) в памяти на пользователя

TELEGRAM_MAX_MESSAGE = 4096
CURRENT_DATE = datetime.now().strftime("%d.%m.%Y")

SYSTEM_PROMPT_BASE = (
    f"Ты — Mewyto AI, точный помощник. Сегодня {CURRENT_DATE}. "
    "Никогда не выдумывай цифры и факты — если не уверен, так и скажи. "
    "Форматирование: обычный текст, важные слова и заголовки выделяй **жирным** (двойными звёздочками), "
    "никаких решёток # и прочей сложной Markdown-разметки. "
    "Математику и химию пиши понятными юникод-символами, а не текстом и не значком ^: "
    "π, √, x², x³, xⁿ, H₂O, Na⁺ — а не 'pi', 'x^2', 'sqrt(x)'. "
    "Если по смыслу ответа нужен график функции — в самом конце ответа, отдельной строкой, напиши "
    "'ГРАФИК: <формула от x>' (в формуле только x, числа и + - * / ** sin cos tan sqrt log exp abs, "
    "больше в этой строке ничего не пиши). Пиши эту строку только если график реально нужен. "
    "Если данные удобно показать таблицей — оформи её Markdown-таблицей (строки с | и строка из дефисов)."
)

SYSTEM_PROMPT_NORMAL = SYSTEM_PROMPT_BASE + " Отвечай быстро и по делу, без лишних деталей."
SYSTEM_PROMPT_ADVANCED = SYSTEM_PROMPT_BASE + " Отвечай максимально разобрано, по шагам, разбирая всё подробно."

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None, threaded=True)
app = Flask(__name__)

_key_index = 0
_key_lock = threading.Lock()
pro_incapable_keys: set[str] = set()  # ключи, которым недоступна HIGH_MODEL — узнаём сами на лету

# Память в оперативке — пропадает при перезапуске Render
user_modes: dict[int, str] = {}       # "normal" | "advanced"
user_sections: dict[int, str] = {}    # "study" | "home" | None
user_prompts: dict[int, str] = {}     # свой промт пользователя
gemini_history: dict[int, list] = {}  # история диалога для Gemini


# ──────────────────────────── Шрифт для PDF/картинок (кириллица) ────────────────────────────
# Берём шрифт из пакета matplotlib — он уже содержит DejaVuSans с кириллицей,
# так что ничего не надо качать из интернета при старте (раньше скачивание иногда не срабатывало).

FONT_NAME = "Helvetica"
FONT_BOLD_NAME = "Helvetica-Bold"
try:
    _ttf_dir = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
    pdfmetrics.registerFont(TTFont("DejaVuSans", os.path.join(_ttf_dir, "DejaVuSans.ttf")))
    FONT_NAME = "DejaVuSans"
    pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", os.path.join(_ttf_dir, "DejaVuSans-Bold.ttf")))
    FONT_BOLD_NAME = "DejaVuSans-Bold"
    plt.rcParams["font.family"] = "DejaVu Sans"
except Exception as e:
    print("Не нашёл шрифт DejaVuSans у matplotlib, PDF/картинки будут латиницей:", e)


def make_pdf(text: str, title: str = "Ответ Mewyto AI") -> bytes:
    import textwrap

    plain = text.replace("**", "")  # в PDF жирный не рисуем, просто убираем маркеры

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    margin = 18 * mm
    y = height - margin

    c.setFont(FONT_BOLD_NAME, 13)
    c.drawString(margin, y, title)
    y -= 10 * mm
    c.setFont(FONT_NAME, 10)

    for paragraph in plain.split("\n"):
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


# ──────────────────────────── График и таблица картинкой ────────────────────────────

SAFE_NAMES = {
    "np": np, "pi": np.pi, "e": np.e,
    "sin": np.sin, "cos": np.cos, "tan": np.tan,
    "sqrt": np.sqrt, "log": np.log, "log10": np.log10, "exp": np.exp, "abs": np.abs,
}

GRAPH_RE = re.compile(r"\n?ГРАФИК:\s*(.+?)\s*$", re.IGNORECASE)
TABLE_RE = re.compile(r"(?:^\|.*\|[ \t]*$\n?){2,}", re.MULTILINE)


def extract_graph_formula(text: str):
    m = GRAPH_RE.search(text.strip())
    if not m:
        return text, None
    formula = m.group(1).strip()
    clean = text[: m.start()].rstrip()
    return clean, formula


def extract_table(text: str):
    m = TABLE_RE.search(text)
    if not m:
        return text, None
    block = m.group(0)
    rows = []
    for line in block.strip().splitlines():
        line = line.strip()
        if re.fullmatch(r"\|[\s\-:|]+\|", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)
    if len(rows) < 2:
        return text, None
    clean = (text[: m.start()] + "[таблица ниже]" + text[m.end():]).strip()
    return clean, rows


def render_graph(formula_text: str) -> bytes:
    expr = formula_text.strip().replace("^", "**")
    if len(expr) > 200 or "__" in expr or "import" in expr:
        raise ValueError("Недопустимая формула")

    expr = re.sub(r"(\d)([a-zA-Z(])", r"\1*\2", expr)
    expr = re.sub(r"(\))([a-zA-Z(])", r"\1*\2", expr)
    for func in ["sin", "cos", "tan", "sqrt", "log", "log10", "exp", "abs"]:
        expr = re.sub(r"\b" + func + r"\b", f"np.{func}", expr)

    x = np.linspace(-10, 10, 600)
    env = {"__builtins__": {}, "x": x, **SAFE_NAMES}
    with np.errstate(all="ignore"):
        y = eval(expr, env)
    y = np.broadcast_to(np.asarray(y, dtype=float), x.shape)

    fig = plt.figure(figsize=(7, 5))
    try:
        plt.plot(x, y, linewidth=2.2, color="#00a8ff")
        plt.axhline(0, color="black", linewidth=1, linestyle="--")
        plt.axvline(0, color="black", linewidth=1, linestyle="--")
        plt.grid(True, linestyle=":", alpha=0.6)
        plt.title(f"y = {formula_text}")
        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=160, bbox_inches="tight")
        return buf.getvalue()
    finally:
        plt.close(fig)


def render_table(rows: list[list[str]]) -> bytes:
    n_rows, n_cols = len(rows), max(len(r) for r in rows)
    fig, ax = plt.subplots(figsize=(min(10, 1.8 * n_cols), 0.55 * n_rows + 1))
    ax.axis("off")
    tbl = ax.table(cellText=rows[1:], colLabels=rows[0], loc="center", cellLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(11)
    tbl.scale(1, 1.6)
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ──────────────────────────── Telegram HTML (жирный текст) ────────────────────────────

def to_telegram_html(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if text.count("**") % 2 == 1:
        idx = text.rfind("**")
        text = text[:idx] + text[idx + 2:]
    parts = text.split("**")
    return "".join(f"<b>{p}</b>" if i % 2 == 1 else p for i, p in enumerate(parts))


# ──────────────────────────── Gemini ────────────────────────────

def remember(uid: int, user_text: str, answer: str) -> None:
    g = gemini_history.setdefault(uid, [])
    g.append({"role": "user", "parts": [{"text": user_text}]})
    g.append({"role": "model", "parts": [{"text": answer}]})
    del g[:-MAX_HISTORY]


def _call_gemini(model: str, key: str, sys_prompt: str, contents: list):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": sys_prompt}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
    }
    return requests.post(url, json=payload, headers={"x-goog-api-key": key}, timeout=90)


def _try_key(key: str, models: list[str], sys_prompt: str, contents: list):
    last = ""
    for model in models:
        try:
            resp = _call_gemini(model, key, sys_prompt, contents)
        except requests.RequestException as e:
            last = str(e)
            continue

        if resp.status_code == 200:
            data = resp.json()
            texts = [
                p["text"]
                for c in data.get("candidates", [])
                for p in c.get("content", {}).get("parts", [])
                if p.get("text")
            ]
            if texts:
                return "OK", "".join(texts)
            last = "Пустой ответ модели"
            continue

        if resp.status_code in (403, 404) and model == HIGH_MODEL:
            pro_incapable_keys.add(key)  # этому ключу недоступна умная модель — запомнили и пробуем проще
            continue

        if resp.status_code in (429, 503):
            last = resp.text[:200]
            continue

        last = f"{resp.status_code}: {resp.text[:200]}"

    return "FAIL", last


def ask_gemini(uid: int, text: str, image_bytes: bytes | None = None) -> str:
    global _key_index

    mode = user_modes.get(uid, "normal")
    sys_prompt = SYSTEM_PROMPT_NORMAL if mode == "normal" else SYSTEM_PROMPT_ADVANCED
    custom = user_prompts.get(uid)
    if custom:
        sys_prompt += f"\n\nИнструкция от пользователя: {custom}"

    history = list(gemini_history.get(uid, []))
    user_parts = [{"text": text}]
    if image_bytes is not None:
        user_parts.append({
            "inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode()}
        })
    contents = history + [{"role": "user", "parts": user_parts}]

    base_models = [NORMAL_MODEL] if mode == "normal" else [HIGH_MODEL, HIGH_MODEL_FALLBACK]

    last_error = ""
    for _ in range(len(GEMINI_API_KEYS)):
        with _key_lock:
            key = GEMINI_API_KEYS[_key_index]
            _key_index = (_key_index + 1) % len(GEMINI_API_KEYS)

        models = base_models
        if mode == "advanced" and key in pro_incapable_keys:
            models = [HIGH_MODEL_FALLBACK]

        status, result = _try_key(key, models, sys_prompt, contents)
        if status == "OK":
            return result
        last_error = result

    raise RuntimeError(f"Не удалось получить ответ ни по одному ключу. {last_error[:300]}")


# ──────────────────────────── Меню ────────────────────────────

BTN_STUDY = "📚 Учёба"
BTN_HOME = "🏠 Быт"
BTN_BACK = "⬅ Назад"
BTN_PROMPT = "📝 Промт"
CONTEXT_PREFIX = "🔄 Контекст:"
MODE_PREFIXES = ("🧠 Режим:", "⚡ Режим:")


def main_menu() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    kb.row(BTN_STUDY, BTN_HOME)
    return kb


def mode_label(uid: int) -> str:
    mode = user_modes.get(uid, "normal")
    return "⚡ Режим: Обычный" if mode == "normal" else "🧠 Режим: Высокий"


def context_label(uid: int) -> str:
    pairs = len(gemini_history.get(uid, [])) // 2
    cap = MAX_HISTORY // 2
    return f"{CONTEXT_PREFIX} {pairs}/{cap}"


def study_menu(uid: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    kb.row(mode_label(uid))
    kb.row(BTN_PROMPT, context_label(uid))
    kb.row(BTN_BACK)
    return kb


# ──────────────────────────── Отправка ответа (текст / PDF / график / таблица) ────────────────────────────

def send_answer(chat_id: int, uid: int, raw_answer: str) -> None:
    text, formula = extract_graph_formula(raw_answer)
    text, table_rows = extract_table(text)
    html_text = to_telegram_html(text)

    if len(html_text) > TELEGRAM_MAX_MESSAGE:
        pdf_bytes = make_pdf(text)
        bot.send_document(
            chat_id,
            ("answer.pdf", BytesIO(pdf_bytes)),
            caption="Ответ получился длинным — сделал PDF-файл.",
            reply_markup=study_menu(uid),
        )
    else:
        bot.send_message(chat_id, html_text, parse_mode="HTML", reply_markup=study_menu(uid))

    if table_rows:
        try:
            bot.send_photo(chat_id, ("table.png", BytesIO(render_table(table_rows))))
        except Exception as e:
            print("Не смог отрисовать таблицу:", e)

    if formula:
        try:
            bot.send_photo(chat_id, ("graph.png", BytesIO(render_graph(formula))), caption=f"y = {formula}")
        except Exception as e:
            bot.send_message(chat_id, f"Не смог построить график: {e}"[:300])


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
    bot.send_message(m.chat.id, "Раздел «Учёба». Пиши вопрос или кидай фото задания.", reply_markup=study_menu(uid))


@bot.message_handler(func=lambda m: m.text == BTN_HOME)
def open_home(m: types.Message):
    user_sections[m.from_user.id] = "home"
    bot.send_message(m.chat.id, "Раздел «Быт» пока пустой — скоро добавим сюда функции.", reply_markup=main_menu())


@bot.message_handler(func=lambda m: m.text == BTN_BACK)
def go_back(m: types.Message):
    user_sections[m.from_user.id] = None
    bot.send_message(m.chat.id, "Главное меню. Выбери раздел:", reply_markup=main_menu())


@bot.message_handler(func=lambda m: bool(m.text) and m.text.startswith(MODE_PREFIXES))
def toggle_mode(m: types.Message):
    uid = m.from_user.id
    cur = user_modes.get(uid, "normal")
    user_modes[uid] = "advanced" if cur == "normal" else "normal"
    readable = "Высокий" if user_modes[uid] == "advanced" else "Обычный"
    bot.send_message(m.chat.id, f"✅ Режим: {readable}", reply_markup=study_menu(uid))


@bot.message_handler(func=lambda m: bool(m.text) and m.text.startswith(CONTEXT_PREFIX))
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


@bot.message_handler(content_types=["photo"])
def handle_photo(m: types.Message):
    uid = m.from_user.id
    if user_sections.get(uid) != "study":
        bot.send_message(m.chat.id, "Сначала выбери раздел «📚 Учёба» в меню.", reply_markup=main_menu())
        return

    caption = m.caption or "Реши задание на фото по шагам. Если это не задание — опиши, что на фото, и ответь по сути."
    thinking = bot.send_message(m.chat.id, "💭 Смотрю фото...")

    try:
        file_info = bot.get_file(m.photo[-1].file_id)
        raw = bot.download_file(file_info.file_path)
        answer = ask_gemini(uid, caption, image_bytes=raw)
        remember(uid, f"[Фото] {caption}", answer)
    except Exception as e:
        try:
            bot.edit_message_text(f"Ошибка при разборе фото: {e}"[:300], m.chat.id, thinking.message_id)
        except Exception:
            pass
        return

    try:
        bot.delete_message(m.chat.id, thinking.message_id)
    except Exception:
        pass

    send_answer(m.chat.id, uid, answer)


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(m: types.Message):
    uid = m.from_user.id
    if user_sections.get(uid) != "study":
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

    send_answer(m.chat.id, uid, answer)


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
