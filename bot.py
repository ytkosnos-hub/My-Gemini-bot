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
from collections import deque, OrderedDict
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
BOT_NAME = env("BOT_NAME") or "Mewyto"          # имя бота (можно сменить в Render -> Environment)
ADMIN_ID = int(env("ADMIN_ID")) if (env("ADMIN_ID") or "").isdigit() else None   # твой Telegram ID
ALLOWED = {int(x) for x in re.findall(r"\d+", env("ALLOWED_IDS") or "")}      # если заполнено, бот только для этих ID
RATE_LIMIT = 10                                  # сообщений в минуту от одного пользователя

# ============ МОДЕЛИ ============
FAST_MODELS = [env("FAST_MODEL") or "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.6-flash"]
DEEP_GEMINI = env("DEEP_MODEL") or "gemini-3.5-flash"
PHOTO_MODELS = [env("PHOTO_MODEL") or "gemini-3.6-flash", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]

# (название, провайдер, модель)
CEREBRAS_GPT = ("Cerebras gpt-oss-120b", "cerebras", "gpt-oss-120b")
GROQ_GPT = ("Groq gpt-oss-120b", "groq", "openai/gpt-oss-120b")
GROQ_COMPOUND = ("Groq Compound (поиск)", "groq", "groq/compound")
OR_ULTRA = ("Nemotron 3 Ultra", "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free")
CF_GPT = ("CF gpt-oss-120b", "cloudflare", "@cf/openai/gpt-oss-120b")
CF_NEMOTRON = ("Nemotron 120B", "cloudflare", "@cf/nvidia/nemotron-3-120b-a12b")
CF_LLAMA = ("Llama 3.3 70B", "cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
BLOCKED = set()   # модели, недоступные на бесплатном тарифе: после первой ошибки больше не вызываем

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
HIGH_MEMBERS = [OR_ULTRA, CF_GPT, CF_NEMOTRON, CF_LLAMA, CEREBRAS_GPT, GROQ_COMPOUND]

SYSTEM_PROMPT = (
    "Тебя зовут Mewyto, ты ИИ-ассистент. Отвечай на русском, по делу, одним конкретным ответом "
    "без вариантов «или» и лишних оговорок. Если просят решить задание, решай его полностью: все пункты "
    "и подпункты по порядку, с краткими шагами и ответом. Если дано фото с заданием, сразу решай его, "
    "а не описывай фото. Пиши для Telegram: без LaTeX и знаков $, формулы обычным текстом с Unicode "
    "(x², √, ×, ·, °, ≤, π). Таблицы используй только когда они реально нужны (сравнение, расписание, данные), в формате Markdown. Важное выделяй **жирным**, списки делай через «•» или цифры. "
    "Химия: индексы нижние (H₂SO₄), заряды ионов верхние (Na⁺, SO₄²⁻), коэффициенты обычными цифрами "
    "(2NaOH + H₂SO₄ → Na₂SO₄ + 2H₂O), осадок ↓, газ ↑."
)
ERR_TEXT = "Не получилось ответить. Попробуй ещё раз через минуту."
SYSTEM_PROMPT = SYSTEM_PROMPT.replace("Mewyto", BOT_NAME)

MAX_HISTORY = 30
RESET_BUTTON = "🔄 Сбросить контекст"
MODE_BUTTONS = {"⚡ Обычный": "normal", "🧠 Высокий": "high"}
MODE_NAMES = {"normal": "⚡ Обычный", "high": "🧠 Высокий"}
IMG_BUTTON = "🎨 Картинка"
SEARCH_BUTTON = "🔎 Поиск"
PROMPT_BUTTON = "📝 Промты"
PDF_BUTTON = "📄 В PDF"
last_answers = {}     # чат -> (вопрос, ответ) для кнопки PDF
custom_prompts = {}   # чат -> инструкция пользователя («промт»)
seen_updates = deque(maxlen=300)
pending = {}   # чат -> "img" / "search": что ждём от пользователя следующим сообщением
# адрес вебхука не содержит токен, чтобы он не светился в логах
WEBHOOK_PATH = hashlib.sha256((TELEGRAM_TOKEN or "").encode()).hexdigest()[:40]
SECRET = hashlib.sha256(("secret" + (TELEGRAM_TOKEN or "")).encode()).hexdigest()[:32]
secret_ok = False

histories = {}
modes = {}
lock = threading.Lock()

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
app = Flask(__name__)
http = requests.Session()   # одно соединение на все запросы: быстрее


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


ELEMENTS = set("H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr".split())
SINGLE_OK = {"H2", "O2", "O3", "N2", "F2", "Cl2", "Br2", "I2", "P4", "S8"}
SUB_DIGITS = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
SUP_CHARGE = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")
CHEM_RE = re.compile(r"(?<![A-Za-z\d])(\d*)((?:[A-Z][a-z]?\d{0,2}|\((?:[A-Z][a-z]?\d{0,2})+\)\d{0,2})+)(?![A-Za-z\d])")


def chem_format(text):
    """H2SO4 -> H₂SO₄, Na(+) -> Na⁺, SO4(2-) -> SO₄²⁻. Коэффициенты (2NaOH) остаются обычными."""
    def formula(m):
        coef, f = m.group(1), m.group(2)
        if not re.search(r"\d", f):
            return m.group(0)
        if not all(sym in ELEMENTS for sym in re.findall(r"[A-Z][a-z]?", f)):
            return m.group(0)
        if len(re.findall(r"[A-Z]", f)) == 1 and "(" not in f and f not in SINGLE_OK:
            return m.group(0)
        return coef + f.translate(SUB_DIGITS)

    text = CHEM_RE.sub(formula, text)
    text = re.sub(r"(?<=[A-Za-z₀-₉\)])\((\d?[+-])\)", lambda m: m.group(1).translate(SUP_CHARGE), text)
    text = re.sub(r"(?<=[A-Za-z₀-₉\)])\^\{?(\d?[+-])\}?", lambda m: m.group(1).translate(SUP_CHARGE), text)
    return text


def to_tg_html(text):
    store = []

    def keep(h):
        store.append(h)
        return f"\x00{len(store) - 1}\x00"

    text = re.sub(r"```[a-zA-Z0-9_+-]*\n?(.*?)```",
                  lambda m: keep("<pre>" + html.escape(m.group(1).strip("\n")) + "</pre>"), text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: keep("<code>" + html.escape(m.group(1)) + "</code>"), text)
    text = chem_format(convert_math(text))

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


TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
PDF_RE = re.compile(r"\b(pdf|пдф)\b", re.I)


def split_tables(text):
    """Вынимает markdown-таблицы из текста. Возвращает (текст без таблиц, [таблицы])."""
    out, tables, cur, in_code = [], [], [], False

    def flush():
        if cur:
            rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in cur if not re.match(r"^[\s|:\-]+$", ln)]
            if len(rows) >= 2:
                tables.append(rows)
            else:
                out.extend(cur)
            cur.clear()

    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
        if not in_code and TABLE_LINE.match(line):
            cur.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out).strip(), tables


