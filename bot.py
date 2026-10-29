import os
import re
import io
import ast
import json
import hashlib
import html
import time
import base64
import threading
import requests
import telebot
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, wait, as_completed, FIRST_COMPLETED, TimeoutError as FutTimeout
from telebot import types
from flask import Flask, request as flask_request


def env(name):
    return (os.getenv(name) or "").strip() or None


# ============ КЛЮЧИ (Render -> Environment) ============
TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
GEMINI_API_KEY = env("GEMINI_API_KEY")
GROQ_KEY = env("GROQ_API_KEY")
CEREBRAS_KEY = env("CEREBRAS_API_KEY")
OPENROUTER_KEY = env("OPENROUTER_API_KEY")
CF_TOKEN = env("CLOUDFLARE_API_TOKEN")
CF_ACCOUNT = env("CLOUDFLARE_ACCOUNT_ID")
RENDER_EXTERNAL_URL = env("RENDER_EXTERNAL_URL")

# ============ МОДЕЛИ ============
FAST_MODELS = [env("FAST_MODEL") or "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.6-flash"]
DEEP_GEMINI = env("DEEP_MODEL") or "gemini-3.5-flash"

# (название, провайдер, модель)
CEREBRAS_GPT = ("Cerebras gpt-oss-120b", "cerebras", "gpt-oss-120b")
GROQ_GPT = ("Groq gpt-oss-120b", "groq", "openai/gpt-oss-120b")
GROQ_COMPOUND = ("Groq Compound (поиск)", "groq", "groq/compound")
OR_ULTRA = ("Nemotron 3 Ultra", "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free")
CF_KIMI = ("Kimi K2.6", "cloudflare", "@cf/moonshotai/kimi-k2.6")
CF_GLM = ("GLM 5.2", "cloudflare", "@cf/zai-org/glm-5.2")

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", GROQ_KEY),
    "cerebras": ("https://api.cerebras.ai/v1/chat/completions", CEREBRAS_KEY),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", OPENROUTER_KEY),
    "cloudflare": (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/v1/chat/completions",
        CF_TOKEN if CF_ACCOUNT else None,
    ),
}

# Высокий режим: эти модели отвечают параллельно, потом судья пишет итог
HIGH_MEMBERS = [OR_ULTRA, CF_KIMI, CF_GLM, CEREBRAS_GPT, GROQ_COMPOUND]

SYSTEM_PROMPT = (
    "Отвечай на русском, кратко и по делу. Пиши для Telegram: без LaTeX и знаков $, "
    "формулы только обычным текстом с символами Unicode (x², √, ×, ·, °, ≤, π). "
    "Без таблиц. Заголовки и важное выделяй **жирным**, списки делай через «•» или цифры."
)

MAX_HISTORY = 30
RESET_BUTTON = "🔄 Сбросить контекст"
MODE_BUTTONS = {"⚡ Обычный": "normal", "🧠 Высокий": "high"}
MODE_NAMES = {"normal": "⚡ Обычный", "high": "🧠 Высокий"}
IMG_BUTTON = "🎨 Картинка"
SEARCH_BUTTON = "🔎 Поиск"
pending = {}   # чат -> "img" / "search": что ждём от пользователя следующим сообщением
# адрес вебхука не содержит токен, чтобы он не светился в логах
WEBHOOK_PATH = hashlib.sha256((TELEGRAM_TOKEN or "").encode()).hexdigest()[:40]

histories = {}
modes = {}
lock = threading.Lock()

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
app = Flask(__name__)


# ============ КРАСИВОЕ ОФОРМЛЕНИЕ ДЛЯ TELEGRAM ============