def clean_cell(c):
    return chem_format(convert_math(c)).replace("**", "").replace("`", "")


def table_png(rows):
    import textwrap
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n = max(len(r) for r in rows)
    cells = [[textwrap.fill(clean_cell(c), 26) for c in r] + [""] * (n - len(r)) for r in rows]
    lines = [max(c.count("\n") + 1 for c in r) for r in cells]
    fig, ax = plt.subplots(figsize=(min(12, max(5, 2.6 * n)), max(1.4, 0.42 * sum(lines) + 0.4)), dpi=130)
    ax.axis("off")
    tbl = ax.table(cellText=cells[1:], colLabels=cells[0], loc="center", cellLoc="left")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10)
    tbl.auto_set_column_width(list(range(n)))
    for (r, c), cell in tbl.get_celld().items():
        cell.set_height(lines[r] / sum(lines))
        cell.set_edgecolor("#b0b7c3")
        if r == 0:
            cell.set_facecolor("#2b5797")
            cell.get_text().set_color("white")
            cell.get_text().set_fontweight("bold")
        elif r % 2 == 0:
            cell.set_facecolor("#eef2f8")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    return buf.getvalue()


def send_answer(message, text, footer=""):
    chat_id = message.chat.id
    body, tables = split_tables(text)
    pngs = []
    try:
        pngs = [table_png(t) for t in tables]
    except Exception as e:
        print("TABLE IMAGE ERROR", repr(e), flush=True)
        pngs, body = [], text    # не вышло: оставляем таблицы текстом
    if not pngs:
        body = text
    if body.strip():
        parts = split_text(body)
        for i, part in enumerate(parts):
            last = i == len(parts) - 1 and not pngs
            try:
                msg_html = to_tg_html(part)
                if last and footer:
                    msg_html += "\n\n<i>" + html.escape(footer.strip()) + "</i>"
                bot.reply_to(message, msg_html, parse_mode="HTML", reply_markup=main_keyboard(chat_id))
            except Exception as e:
                print("HTML SEND FAILED, plain text", e, flush=True)
                bot.reply_to(message, part, reply_markup=main_keyboard(chat_id))
    for png in pngs:
        bot.send_photo(chat_id, png, reply_markup=main_keyboard(chat_id))


# ============ PDF ============

def build_pdf(title, text):
    import matplotlib
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Preformatted
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    fdir = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        for name, fn in (("DejaVu", "DejaVuSans.ttf"), ("DejaVu-Bold", "DejaVuSans-Bold.ttf"),
                         ("DejaVu-Italic", "DejaVuSans-Oblique.ttf"), ("DejaVu-BoldItalic", "DejaVuSans-BoldOblique.ttf"),
                         ("DejaVuMono", "DejaVuSansMono.ttf")):
            pdfmetrics.registerFont(TTFont(name, os.path.join(fdir, fn)))
        pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold", italic="DejaVu-Italic", boldItalic="DejaVu-BoldItalic")

    body = ParagraphStyle("body", fontName="DejaVu", fontSize=10.5, leading=15, spaceAfter=3)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=12, bulletIndent=2)
    h1 = ParagraphStyle("h1", parent=body, fontName="DejaVu-Bold", fontSize=15, leading=20, spaceBefore=6, spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=body, fontName="DejaVu-Bold", fontSize=12.5, leading=17, spaceBefore=6, spaceAfter=4)
    cell = ParagraphStyle("cell", parent=body, fontSize=9.5, leading=12.5, spaceAfter=0)
    code = ParagraphStyle("code", fontName="DejaVuMono", fontSize=8.5, leading=11, backColor=colors.HexColor("#f3f4f6"), leftIndent=4)

    def inline(t):
        t = html.escape(t, quote=False)
        t = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<![\w*])\*([^\s*][^*\n]*?)\*(?![\w*])", r"<i>\1</i>", t)
        return re.sub(r"`([^`\n]+)`", r'<font face="DejaVuMono">\1</font>', t)

    def para(t, style):
        try:
            return Paragraph(inline(t), style)
        except Exception:
            return Paragraph(html.escape(t, quote=False), style)

    text = chem_format(convert_math(text))
    story = [para(title[:200], h1), Spacer(1, 4)]
    lines, i = text.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            story.append(Preformatted("\n".join(buf), code))
        elif TABLE_LINE.match(ln):
            rows = []
            while i < len(lines) and TABLE_LINE.match(lines[i]):
                if not re.match(r"^[\s|:\-]+$", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            i -= 1
            n = max(len(r) for r in rows)
            data = [[para(c, cell) for c in r] + [""] * (n - len(r)) for r in rows]
            t = Table(data, colWidths=[(A4[0] - 36 * mm) / n] * n, repeatRows=1)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#b0b7c3")),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbe4f3")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story += [t, Spacer(1, 6)]
        elif re.match(r"^\s{0,3}#{1,6}\s", ln):
            lvl = len(ln.strip().split(" ")[0])
            story.append(para(ln.strip().lstrip("#").strip().replace("**", ""), h1 if lvl == 1 else h2))
        elif re.match(r"^\s*[-*•]\s+", ln):
            story.append(Paragraph(inline(re.sub(r"^\s*[-*•]\s+", "", ln)), bullet, bulletText="•"))
        elif not ln.strip():
            story.append(Spacer(1, 5))
        else:
            story.append(para(ln, body))
        i += 1
    out = io.BytesIO()
    SimpleDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                      title=title[:100], author=BOT_NAME).build(story)
    return out.getvalue()


def send_pdf(message):
    chat_id = message.chat.id
    last = last_answers.get(chat_id)
    if not last:
        bot.reply_to(message, "Пока нечего отправлять в PDF. Сначала задай вопрос.", reply_markup=main_keyboard(chat_id))
        return
    question, answer = last
    try:
        data = build_pdf(question, answer)
        f = io.BytesIO(data)
        f.name = (re.sub(r"[^\w]+", "_", question, flags=re.U)[:30].strip("_") or "answer") + ".pdf"
        bot.send_document(chat_id, f, reply_markup=main_keyboard(chat_id))
    except Exception as e:
        print("PDF ERROR", repr(e), flush=True)
        bot.reply_to(message, "Не получилось сделать PDF. Попробуй позже.", reply_markup=main_keyboard(chat_id))


# ============ ПАМЯТЬ И КНОПКИ ============

def main_keyboard(chat_id=None):
    n = len(histories.get(chat_id, [])) if chat_id else 0
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(*[types.KeyboardButton(t) for t in MODE_BUTTONS])
    kb.row(types.KeyboardButton(IMG_BUTTON), types.KeyboardButton(SEARCH_BUTTON))
    kb.row(types.KeyboardButton(PROMPT_BUTTON), types.KeyboardButton(PDF_BUTTON))
    kb.row(types.KeyboardButton(f"{RESET_BUTTON} · {n}/{MAX_HISTORY}"))
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
        last_answers[chat_id] = (user_text, answer)