SUP = dict(zip("0123456789+-=()nix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ"))
SUB = dict(zip("0123456789+-=()aehiklmnoprstux", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢₖₗₘₙₒₚᵣₛₜᵤₓ"))

SYMBOLS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "sim": "∼", "infty": "∞", "to": "→", "rightarrow": "→",
    "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "leftrightarrow": "↔",
    "sum": "∑", "prod": "∏", "int": "∫", "partial": "∂", "nabla": "∇", "in": "∈", "notin": "∉", "subset": "⊂",
    "cup": "∪", "cap": "∩", "forall": "∀", "exists": "∃", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "ldots": "…", "dots": "…", "cdots": "…", "circ": "°", "degree": "°", "prime": "′", "emptyset": "∅",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ",
    "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Pi": "Π", "Sigma": "Σ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω", "left": "", "right": "", "quad": " ", "qquad": "  ",
    "sin": "sin", "cos": "cos", "tan": "tan", "cot": "cot", "ln": "ln", "log": "log", "lim": "lim",
    "max": "max", "min": "min", "exp": "exp",
}


def _wrap(s):
    s = s.strip()
    return f"({s})" if re.search(r"[\s+\-−±*/]", s) else s


def _script(content, table, mark):
    content = content.strip()
    if content and all(c in table for c in content):
        return "".join(table[c] for c in content)
    return f"{mark}({content})" if len(content) > 1 else f"{mark}{content}"


def latex_to_text(s):
    s = s.replace("^{\\circ}", "°").replace("^\\circ", "°")
    for _ in range(4):
        s = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", lambda m: f"{_wrap(m.group(1))}/{_wrap(m.group(2))}", s)
        s = re.sub(r"\\sqrt\[(\d+)\]\{([^{}]*)\}", lambda m: f"{_script(m.group(1), SUP, '^')}√{_wrap(m.group(2))}", s)
        s = re.sub(r"\\sqrt\{([^{}]*)\}", lambda m: f"√{_wrap(m.group(1))}", s)
        s = re.sub(r"\\(?:text|mathrm|mathbf|mathit|operatorname|mathbb|mathcal|boldsymbol)\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\[,;:! ]", " ", s)
    s = s.replace("\\\\", " ").replace("\\%", "%").replace("\\{", "{").replace("\\}", "}")
    s = re.sub(r"\\([a-zA-Z]+)", lambda m: SYMBOLS.get(m.group(1), m.group(1)), s)
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: _script(m.group(1), SUP, "^"), s)
    s = re.sub(r"\^(.)", lambda m: _script(m.group(1), SUP, "^"), s)
    s = re.sub(r"_\{([^{}]*)\}", lambda m: _script(m.group(1), SUB, "_"), s)
    s = re.sub(r"_([0-9a-z])", lambda m: _script(m.group(1), SUB, "_"), s)
    s = s.replace("{", "").replace("}", "")
    return re.sub(r"[ \t]+", " ", s).strip()


def convert_math(t):
    for pat in (r"\$\$(.+?)\$\$", r"\\\[(.+?)\\\]"):
        t = re.sub(pat, lambda m: "\n" + latex_to_text(m.group(1)) + "\n", t, flags=re.S)
    t = re.sub(r"\\\((.+?)\\\)", lambda m: latex_to_text(m.group(1)), t, flags=re.S)
    t = re.sub(r"(?<![\\$\w])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\w$])", lambda m: latex_to_text(m.group(1)), t)
    return t


def to_tg_html(text):
    store = []

    def keep(h):
        store.append(h)
        return f"\x00{len(store) - 1}\x00"

    text = re.sub(r"```[a-zA-Z0-9_+-]*\n?(.*?)```",
                  lambda m: keep("<pre>" + html.escape(m.group(1).strip("\n")) + "</pre>"), text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: keep("<code>" + html.escape(m.group(1)) + "</code>"), text)
    text = convert_math(text)

    # таблицы -> моноширинный блок
    out, rows = [], []

    def flush():
        if rows:
            out.append(keep("<pre>" + html.escape("\n".join(rows)) + "</pre>"))
            rows.clear()

    for line in text.split("\n"):
        if re.match(r"^\s*\|.*\|\s*$", line):
            if re.match(r"^[\s|:\-]+$", line):
                continue
            rows.append(" | ".join(c.strip() for c in line.strip().strip("|").split("|")))
        else:
            flush()
            out.append(line)
    flush()
    text = "\n".join(out)

    text = html.escape(text, quote=False)
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", lambda m: "<b>" + m.group(1).replace("**", "") + "</b>", text, flags=re.M)
    text = re.sub(r"^(\s*)[*-]\s+", r"\1• ", text, flags=re.M)
    text = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*([^\s*][^*\n]*?)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^\s*([-*_])\1{2,}\s*$", "──────────", text, flags=re.M)
    return re.sub(r"\x00(\d+)\x00", lambda m: store[int(m.group(1))], text)


def split_text(text, limit=3500):
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 2 > limit and cur:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks or [""]