# ============ ВЫЗОВЫ НЕЙРОСЕТЕЙ ============

def call_gemini(model, contents, timeout=25, system=True, tools=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    body = {"contents": contents}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system if isinstance(system, str) else SYSTEM_PROMPT}]}
    if tools:
        body["tools"] = tools
    try:
        res = http.post(url, headers=headers, json=body, timeout=timeout).json()
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


def call_openai_style(spec, messages, timeout=45, extra=None, max_tokens=None):
    label, provider, model = spec
    url, key = PROVIDERS[provider]
    if not key:
        return False, f"{label}: нет ключа"
    if model in BLOCKED:
        return False, f"{label}: недоступна"
    res = {}
    for ex in ([extra, None] if extra else [None]):
        payload = {"model": model, "messages": messages}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if ex:
            payload.update(ex)
        try:
            r = http.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json=payload, timeout=timeout)
            res = r.json()
        except Exception as e:
            print("API REQUEST ERROR", label, e, flush=True)
            return False, f"{label}: {e}"
        try:
            text = (res["choices"][0]["message"]["content"] or "").strip()
            if text:
                return True, text
        except (KeyError, IndexError, TypeError):
            pass
        if getattr(r, "status_code", None) != 400:
            break   # повторяем без доп. параметров только при ошибке 400
    if "Free plan" in str(res) or "not available on the" in str(res):
        BLOCKED.add(model)
    print("API ERROR", label, str(res)[:300], flush=True)
    return False, f"{label}: {str(res)[:200]}"


def gemini_contents(history, parts):
    contents = [{"role": "user" if h["role"] == "user" else "model", "parts": [{"text": h["text"]}]} for h in history]
    contents.append({"role": "user", "parts": parts})
    return contents


def openai_messages(history, question, sysmsg=None):
    msgs = [{"role": "system", "content": sysmsg or SYSTEM_PROMPT}]
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


def sysmsg_for(chat_id):
    with lock:
        extra = custom_prompts.get(chat_id)
    return SYSTEM_PROMPT + (f"\n\nИнструкция пользователя, выполняй её всегда: {extra}" if extra else "")


def run_normal(history, question, image=None, sysmsg=None):
    """Обычный режим: Gemini и быстрые Cerebras/Groq отвечают наперегонки, берётся первый ответ."""
    sysmsg = sysmsg or SYSTEM_PROMPT
    if image:
        contents = gemini_contents(history, user_parts(question, image))
        ok, text, _ = gemini_chain(PHOTO_MODELS, contents, budget=55, per_call=35, system=sysmsg)
        return ok, (text if ok else ""), ""
    contents = gemini_contents(history, user_parts(question, None))
    tasks = [lambda: gemini_chain(FAST_MODELS[:2], contents, budget=40, per_call=20, system=sysmsg)]
    msgs = openai_messages(history, question, sysmsg)
    for spec in (CEREBRAS_GPT, GROQ_GPT):
        if PROVIDERS[spec[1]][1]:
            tasks.append(lambda s=spec: (*call_openai_style(s, msgs, 40, {"reasoning_effort": "low"}, 1800), s[0]))
    ok, text, _ = race(tasks, 45)
    return (True, text, "") if ok else (False, "", "")


def run_high(history, question, image=None, sysmsg=None):
    """Высокий режим: совет сильных моделей + судья, до ~3 минут."""
    sysmsg = sysmsg or SYSTEM_PROMPT
    t0 = time.time()
    if image:
        contents = gemini_contents(history, user_parts(question, image))
        ok, text, _ = gemini_chain([DEEP_GEMINI] + PHOTO_MODELS, contents, budget=150, per_call=70, system=sysmsg)
        return ok, (text if ok else ""), ""

    msgs = openai_messages(history, question, sysmsg)
    members = [s for s in HIGH_MEMBERS if PROVIDERS[s[1]][1] and s[2] not in BLOCKED]
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
                if time.time() - second > 12:
                    break
        pool.shutdown(wait=False)

    if not answers:
        return run_normal(history, question, None, sysmsg)
    if len(answers) == 1:
        return True, next(iter(answers.values())), ""

    joined = "\n\n".join(f"=== Ответ {i} ===\n{t[:5000]}" for i, t in enumerate(answers.values(), 1))
    prompt = (
        "Ты судья. Ниже вопрос и ответы нескольких нейросетей. Найди, где они согласны и где спорят, "
        "проверь спорное своим рассуждением, исправь ошибки и напиши ОДИН лучший итоговый ответ "
        "(на языке вопроса). Не упоминай нейросети и свой процесс, только сам ответ.\n\n"
        f"ВОПРОС:\n{question}\n\n{joined}"
    )

    def left():
        return max(10, 170 - (time.time() - t0))

    ok, text, _ = gemini_chain([DEEP_GEMINI], [{"role": "user", "parts": [{"text": prompt}]}], budget=left(), per_call=min(60, left()), system=sysmsg)
    if ok:
        return True, text, ""
    for spec in (CEREBRAS_GPT, GROQ_GPT):
        ok, text = call_openai_style(spec, [{"role": "system", "content": sysmsg}, {"role": "user", "content": prompt}], min(50, left()))
        if ok:
            return True, text, ""
    return True, next(iter(answers.values())), ""


RUNNERS = {"normal": run_normal, "high": run_high}


def process_question(message, question):
    chat_id = message.chat.id
    mode = get_mode(chat_id)
    bot.send_chat_action(chat_id, "typing")
    wait_msg = None
    if mode == "high":
        wait_msg = bot.send_message(chat_id, "🧠 Высокий режим: думаю, до 3 минут…")
    ok, text, _ = RUNNERS[mode](get_history(chat_id), question, None, sysmsg_for(chat_id))
    if wait_msg:
        try:
            bot.delete_message(chat_id, wait_msg.message_id)
        except Exception:
            pass
    if ok:
        save_turn(chat_id, question, text)
        send_answer(message, text)
        if PDF_RE.search(question):
            send_pdf(message)
    else:
        bot.reply_to(message, ERR_TEXT, reply_markup=main_keyboard(message.chat.id))


def process_photo(message, img, caption):
    """Фото: сразу решаем задание; если в задании нужен график, присылаем и его."""
    chat_id = message.chat.id
    mode = get_mode(chat_id)
    task = caption or "Реши все задания на фото полностью, по порядку. Не описывай фото."
    bot.send_chat_action(chat_id, "typing")
    pool = ThreadPoolExecutor(max_workers=2)
    f_sol = pool.submit(RUNNERS[mode], get_history(chat_id), task, img, sysmsg_for(chat_id))
    f_gr = pool.submit(graph_from_image, img, caption)
    try:
        ok, text, _ = f_sol.result(timeout=170)
    except Exception as e:
        print("PHOTO SOLVE ERROR", e, flush=True)
        ok, text = False, ""
    try:
        spec = f_gr.result(timeout=25)
    except Exception:
        spec = None
    pool.shutdown(wait=False)
    if ok:
        save_turn(chat_id, f"[Я отправил фото] {task}", text)
        send_answer(message, text)
    else:
        bot.reply_to(message, ERR_TEXT, reply_markup=main_keyboard(message.chat.id))
    if spec:
        try:
            bot.send_photo(chat_id, make_plot(spec), caption=str(spec.get("caption") or "")[:900] or None,
                           reply_markup=main_keyboard(message.chat.id))
        except Exception as e:
            print("PHOTO GRAPH ERROR", e, flush=True)


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
        bot.send_photo(chat_id, img, reply_to_message_id=message.message_id, reply_markup=main_keyboard(message.chat.id))
    else:
        bot.reply_to(message, "Не получилось нарисовать. Попробуй описать иначе или повторить чуть позже.", reply_markup=main_keyboard(message.chat.id))


# ============ ГРАФИКИ ============

GRAPH_RE = re.compile(
    r"((?:постро\w*|нарису\w*|начерти\w*|изобрази\w*|покаж\w*|сделай|дай|выведи)\W+(?:\w+\W+){0,4}?"
    r"(?:график|диаграмм|парабол|гипербол|синусоид|косинусоид|окружност)"
    r"|график\w*[^\n]{0,30}(?:y\s*=|f\s*\(\s*x))",
    re.I,
)
# мягкая проверка: когда пользователь нажал «Картинка», но просит что-то математическое
MATH_RE = re.compile(r"(график|парабол|гипербол|синусоид|косинусоид|окружност|функци|\by\s*=|\bf\s*\(\s*x|x\s*(?:\^|\*\*)\s*\d)", re.I)
SOLVE_RE = re.compile(r"(реши|решить|найди|найти|вычисли|определи|докажи|исследуй|разбери|объясни)", re.I)


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