def send_answer(message, text, footer=""):
    parts = split_text(text)
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        try:
            body = to_tg_html(part)
            if last and footer:
                body += "\n\n<i>" + html.escape(footer.strip()) + "</i>"
            bot.reply_to(message, body, parse_mode="HTML", reply_markup=main_keyboard())
        except Exception as e:
            print("HTML SEND FAILED, plain text", e, flush=True)
            bot.reply_to(message, part + (f"\n\n{footer.strip()}" if last and footer else ""), reply_markup=main_keyboard())


# ============ ПАМЯТЬ И КНОПКИ ============

def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(*[types.KeyboardButton(t) for t in MODE_BUTTONS])
    kb.row(types.KeyboardButton(IMG_BUTTON), types.KeyboardButton(SEARCH_BUTTON))
    kb.row(types.KeyboardButton(RESET_BUTTON))
    return kb


def get_mode(chat_id):
    with lock:
        return modes.get(chat_id, "normal")


def get_history(chat_id):
    with lock:
        return list(histories.get(chat_id, []))


def save_turn(chat_id, user_text, answer):
    with lock:
        h = histories.get(chat_id, []) + [
            {"role": "user", "text": user_text},
            {"role": "assistant", "text": answer},
        ]
        h = h[-MAX_HISTORY:]
        if h and h[0]["role"] == "assistant":
            h = h[1:]
        histories[chat_id] = h


# ============ ВЫЗОВЫ НЕЙРОСЕТЕЙ ============

def call_gemini(model, contents, timeout=25, system=True):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    body = {"contents": contents}
    if system:
        body["systemInstruction"] = {"parts": [{"text": SYSTEM_PROMPT}]}
    try:
        res = requests.post(url, headers=headers, json=body, timeout=timeout).json()
    except Exception as e:
        print("GEMINI REQUEST ERROR", model, e, flush=True)
        return False, str(e), None
    try:
        parts = res["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        if text:
            return True, text, None
    except (KeyError, IndexError, TypeError):
        pass
    err = res.get("error", {}) if isinstance(res, dict) else {}
    print("GEMINI ERROR", model, err.get("code"), err.get("message"), flush=True)
    return False, f"{model}: {err.get('code')} {err.get('message', res)}", err.get("code")


def gemini_chain(models, contents, budget=50, per_call=25, system=True):
    """Пробует модели по очереди, но не дольше budget секунд. Возвращает (успех, текст, модель)."""
    t0, last = time.time(), ""
    for model in models:
        left = budget - (time.time() - t0)
        if left < 5:
            break
        ok, text, code = call_gemini(model, contents, min(per_call, left), system)
        if ok:
            return True, text, model
        last = text
        if code in (400, 401, 403):
            break
    return False, f"Ошибка Gemini: {last[:300]}", None


def call_openai_style(spec, messages, timeout=45):
    label, provider, model = spec
    url, key = PROVIDERS[provider]
    if not key:
        return False, f"{label}: нет ключа"
    try:
        res = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages},
            timeout=timeout,
        ).json()
    except Exception as e:
        print("API REQUEST ERROR", label, e, flush=True)
        return False, f"{label}: {e}"
    try:
        text = (res["choices"][0]["message"]["content"] or "").strip()
        if text:
            return True, text
    except (KeyError, IndexError, TypeError):
        pass
    print("API ERROR", label, str(res)[:300], flush=True)
    return False, f"{label}: {str(res)[:200]}"


def gemini_contents(history, parts):
    contents = [{"role": "user" if h["role"] == "user" else "model", "parts": [{"text": h["text"]}]} for h in history]
    contents.append({"role": "user", "parts": parts})
    return contents


def openai_messages(history, question):
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs += [{"role": h["role"], "content": h["text"]} for h in history]
    msgs.append({"role": "user", "content": question})
    return msgs


def user_parts(question, image):
    parts = [{"text": question}]
    if image:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image).decode()}})
    return parts


# ============ РЕЖИМЫ ============

def race(tasks, timeout):
    """Запускает все задачи сразу и возвращает первый успешный ответ."""
    pool = ThreadPoolExecutor(max_workers=len(tasks))
    futs = [pool.submit(t) for t in tasks]
    try:
        for f in as_completed(futs, timeout=timeout):
            try:
                ok, text, label = f.result()
            except Exception as e:
                print("RACE ERROR", e, flush=True)
                continue
            if ok:
                return True, text, label
    except FutTimeout:
        pass
    finally:
        pool.shutdown(wait=False)
    return False, "", ""


def run_normal(history, question, image=None):
    """Обычный режим: Gemini и быстрые Cerebras/Groq отвечают наперегонки, берём первый ответ."""
    contents = gemini_contents(history, user_parts(question, image))
    tasks = [lambda: gemini_chain(FAST_MODELS[:2], contents, budget=40, per_call=20)]
    if not image:
        msgs = openai_messages(history, question)
        for spec in (CEREBRAS_GPT, GROQ_GPT):
            if PROVIDERS[spec[1]][1]:
                tasks.append(lambda s=spec: (*call_openai_style(s, msgs, 40), s[0]))
    ok, text, _ = race(tasks, 45)
    if ok:
        return True, text, ""
    return False, "Ни одна модель не ответила вовремя. Попробуй ещё раз.", ""


def run_high(history, question, image=None):
    """Высокий режим: совет сильных моделей + судья, до ~3 минут."""
    t0 = time.time()
    if image:
        contents = gemini_contents(history, user_parts(question, image))
        ok, text, used = gemini_chain([DEEP_GEMINI] + FAST_MODELS, contents, budget=120, per_call=60)
        return ok, text, (f"🧠 {used}" if ok else "")

    msgs = openai_messages(history, question)
    members = [s for s in HIGH_MEMBERS if PROVIDERS[s[1]][1]]
    answers = {}
    if members:
        pool = ThreadPoolExecutor(max_workers=len(members))
        futs = {pool.submit(call_openai_style, s, msgs, 80): s[0] for s in members}
        waiting, second = set(futs), None
        while waiting and time.time() - t0 < 85:
            done, waiting = wait(waiting, timeout=2, return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    ok, text = f.result()
                    if ok:
                        answers[futs[f]] = text
                except Exception as e:
                    print("MEMBER ERROR", futs[f], e, flush=True)
            if len(answers) >= 3:
                break
            if len(answers) >= 2:
                second = second or time.time()
                if time.time() - second > 12:   # не ждём самых медленных
                    break
        pool.shutdown(wait=False)

    if not answers:
        ok, text, _ = run_normal(history, question)
        return ok, text, ("⚡ Высокий режим сейчас недоступен, ответил обычный" if ok else "")
    if len(answers) == 1:
        label, text = next(iter(answers.items()))
        return True, text, f"🧠 Ответила только: {label}"

    joined = "\n\n".join(f"=== Ответ {i} ===\n{t[:5000]}" for i, t in enumerate(answers.values(), 1))
    prompt = (
        "Ты судья. Ниже вопрос и ответы нескольких нейросетей. Найди, где они согласны и где спорят, "
        "проверь спорное своим рассуждением, исправь ошибки и напиши ОДИН лучший итоговый ответ "
        "(на языке вопроса). Не упоминай нейросети и свой процесс, только сам ответ.\n\n"
        f"ВОПРОС:\n{question}\n\n{joined}"
    )
    footer = f"🧠 Совет ({len(answers)}): " + ", ".join(answers)

    def left():
        return max(10, 170 - (time.time() - t0))

    ok, text, _ = gemini_chain([DEEP_GEMINI], [{"role": "user", "parts": [{"text": prompt}]}], budget=left(), per_call=min(60, left()))
    if ok:
        return True, text, footer + " → судья Gemini 3.5"
    for spec in (CEREBRAS_GPT, GROQ_GPT):
        ok, text = call_openai_style(spec, [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}], min(50, left()))
        if ok:
            return True, text, footer + f" → судья {spec[0]}"
    label, text = next(iter(answers.items()))
    return True, text, f"🧠 Судья недоступен, ответ: {label}"


RUNNERS = {"normal": run_normal, "high": run_high}


def process_question(message, question, image=None):
    chat_id = message.chat.id
    mode = get_mode(chat_id)
    bot.send_chat_action(chat_id, "typing")
    wait_msg = None
    if mode == "high" and not image:
        wait_msg = bot.send_message(chat_id, "🧠 Высокий режим: думаю, до 3 минут…")
    ok, text, footer = RUNNERS[mode](get_history(chat_id), question, image)
    if wait_msg:
        try:
            bot.delete_message(chat_id, wait_msg.message_id)
        except Exception:
            pass
    if ok:
        save_turn(chat_id, question if not image else f"[Я отправил фото] {question}", text)
        send_answer(message, text, footer)
    else:
        bot.reply_to(message, text[:1500], reply_markup=main_keyboard())