GRAPH_JSON_HELP = (
    '{"title":"...","x_min":-10,"x_max":10,"y_min":null,"y_max":null,'
    '"series":[{"type":"func","expr":"x**2-4","label":"y = x² − 4"},'
    '{"type":"implicit","expr":"x**2+y**2=9","label":"x² + y² = 9"},'
    '{"type":"points","x":[1,2,3],"y":[2,4,3],"label":"данные"}],'
    '"caption":"1-2 предложения на русском: что изображено, ключевые точки"}\n'
    "Правила: в expr только x (и y для implicit), операции + - * / **, умножение всегда знаком *, "
    "функции sin cos tan asin acos atan exp log sqrt abs, константы pi e. "
    "Выбери x_min и x_max так, чтобы было видно главное. Используй нужные типы серий, лишние не добавляй."
)


def parse_spec(raw):
    m = re.search(r"\{.*\}", raw or "", re.S)
    return json.loads(m.group(0)) if m else None


def do_graph(message, text):
    """Строит график по запросу. Возвращает True, если график отправлен."""
    chat_id = message.chat.id
    bot.send_chat_action(chat_id, "upload_photo")
    prompt = "Пользователь просит построить график. Верни ТОЛЬКО JSON без пояснений и без ```:\n" + GRAPH_JSON_HELP + f"\nЗапрос: {text}"
    ok, raw, _ = gemini_chain(FAST_MODELS[:2], [{"role": "user", "parts": [{"text": prompt}]}], budget=35, per_call=20, system=False)
    if not ok:
        return False
    try:
        spec = parse_spec(raw)
        png = make_plot(spec)
    except Exception as e:
        print("GRAPH ERROR", repr(e), flush=True)
        return False
    bot.send_photo(chat_id, png, caption=str(spec.get("caption") or "")[:900] or None,
                   reply_to_message_id=message.message_id, reply_markup=main_keyboard(message.chat.id))
    return True


def graph_from_image(img, caption):
    """Если в задании на фото нужно построить график, возвращает его описание, иначе None."""
    prompt = (
        "Посмотри на задание. Если в нём требуется построить или нарисовать график функции, параболу, гиперболу и т.п., "
        "верни ТОЛЬКО JSON для первого такого задания (без пояснений и без ```), формат:\n" + GRAPH_JSON_HELP +
        "\nЕсли построение графика не требуется, верни ровно слово NONE."
        + (f"\nПодпись пользователя: {caption}" if caption else "")
    )
    ok, raw, _ = gemini_chain(FAST_MODELS[:2], [{"role": "user", "parts": user_parts(prompt, img)}], budget=40, per_call=25, system=False)
    if not ok or not re.search(r"\{", raw) or raw.strip().upper().startswith("NONE"):
        return None
    try:
        spec = parse_spec(raw)
        make_plot(spec)   # проверяем заранее, что график строится
        return spec
    except Exception as e:
        print("GRAPH FROM IMAGE ERROR", repr(e), flush=True)
        return None


# ============ ПОИСК ============

def do_search(message, question):
    chat_id = message.chat.id
    bot.send_chat_action(chat_id, "typing")
    sysmsg, hist = sysmsg_for(chat_id), get_history(chat_id)
    ok, text = False, ""
    if GROQ_KEY:
        ok, text = call_openai_style(GROQ_COMPOUND, openai_messages(hist, question, sysmsg), 60)
    if not ok:
        ok, text, _ = call_gemini(FAST_MODELS[0], gemini_contents(hist, [{"text": question}]), 40, sysmsg,
                                  tools=[{"google_search": {}}])
    if ok:
        save_turn(chat_id, question, text)
        send_answer(message, text)
    else:
        bot.reply_to(message, "Поиск сейчас недоступен. Попробуй чуть позже.", reply_markup=main_keyboard(message.chat.id))


# ============ ПОЛЬЗОВАТЕЛИ И ЗАЩИТА ============

users = {}               # id -> {"name","username","first","last","n"}
registry_msg_id = None
users_dirty = False
rate = {}
warned = {}


def now_msk():
    return time.strftime("%d.%m %H:%M", time.gmtime(time.time() + 3 * 3600))


def clean_field(x):
    return re.sub(r"[|\n\r]+", " ", x or "").strip()[:40]


def save_registry(force=False):
    """Список пользователей хранится закреплённым сообщением в твоём чате с ботом (так он переживает перезапуски)."""
    global registry_msg_id, users_dirty
    if not ADMIN_ID or not (users_dirty or force):
        return
    lines = ["USERS_REGISTRY (не удаляй это сообщение)"] + [
        f"{i}|{u['name']}|{u['username']}|{u['first']}|{u['last']}|{u['n']}" for i, u in users.items()]
    text = "\n".join(lines)[:4000]
    try:
        if registry_msg_id:
            bot.edit_message_text(text, ADMIN_ID, registry_msg_id)
        else:
            m = bot.send_message(ADMIN_ID, text)
            registry_msg_id = m.message_id
            bot.pin_chat_message(ADMIN_ID, registry_msg_id, disable_notification=True)
        users_dirty = False
    except Exception as e:
        if "not modified" in str(e).lower():
            users_dirty = False
        else:
            print("REGISTRY ERROR", repr(e), flush=True)


def load_registry():
    global registry_msg_id
    if not ADMIN_ID:
        return
    try:
        pm = bot.get_chat(ADMIN_ID).pinned_message
        if pm and pm.text and pm.text.startswith("USERS_REGISTRY"):
            registry_msg_id = pm.message_id
            for ln in pm.text.split("\n")[1:]:
                p = ln.split("|")
                if len(p) >= 6 and p[0].isdigit():
                    users[int(p[0])] = {"name": p[1], "username": p[2], "first": p[3], "last": p[4], "n": int(p[5] or 0)}
    except Exception as e:
        print("LOAD REGISTRY ERROR", repr(e), flush=True)


def registry_worker():
    load_registry()
    while True:
        time.sleep(300)
        save_registry()


def track(u):
    global users_dirty
    name = clean_field(f"{u.first_name or ''} {u.last_name or ''}")
    with lock:
        rec = users.get(u.id)
        new = rec is None
        if new:
            rec = users[u.id] = {"name": name, "username": "", "first": now_msk(), "last": "", "n": 0}
        rec.update(name=name, username=clean_field(u.username), last=now_msk())
        rec["n"] += 1
        users_dirty = True
    if new and ADMIN_ID and u.id != ADMIN_ID:
        try:
            bot.send_message(ADMIN_ID, f"👤 Новый пользователь: {name} {'@' + u.username if u.username else ''}\nID: {u.id}")
        except Exception as e:
            print("NOTIFY ERROR", repr(e), flush=True)
        save_registry(force=True)


def guard(update):
    """Проверка доступа и частоты запросов. True: пропускаем."""
    msg = update.message
    if not msg or not msg.from_user:
        return True
    uid = msg.from_user.id
    if uid == ADMIN_ID:
        return True
    if ALLOWED and uid not in ALLOWED:
        if time.time() - warned.get(uid, 0) > 3600:
            warned[uid] = time.time()
            bot.send_message(msg.chat.id, "Этот бот приватный.")
        return False
    dq = rate.setdefault(uid, deque(maxlen=RATE_LIMIT))
    now = time.time()
    if len(dq) == RATE_LIMIT and now - dq[0] < 60:
        if now - warned.get(uid, 0) > 60:
            warned[uid] = now
            bot.send_message(msg.chat.id, "Слишком много сообщений подряд. Подожди минуту.")
        return False
    dq.append(now)
    return True


def handle_update(update):
    try:
        if guard(update):
            if update.message and update.message.from_user:
                track(update.message.from_user)
            bot.process_new_updates([update])
    except Exception as e:
        print("UPDATE ERROR", repr(e), flush=True)


# ============ TELEGRAM ============

@app.route(f"/{WEBHOOK_PATH}", methods=["POST"])
def webhook():
    if flask_request.headers.get("content-type") == "application/json":
        if secret_ok and flask_request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
            return "Forbidden", 403
        update = telebot.types.Update.de_json(flask_request.get_data().decode("utf-8"))
        if update.update_id in seen_updates:   # Telegram иногда присылает одно и то же дважды
            return "", 200
        seen_updates.append(update.update_id)
        threading.Thread(target=handle_update, args=(update,), daemon=True).start()
        return "", 200
    return "Forbidden", 403


@app.route("/")
def index():
    return "Bot is running perfectly!", 200


@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(
        message,
        f"Привет! Я {BOT_NAME}, твой ИИ-ассистент. Я помню наш разговор.\n\n"
        "⚡ Обычный: быстрый ответ, до 1 минуты\n"
        "🧠 Высокий: несколько сильных нейросетей думают вместе, до 3 минут\n"
        "🎨 Картинка, 🔎 Поиск: нажми и напиши, что нужно\n"
        "📝 Промты: задай мне свои правила, и я буду им следовать\n📄 В PDF: пришлю последний ответ файлом\n\n"
        "Пришли фото с заданием, и я сразу его решу. Могу построить график: «построй параболу y=x²−4».",
        reply_markup=main_keyboard(message.chat.id),
    )


@bot.message_handler(commands=["reset"])
@bot.message_handler(func=lambda m: m.text.startswith("🔄"), content_types=["text"])
def reset_context(message):
    with lock:
        histories.pop(message.chat.id, None)
        pending.pop(message.chat.id, None)
    bot.reply_to(message, "Готово, контекст сброшен.", reply_markup=main_keyboard(message.chat.id))