# ============ КАРТИНКИ ============

DRAW_RE = re.compile(
    r"(нарису\w*|изобрази\w*"
    r"|сгенерируй\w*\s+(?:\w+\s+){0,2}(?:картин|изображ|фото|арт|логотип|иллюстрац)\w*"
    r"|(?:создай|сделай|дай|покажи)\s+(?:\w+\s+){0,2}(?:картинк|изображени|логотип|иллюстраци|обои)\w*)",
    re.I,
)


def draw_image(prompt):
    """Cloudflare FLUX, запасной вариант Gemini. Возвращает байты картинки или None."""
    if CF_TOKEN and CF_ACCOUNT:
        try:
            url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/@cf/black-forest-labs/flux-1-schnell"
            r = requests.post(url, headers={"Authorization": f"Bearer {CF_TOKEN}"}, json={"prompt": prompt, "steps": 6}, timeout=60)
            if r.headers.get("content-type", "").startswith("image"):
                return r.content
            data = r.json()
            b64 = ((data or {}).get("result") or {}).get("image")
            if b64:
                return base64.b64decode(b64)
            print("CF IMG ERROR", str(data)[:300], flush=True)
        except Exception as e:
            print("CF IMG EXCEPTION", e, flush=True)
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent"
        res = requests.post(
            url,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}},
            timeout=60,
        ).json()
        for part in res["candidates"][0]["content"]["parts"]:
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])
        print("GEMINI IMG ERROR", str(res)[:300], flush=True)
    except Exception as e:
        print("GEMINI IMG EXCEPTION", e, flush=True)
    return None


def do_image(message, text):
    chat_id = message.chat.id
    bot.send_chat_action(chat_id, "upload_photo")
    ask = ("Переведи на английский и сделай из этого подробный промпт для генератора картинок "
           "(до 60 слов). Верни только промпт.\n" + text)
    ok, en, _ = call_gemini(FAST_MODELS[0], [{"role": "user", "parts": [{"text": ask}]}], 12, system=False)
    img = draw_image((en if ok else text)[:900])
    if img:
        bot.send_photo(chat_id, img, reply_to_message_id=message.message_id, reply_markup=main_keyboard())
    else:
        bot.reply_to(message, "Не получилось нарисовать. Проверь в Render токен и Account ID Cloudflare (смотри Logs).", reply_markup=main_keyboard())


# ============ ГРАФИКИ ============

GRAPH_RE = re.compile(
    r"((?:постро\w*|нарису\w*|начерти\w*|изобрази\w*|покаж\w*|сделай|дай|выведи)\W+(?:\w+\W+){0,4}?"
    r"(?:график|диаграмм|парабол|гипербол|синусоид|окружност)"
    r"|график\w*[^\n]{0,30}(?:y\s*=|f\s*\(\s*x))",
    re.I,
)


def safe_eval(expr, variables):
    """Безопасно считает формулу: только числа, x/y, + - * / ** и простые функции."""
    import numpy as np
    funcs = {
        "sin": np.sin, "cos": np.cos, "tan": np.tan, "asin": np.arcsin, "acos": np.arccos, "atan": np.arctan,
        "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh, "exp": np.exp, "log": np.log, "ln": np.log,
        "log10": np.log10, "log2": np.log2, "sqrt": np.sqrt, "abs": np.abs,
    }
    consts = {"pi": np.pi, "e": np.e}
    expr = (expr.replace("^", "**").replace("√", "sqrt").replace("π", "pi")
            .replace("·", "*").replace("×", "*").replace("−", "-"))
    tree = ast.parse(expr, mode="eval")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return np.float64(n.value)
        if isinstance(n, ast.Name):
            if n.id in variables:
                return variables[n.id]
            if n.id in consts:
                return consts[n.id]
            raise ValueError("unknown name " + n.id)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                return a / b
            if isinstance(n.op, ast.Pow):
                return np.power(a, b)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in funcs and len(n.args) == 1 and not n.keywords:
            return funcs[n.func.id](ev(n.args[0]))
        raise ValueError("bad expression")

    return ev(tree)


def make_plot(spec):
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x0, x1 = float(spec.get("x_min", -10)), float(spec.get("x_max", 10))
    if not x0 < x1:
        x0, x1 = -10.0, 10.0
    has_y = spec.get("y_min") is not None and spec.get("y_max") is not None
    y0, y1 = (float(spec["y_min"]), float(spec["y_max"])) if has_y else (x0, x1)

    fig, ax = plt.subplots(figsize=(8, 6), dpi=110)
    ax.axhline(0, color="black", lw=0.8)
    ax.axvline(0, color="black", lw=0.8)
    ax.grid(alpha=0.3)
    n_lines, ys_all, kinds = 0, [], set()
    with np.errstate(all="ignore"):
        for s in (spec.get("series") or [])[:6]:
            kind, label = s.get("type"), s.get("label") or s.get("expr") or ""
            if kind == "func":
                x = np.linspace(x0, x1, 2000)
                y = safe_eval(s["expr"], {"x": x}) * np.ones_like(x)
                y = np.where(np.abs(y) > 1e6, np.nan, y)
                ax.plot(x, y, label=label)
                ys_all.append(y[np.isfinite(y)])
            elif kind == "implicit":
                e = s["expr"]
                if "=" in e:
                    left, right = e.split("=", 1)
                    e = f"({left})-({right})"
                X, Y = np.meshgrid(np.linspace(x0, x1, 700), np.linspace(y0, y1, 700))
                Z = safe_eval(e, {"x": X, "y": Y}) * np.ones_like(X)
                ax.contour(X, Y, Z, levels=[0], colors=[f"C{n_lines}"])
                ax.plot([], [], color=f"C{n_lines}", label=label)
            elif kind == "points":
                xs = [float(v) for v in s["x"][:200]]
                ys = [float(v) for v in s["y"][:200]]
                ax.plot(xs, ys, "o-", label=label)
            else:
                continue
            kinds.add(kind)
            n_lines += 1
    if n_lines == 0:
        raise ValueError("empty plot")
    ax.set_xlim(x0, x1)
    if has_y:
        ax.set_ylim(y0, y1)
    elif ys_all and kinds == {"func"}:
        allv = np.concatenate(ys_all)
        if allv.size:
            lo, hi = np.percentile(allv, [1, 99])
            if hi > lo:
                pad = 0.15 * (hi - lo)
                ax.set_ylim(lo - pad, hi + pad)
    if kinds == {"implicit"}:
        ax.set_aspect("equal")
    if spec.get("title"):
        ax.set_title(str(spec["title"]))
    ax.legend()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def do_graph(message, text):
    """Строит график по запросу. Возвращает True, если график отправлен."""
    chat_id = message.chat.id
    bot.send_chat_action(chat_id, "upload_photo")
    prompt = (
        "Пользователь просит построить график. Верни ТОЛЬКО JSON без пояснений и без ```:\n"
        '{"title":"...","x_min":-10,"x_max":10,"y_min":null,"y_max":null,'
        '"series":[{"type":"func","expr":"x**2-4","label":"y = x² − 4"},'
        '{"type":"implicit","expr":"x**2+y**2=9","label":"x² + y² = 9"},'
        '{"type":"points","x":[1,2,3],"y":[2,4,3],"label":"данные"}],'
        '"caption":"1-2 предложения на русском: что изображено, ключевые точки"}\n'
        "Правила: в expr только x (и y для implicit), операции + - * / **, умножение всегда знаком *, "
        "функции sin cos tan asin acos atan exp log sqrt abs, константы pi e. "
        "Выбери x_min и x_max так, чтобы было видно главное. Используй нужные типы серий, лишние не добавляй.\n"
        f"Запрос: {text}"
    )
    ok, raw, _ = gemini_chain(FAST_MODELS[:2], [{"role": "user", "parts": [{"text": prompt}]}], budget=35, per_call=20, system=False)
    if not ok:
        return False
    try:
        spec = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        png = make_plot(spec)
    except ImportError:
        bot.reply_to(message, "Для графиков добавь строки numpy и matplotlib в requirements.txt на GitHub.", reply_markup=main_keyboard())
        return True
    except Exception as e:
        print("GRAPH ERROR", e, flush=True)
        return False
    bot.send_photo(chat_id, png, caption=str(spec.get("caption") or "")[:900] or None,
                   reply_to_message_id=message.message_id, reply_markup=main_keyboard())
    return True


# ============ ПОИСК ============