@bot.message_handler(func=lambda m: m.text in MODE_BUTTONS, content_types=["text"])
def switch_mode(message):
    mode = MODE_BUTTONS[message.text]
    with lock:
        modes[message.chat.id] = mode
        pending.pop(message.chat.id, None)
    bot.reply_to(message, f"Режим: {MODE_NAMES[mode]}", reply_markup=main_keyboard(message.chat.id))


@bot.message_handler(func=lambda m: m.text == IMG_BUTTON, content_types=["text"])
def img_button(message):
    with lock:
        pending[message.chat.id] = "img"
    bot.reply_to(message, "Напиши, что нарисовать. Например: рыжий кот в космосе", reply_markup=main_keyboard(message.chat.id))


@bot.message_handler(func=lambda m: m.text == SEARCH_BUTTON, content_types=["text"])
def search_button(message):
    with lock:
        pending[message.chat.id] = "search"
    bot.reply_to(message, "Напиши, что найти в интернете. Например: курс доллара сегодня", reply_markup=main_keyboard(message.chat.id))


@bot.message_handler(func=lambda m: m.text == PROMPT_BUTTON, content_types=["text"])
def prompt_button(message):
    with lock:
        pending[message.chat.id] = "prompt"
        current = custom_prompts.get(message.chat.id)
    bot.reply_to(
        message,
        "📝 Напиши инструкцию, и я буду следовать ей во всех ответах. Например:\n"
        "• «Отвечай коротко, как для 5 класса»\n• «Ты репетитор по химии, объясняй по шагам»\n\n"
        + (f"Сейчас стоит: {current}\n\n" if current else "Сейчас инструкции нет.\n\n")
        + "Чтобы удалить её, напиши «удалить».",
        reply_markup=main_keyboard(message.chat.id),
    )


def save_prompt(message, text):
    chat_id = message.chat.id
    if text.strip().lower() in ("удалить", "сбросить", "очистить", "нет"):
        with lock:
            custom_prompts.pop(chat_id, None)
        bot.reply_to(message, "Готово, инструкция удалена.", reply_markup=main_keyboard(message.chat.id))
    else:
        with lock:
            custom_prompts[chat_id] = text.strip()[:1500]
        bot.reply_to(message, "Принято, буду следовать этой инструкции.", reply_markup=main_keyboard(message.chat.id))


@bot.message_handler(func=lambda m: m.text == PDF_BUTTON, content_types=["text"])
def pdf_button(message):
    send_pdf(message)


@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    bot.reply_to(message, f"Твой Telegram ID: {message.from_user.id}")


@bot.message_handler(commands=["users", "stats"])
def cmd_users(message):
    if message.from_user.id != ADMIN_ID:
        return
    with lock:
        rows = sorted(users.items(), key=lambda kv: kv[1]["last"], reverse=True)
    total = sum(u["n"] for _, u in rows)
    lines = [f"👥 Пользователей: {len(rows)}, сообщений: {total}\n"]
    for uid, u in rows[:40]:
        lines.append(f"{u['name'] or '—'} {('@' + u['username']) if u['username'] else ''} · {uid} · {u['n']} сообщ. · был {u['last']}")
    bot.reply_to(message, "\n".join(lines)[:4000])


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
    if wait_for == "prompt":
        return save_prompt(message, text)
    if wait_for == "search":
        return do_search(message, text)
    if wait_for == "img":
        if MATH_RE.search(text) and do_graph(message, text):
            return
        return do_image(message, text)
    is_graph = bool(GRAPH_RE.search(text))
    graph_done = is_graph and do_graph(message, text)
    if graph_done and not SOLVE_RE.search(text):
        return
    if not is_graph and DRAW_RE.search(text):
        return do_image(message, text)
    process_question(message, text)


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    try:
        info = bot.get_file(message.photo[-1].file_id)
        img = bot.download_file(info.file_path)
        process_photo(message, img, message.caption)
    except Exception as e:
        print("PHOTO ERROR", repr(e), flush=True)
        bot.reply_to(message, ERR_TEXT, reply_markup=main_keyboard(message.chat.id))


# ============ ЗАПУСК ============

print("Cloudflare Account ID length:", len(CF_ACCOUNT or ""), "(должно быть 32)", flush=True)

if RENDER_EXTERNAL_URL:
    bot.remove_webhook()
    try:
        bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/{WEBHOOK_PATH}", secret_token=SECRET)
        secret_ok = True
    except Exception as e:
        print("SECRET TOKEN NOT SET", repr(e), flush=True)
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

threading.Thread(target=registry_worker, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