def do_search(message, question):
    if not GROQ_KEY:
        bot.reply_to(message, "Для поиска нужен ключ GROQ_API_KEY в Render -> Environment.")
        return
    chat_id = message.chat.id
    bot.send_chat_action(chat_id, "typing")
    ok, text = call_openai_style(GROQ_COMPOUND, openai_messages(get_history(chat_id), question), 60)
    if ok:
        save_turn(chat_id, question, text)
        send_answer(message, text, "🔎 Groq Compound")
    else:
        bot.reply_to(message, f"Поиск не сработал: {text[:300]}")


# ============ TELEGRAM ============

@app.route(f"/{WEBHOOK_PATH}", methods=["POST"])
def webhook():
    if flask_request.headers.get("content-type") == "application/json":
        update = telebot.types.Update.de_json(flask_request.get_data().decode("utf-8"))
        threading.Thread(target=bot.process_new_updates, args=([update],), daemon=True).start()
        return "", 200
    return "Forbidden", 403


@app.route("/")
def index():
    return "Bot is running perfectly!", 200


@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(
        message,
        "Здарова! Я помню наш разговор. Кнопки внизу:\n\n"
        "⚡ Обычный: быстрый ответ, до 1 минуты\n"
        "🧠 Высокий: несколько сильных нейросетей думают вместе, до 3 минут\n"
        "🎨 Картинка и 🔎 Поиск: нажми и напиши, что нужно\n\n"
        "Рисую и без кнопок: просто напиши «нарисуй …» или «построй график …».",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["reset"])
@bot.message_handler(func=lambda m: m.text == RESET_BUTTON, content_types=["text"])
def reset_context(message):
    with lock:
        histories.pop(message.chat.id, None)
        pending.pop(message.chat.id, None)
    bot.reply_to(message, "Готово, контекст сброшен.", reply_markup=main_keyboard())


@bot.message_handler(func=lambda m: m.text in MODE_BUTTONS, content_types=["text"])
def switch_mode(message):
    mode = MODE_BUTTONS[message.text]
    with lock:
        modes[message.chat.id] = mode
        pending.pop(message.chat.id, None)
    bot.reply_to(message, f"Режим: {MODE_NAMES[mode]}", reply_markup=main_keyboard())


@bot.message_handler(func=lambda m: m.text == IMG_BUTTON, content_types=["text"])
def img_button(message):
    with lock:
        pending[message.chat.id] = "img"
    bot.reply_to(message, "Напиши, что нарисовать. Например: рыжий кот в космосе", reply_markup=main_keyboard())


@bot.message_handler(func=lambda m: m.text == SEARCH_BUTTON, content_types=["text"])
def search_button(message):
    with lock:
        pending[message.chat.id] = "search"
    bot.reply_to(message, "Напиши, что найти в интернете. Например: курс доллара сегодня", reply_markup=main_keyboard())


@bot.message_handler(commands=["search"])
def cmd_search(message):
    q = message.text.partition(" ")[2].strip()
    if q:
        do_search(message, q)
    else:
        search_button(message)


@bot.message_handler(commands=["img"])
def cmd_img(message):
    q = message.text.partition(" ")[2].strip()
    if q:
        do_image(message, q)
    else:
        img_button(message)


@bot.message_handler(content_types=["text"])
def handle_text(message):
    text = message.text
    with lock:
        wait_for = pending.pop(message.chat.id, None)
    if wait_for == "img":
        do_image(message, text)
    elif wait_for == "search":
        do_search(message, text)
    elif GRAPH_RE.search(text) and do_graph(message, text):
        pass
    elif DRAW_RE.search(text):
        do_image(message, text)
    else:
        process_question(message, text)


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    try:
        info = bot.get_file(message.photo[-1].file_id)
        img = bot.download_file(info.file_path)
        process_question(message, message.caption or "Опиши что на фото и ответь на русском", img)
    except Exception as e:
        print("PHOTO ERROR", e, flush=True)
        bot.reply_to(message, f"Ошибка обработки фото: {e}")


# ============ ЗАПУСК ============

print("Cloudflare Account ID length:", len(CF_ACCOUNT or ""), "(должно быть 32)", flush=True)

if RENDER_EXTERNAL_URL:
    bot.remove_webhook()
    bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/{WEBHOOK_PATH}")
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Начать"),
            types.BotCommand("reset", "Сбросить контекст"),
            types.BotCommand("search", "Поиск в интернете"),
            types.BotCommand("img", "Нарисовать картинку"),
        ])
    except Exception as e:
        print("COMMANDS ERROR", e, flush=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
