import os
import re
import io
import html
import json
import math
import base64
import sqlite3
import hashlib
import logging
import asyncio
import unicodedata
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from difflib import SequenceMatcher

from PIL import Image
import matplotlib
matplotlib.use('Agg')  # تنظیم حالت غیرتعاملی برای محیط سرور/کانتینر
import matplotlib.pyplot as plt

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineQueryResultArticle,
    InputTextMessageContent
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# شناسه ادمین‌ها (لیست جدا شده با کاما)
ADMIN_IDS = [int(i.strip()) for i in os.getenv("ADMIN_ID", "0").split(",") if i.strip().isdigit()]

# شناسه انحصاری مالك اصلی (آیدی عددی خود را جایگزین کنید یا در متغیر محیطی OWNER_ID بگذارید)
OWNER_ID = int(os.getenv("OWNER_ID", "8202357756"))

DB_FILE = os.getenv("DB_FILE", "gamefa_duplicate.db")
ARCHIVE_SIZE = int(os.getenv("ARCHIVE_SIZE", "500"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
MAX_AI_CANDIDATES = int(os.getenv("MAX_AI_CANDIDATES", "5"))

# ============================================================
# LOGGING SETUP
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("gamefa-engine")

# ============================================================
# OPENAI ASYNC CLIENT
# ============================================================

openai_client: Optional[AsyncOpenAI] = None
if OPENAI_API_KEY:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ============================================================
# MAPPING & CONSTANTS
# ============================================================

GAME_ALIASES = {
    "ps5": "playstation5",
    "پلی استیشن 5": "playstation5",
    "پلی استیشن ۵": "playstation5",
    "ps4": "playstation4",
    "پلی استیشن 4": "playstation4",
    "xbox series x": "xboxseriesx",
    "ایکس باکس سری ایکس": "xboxseriesx",
    "pc": "کامپیوتر",
    "پی سی": "کامپیوتر",
    "راک استار": "rockstar",
    "راکستار": "rockstar",
    "جی تی ای": "gta",
    "جی تی ای 6": "gta6",
    "کالاف دیوتی": "callofduty",
    "سونی": "sony",
    "مایکروسافت": "microsoft",
    "نینتندو": "nintendo",
}

EVENT_TYPES = [
    "سیستم پیشنهادی", "سیستم مورد نیاز", "تاریخ انتشار", "تریلر",
    "ویدیو", "تاخیر", "شایعه", "کد تخفیف", "آپدیت", "پچ", "قیمت", "فروش", "مدت زمان"
]

PERSIAN_STOPWORDS = {
    "در", "یک", "از", "به", "با", "که", "را", "روی", "بر", "برای", "شد", "کرد",
    "است", "بود", "این", "آن", "هم", "نیز", "تا", "چون", "باید", "ستاره",
    "جدید", "اعلام", "انتشار", "تاریخ", "تریلر", "پوستر", "ویدیو", "تصویر",
    "عکس", "دانلود", "تماشا", "کنید", "رسما", "رسمی", "شایعه", "تایید",
    "پوشش", "اخبار", "خبر", "وجود", "داد"
}

# ============================================================
# MAIN REPLY KEYBOARD LAYOUT
# ============================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔍 بررسی خبر جدید"), KeyboardButton("📈 آمار تصویری")],
        [KeyboardButton("🧠 وضعیت AI"), KeyboardButton("📋 راهنما"), KeyboardButton("📦 وضعیت دیتابیس")],
        [KeyboardButton("📜 لاگ ادمین‌ها"), KeyboardButton("💾 دریافت بک‌آپ"), KeyboardButton("👥 ادمین‌ها")],
        [KeyboardButton("🗑 پاکسازی کامل آرشیو")]
    ],
    resize_keyboard=True
)

# ============================================================
# SAFE TELEGRAM MESSAGE SENDER
# ============================================================

async def safe_reply_text(message, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        return await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "parse entities" in str(e).lower() or "entity" in str(e).lower():
            logger.warning("HTML parsing failed, falling back to plain text.")
            clean_text = re.sub(r'<[^>]+>', '', text)
            return await message.reply_text(clean_text, reply_markup=reply_markup, parse_mode=None)
        raise e

async def safe_edit_text(message, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        return await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "parse entities" in str(e).lower() or "entity" in str(e).lower():
            logger.warning("HTML parsing failed during edit, falling back to plain text.")
            clean_text = re.sub(r'<[^>]+>', '', text)
            return await message.edit_text(clean_text, reply_markup=reply_markup, parse_mode=None)
        raise e

# ============================================================
# DATABASE SETUP & AUDIT LOG
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id TEXT,
                text TEXT NOT NULL,
                normalized TEXT NOT NULL,
                title TEXT,
                url TEXT,
                sha256 TEXT UNIQUE,
                image_hash TEXT,
                embedding TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                details TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sha256 ON news(sha256)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_url ON news(url)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_image_hash ON news(image_hash)")
        conn.commit()

def log_audit(user_id: int, action: str, details: str = ""):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_logs (user_id, action, details) VALUES (?, ?, ?)",
            (user_id, action, details)
        )
        conn.commit()

# ============================================================
# AI SCHEMAS (DUPLICATE CHECK & CONTENT ASSISTANT)
# ============================================================

class AIResult(BaseModel):
    news_a_core: str = Field(description="حقایق کلیدی خبر جدید")
    news_b_core: str = Field(description="حقایق کلیدی خبر آرشیوی")
    key_differences: str = Field(description="تفاوت‌های بنیادی")
    same_event: bool = Field(description="آیا رویداد خبری یا تیتر یکسان است؟")
    same_claim: bool = Field(description="آیا موضوع اصلی یکی است؟")
    duplicate: bool = Field(description="آیا خبر تکراری است؟")
    is_update: bool = Field(description="آیا این خبر یک پوشش تکمیلی یا بروزرسانی خبر قبلی است؟")
    confidence: float = Field(description="اطمینان بین 0.0 تا 1.0")
    explanation: str = Field(description="توضیح کوتاه به فارسی")

class AIContentAssistant(BaseModel):
    rewritten_text: str = Field(description="پیش‌نویس بازنویسی شده با لحن جذاب رسانه گیمفا")
    hashtags: List[str] = Field(description="لیست هشتگ‌های پیشنهادی مرتبط مانند #PS5")
    urgency_level: str = Field(description="سطح اهمیت خبر: فوری / مهم / عادی")

# ============================================================
# TEXT PROCESSING & EMBEDDING
# ============================================================

PERSIAN_ARABIC_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

def normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(PERSIAN_ARABIC_DIGITS)
    replacements = {"ي": "y", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "ؤ": "و", "إ": "ا", "أ": "ا", "ٱ": "ا", "ـ": ""}
    for a, b in replacements.items():
        text = text.replace(a, b)
    text = text.replace("\u200c", " ")
    text = re.sub(r"https?://\S+", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^\w\s\u0600-\u06ff]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip().lower()

    for alias, standard in GAME_ALIASES.items():
        text = re.sub(r'\b' + re.escape(alias) + r'\b', standard, text)

    return text

def clean_gaming_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\[(شایعه|رسمی|فوری|تحلیل|ویدیو|تکمیلی|اختصاصی|گیمفا|دانلود|تریلر)\]', ' ', text)
    text = re.sub(r'(@\w+|https?://\S+|t\.me/\S+|gamefa\.com\S*|رسانه گیمفا|گیمفا)', ' ', text, flags=re.I)
    return normalize(text)

def sha256_hash(text: str) -> str:
    return hashlib.sha256(clean_gaming_text(text).encode("utf-8")).hexdigest()

def extract_title(text: str) -> str:
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0][:600] if lines else ""

async def make_embedding(text: str) -> Optional[List[float]]:
    if not openai_client:
        return None
    try:
        response = await openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=clean_gaming_text(text)[:10000]
        )
        return response.data[0].embedding
    except Exception as e:
        logger.exception("Embedding API error: %s", e)
        return None

def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm_v1 = math.sqrt(sum(a * a for a in v1))
    norm_v2 = math.sqrt(sum(b * b for b in v2))
    return dot / (norm_v1 * norm_v2) if norm_v1 and norm_v2 else 0.0

# ============================================================
# OCR & IMAGE HASHING
# ============================================================

def compute_image_hash(image_bytes: bytes) -> str:
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert('L')
        w, h = img.size
        img = img.crop((int(w * 0.1), int(h * 0.1), int(w * 0.9), int(h * 0.9)))
        img = img.resize((9, 8), Image.Resampling.LANCZOS)
        
        pixels = list(img.getdata())
        difference = []
        for row in range(8):
            for col in range(8):
                difference.append(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
        
        decimal_value = 0
        hex_string = []
        for index, value in enumerate(difference):
            if value:
                decimal_value += 2 ** (index % 4)
            if index % 4 == 3:
                hex_string.append(hex(decimal_value)[2:])
                decimal_value = 0
        return "".join(hex_string)
    except Exception as e:
        logger.error("Image hashing error: %s", e)
        return ""

def hamming_distance(h1: str, h2: str) -> int:
    if not h1 or not h2 or len(h1) != len(h2):
        return 999
    return sum(c1 != c2 for c1, c2 in zip(h1, h2))

async def extract_ocr_text(image_bytes: bytes) -> str:
    if not openai_client:
        return ""
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        response = await openai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "تمام متن‌های موجود در این تصویر خبری گیمینگ را دقیقاً استخراج کن و موضوع آن را شرح بده:"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            max_tokens=400
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.error("Vision OCR error: %s", e)
        return ""

# ============================================================
# AI GENERATIVE REWRITE & HASHTAGS
# ============================================================

async def generate_ai_assistant_content(text: str) -> Optional[AIContentAssistant]:
    if not openai_client:
        return None
    try:
        response = await openai_client.beta.chat.completions.parse(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "تو دستیار سردبیری رسانه گیمفا هستی. برای این خبر یک بازنویسی جذاب با لحن اختصاصی، هشتگ‌های کلیدی و سطح اهمیت آن را مشخص کن."},
                {"role": "user", "content": text[:4000]}
            ],
            response_format=AIContentAssistant
        )
        return response.choices[0].message.parsed
    except Exception as e:
        logger.error("AI Assistant Generation error: %s", e)
        return None

# ============================================================
# AI COMPARE (DUPLICATE CHECK)
# ============================================================

async def ai_compare(new_text: str, old_text: str) -> Optional[AIResult]:
    if not openai_client:
        return None
    try:
        response = await openai_client.beta.chat.completions.parse(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "آیا این دو خبر مربوط به یک موضوع یا رویداد خبری یکسان هستند؟ بازنویسی کلمات همچنان خبر را تکراری می‌سازد."},
                {"role": "user", "content": f"خبر جدید:\n{new_text[:4000]}\n\nخبر آرشیو:\n{old_text[:4000]}"}
            ],
            response_format=AIResult
        )
        return response.choices[0].message.parsed
    except Exception as e:
        logger.error("AI Compare error: %s", e)
        return None

# ============================================================
# VISUAL ANALYTICS (MATPLOTLIB CHART)
# ============================================================

def generate_analytics_chart_bytes() -> io.BytesIO:
    with get_db() as conn:
        total_news = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        total_logs = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        saves = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'FORCE_SAVE'").fetchone()[0]
        discards = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'FORCE_DISCARD'").fetchone()[0]

    fig, ax = plt.subplots(figsize=(6, 4))
    categories = ['کل آرشیو', 'تایید دستی', 'رد دستی', 'کل فعالیت‌ها']
    values = [total_news, saves, discards, total_logs]
    colors = ['#3498db', '#2ecc71', '#e74c3c', '#f1c40f']

    bars = ax.bar(categories, values, color=colors)
    ax.set_ylabel('تعداد')
    ax.set_title('آمار عملکرد دیتابیس و مدیریت اخبار Gamefa', fontsize=12)
    
    for bar in bars:
        yval = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, yval + 0.1, int(yval), ha='center', va='bottom')

    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf

# ============================================================
# MAIN CHECK PIPELINE
# ============================================================

async def check_duplicate(text: str, image_hash: Optional[str] = None, image_bytes: Optional[bytes] = None) -> Dict[str, Any]:
    if image_bytes and len(text.split()) < 5:
        ocr_text = await extract_ocr_text(image_bytes)
        if ocr_text:
            text = f"{text}\n[متن استخراج‌شده از تصویر]: {ocr_text}"

    fingerprint = sha256_hash(text)

    # Fast DB Check
    with get_db() as conn:
        row = conn.execute("SELECT * FROM news WHERE sha256 = ? LIMIT 1", (fingerprint,)).fetchone()
        if row:
            return {"duplicate": True, "reason": "exact_hash", "confidence": 1.0, "row": row}

        if image_hash:
            img_rows = conn.execute("SELECT * FROM news WHERE image_hash IS NOT NULL AND image_hash != '' ORDER BY id DESC LIMIT 50").fetchall()
            for r in img_rows:
                if hamming_distance(image_hash, r["image_hash"]) <= 6:
                    return {"duplicate": True, "reason": "image_match", "confidence": 0.95, "row": r}

    # Vector / AI Search
    cleaned = clean_gaming_text(text)
    embedding = await make_embedding(cleaned)

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (ARCHIVE_SIZE,)).fetchall()

    if not rows:
        return {"duplicate": False, "reason": "new_news", "confidence": 0.0, "row": None}

    candidates = []
    for r in rows:
        score = 0.0
        if embedding and r["embedding"]:
            try:
                score = cosine_similarity(embedding, json.loads(r["embedding"]))
            except Exception:
                pass
        candidates.append((score, r))

    candidates.sort(key=lambda x: x[0], reverse=True)
    top_candidates = candidates[:MAX_AI_CANDIDATES]

    tasks = [ai_compare(text, r["text"]) for sc, r in top_candidates if sc > 0.20]
    if tasks:
        ai_results = await asyncio.gather(*tasks)
        for res, (sc, r) in zip(ai_results, top_candidates):
            if res and res.duplicate and res.confidence >= 0.70:
                return {"duplicate": True, "reason": "ai_detected", "confidence": res.confidence, "row": r, "explanation": res.explanation}

    return {"duplicate": False, "reason": "new_news", "confidence": 0.0, "row": None}

async def save_news(text: str, image_hash: Optional[str] = None) -> int:
    cleaned = clean_gaming_text(text)
    title = extract_title(text)
    fingerprint = sha256_hash(text)
    embedding = await make_embedding(cleaned)
    embedding_json = json.dumps(embedding) if embedding else None

    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO news (telegram_id, text, normalized, title, sha256, image_hash, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("", text, cleaned, title, fingerprint, image_hash or "", embedding_json)
            )
            conn.execute("DELETE FROM news WHERE id NOT IN (SELECT id FROM news ORDER BY id DESC LIMIT ?)", (ARCHIVE_SIZE,))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        return conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]

# ============================================================
# TELEGRAM HANDLERS
# ============================================================

def is_allowed(update: Update) -> bool:
    if not ADMIN_IDS or 0 in ADMIN_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return
    await safe_reply_text(
        update.message,
        "🤖 <b>ربات هوشمند تشخیص خبر و دستیار محتوای گیمفا</b>\n\n"
        "متن یا تصویر خبر جدید را جهت آنالیز ارسال کنید:",
        reply_markup=MAIN_KEYBOARD
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    text = (update.message.text or update.message.caption or "").strip()
    photo = update.message.photo

    # ============================================================
    # مدیریت دستورات و دکمه‌های کیبورد اصلی (جلوگیری از آنالیز متن دکمه‌ها)
    # ============================================================
    if text == "📈 آمار تصویری":
        chart_buf = generate_analytics_chart_bytes()
        await update.message.reply_photo(photo=chart_buf, caption="📈 **نمودار تحلیل عملکرد سیستم خبر گیمفا**")
        return

    elif text == "💾 دریافت بک‌آپ":
        if user_id != OWNER_ID:
            await safe_reply_text(update.message, "⛔ <b>دسترسی غیرمجاز!</b>\nدریافت فایل بک‌آپ فقط برای مالک اصلی ربات مجاز است.")
            return
        log_audit(user_id, "DOWNLOAD_BACKUP")
        with open(DB_FILE, "rb") as f:
            await update.message.reply_document(document=f, filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db", caption="📦 نسخه پشتیبان دیتابیس")
        return

    elif text == "📜 لاگ ادمین‌ها":
        with get_db() as conn:
            logs = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 10").fetchall()
        if not logs:
            await safe_reply_text(update.message, "📜 هیچ لاگ فعالیتی ثبت نشده است.")
            return
        msg = "📜 <b>آخرین فعالیت‌های ثبت‌شده ادمین‌ها:</b>\n\n"
        for l in logs:
            msg += f"👤 کاربر: <code>{l['user_id']}</code> | اکشن: <b>{l['action']}</b>\n📅 {l['created_at']}\n---\n"
        await safe_reply_text(update.message, msg)
        return

    elif text == "🗑 پاکسازی کامل آرشیو":
        if user_id != OWNER_ID:
            await safe_reply_text(update.message, "⛔ <b>دسترسی غیرمجاز!</b>\nعملیات پاکسازی کامل آرشیو فقط برای مالک اصلی ربات مجاز است.")
            return
        with get_db() as conn:
            conn.execute("DELETE FROM news")
            conn.commit()
        log_audit(user_id, "PURGE_ARCHIVE")
        await safe_reply_text(update.message, "🗑 <b>آرشیو دیتابیس با موفقیت پاکسازی شد.</b>", reply_markup=MAIN_KEYBOARD)
        return

    elif text in ["📊 آمار آرشیو", "📦 وضعیت دیتابیس"]:
        with get_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        await safe_reply_text(update.message, f"📊 <b>وضعیت دیتابیس:</b>\n\nتعداد اخبار ذخیره‌شده: <code>{total}/{ARCHIVE_SIZE}</code>")
        return

    elif text == "👥 ادمین‌ها":
        admins_str = ", ".join(map(str, ADMIN_IDS))
        await safe_reply_text(update.message, f"👥 <b>مدیریت دسترسی:</b>\n\n• مالک اصلی: <code>{OWNER_ID}</code>\n• شناسه ادمین‌ها: <code>{admins_str}</code>")
        return

    elif text in ["📋 راهنما", "🔍 بررسی خبر جدید"]:
        await safe_reply_text(
            update.message,
            "ℹ️ <b>راهنمای بررسی خبر:</b>\n\n"
            "• متن، تصویر یا پستی که می‌خواهید بررسی شود را مستقیماً به چت بفرستید (یا فوروارد کنید).\n"
            "• ربات به صورت خودکار آن را در آرشیو مقایسه کرده و نتیجه را اعلام می‌کند."
        )
        return

    elif text == "🧠 وضعیت AI":
        status_ai = "🟢 فعال" if openai_client else "🔴 غیرفعال (کلید API تنظیم نشده)"
        await safe_reply_text(
            update.message,
            f"🧠 <b>وضعیت موتور هوش مصنوعی:</b>\n\n"
            f"• وضعیت اتصال: {status_ai}\n"
            f"• مدل پردازش متنی و Vision: <code>{AI_MODEL}</code>\n"
            f"• مدل Embedding: <code>{EMBEDDING_MODEL}</code>"
        )
        return

    # ============================================================
    # پردازش خبر واقعی ارسال‌شده
    # ============================================================
    image_hash = None
    image_bytes = None
    if photo:
        file = await context.bot.get_file(photo[-1].file_id)
        img_bytearray = await file.download_as_bytearray()
        image_bytes = bytes(img_bytearray)
        image_hash = compute_image_hash(image_bytes)

    if not text and not image_hash:
        return

    status = await safe_reply_text(update.message, "🔎 در حال آنالیز ۶ لایه‌ای و استخراج هوشمند اطلاعات...")

    result = await check_duplicate(text, image_hash, image_bytes)

    if result["duplicate"]:
        conf = result["confidence"] * 100
        old_text = result["row"]["text"][:200]
        explanation = html.escape(result.get("explanation", "مطابقت با داده‌های آرشیو."))
        await safe_edit_text(
            status,
            f"♻️ <b>خبر تکراری است!</b>\n\n"
            f"🎯 درصد اطمینان: {conf:.1f}%\n"
            f"💡 دلیل AI: {explanation}\n\n"
            f"📌 <b>خبر قبلی موجود:</b>\n«{html.escape(old_text)}...»\n\n"
            f"⛔ خبر ذخیره نشد."
        )
        log_audit(user_id, "DUPLICATE_DETECTED", f"Confidence: {conf:.1f}%")
        return

    # ذخیره و تولید پیش‌نویس بازنویسی شده با AI
    total = await save_news(text, image_hash)
    log_audit(user_id, "SAVE_NEWS")

    assistant_data = await generate_ai_assistant_content(text)
    
    response_msg = f"🆕 <b>خبر جدید است و ذخیره شد.</b>\n📦 ظرفیت آرشیو: {total}/{ARCHIVE_SIZE}\n\n"
    if assistant_data:
        tags = " ".join([f"#{t.strip('#')}" for t in assistant_data.hashtags])
        response_msg += (
            f"🚨 <b>سطح اهمیت:</b> {html.escape(assistant_data.urgency_level)}\n\n"
            f"✍️ <b>پیش‌نویس پیشنهاد شده برای انتشار:</b>\n"
            f"«{html.escape(assistant_data.rewritten_text)}»\n\n"
            f"🏷 <b>هشتگ‌ها:</b>\n{html.escape(tags)}"
        )

    await safe_edit_text(status, response_msg)

# ============================================================
# INLINE SEARCH HANDLER FOR ADMINS
# ============================================================

async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if not query:
        return

    norm_query = normalize(query)
    with get_db() as conn:
        rows = conn.execute("SELECT title, text FROM news WHERE normalized LIKE ? ORDER BY id DESC LIMIT 5", (f"%{norm_query}%",)).fetchall()

    results = []
    for idx, r in enumerate(rows):
        results.append(
            InlineQueryResultArticle(
                id=str(idx),
                title=r["title"] or "خبر آرشیوی",
                input_message_content=InputTextMessageContent(f"📌 <b>{html.escape(r['title'] or '')}</b>\n\n{html.escape(r['text'][:500])}...", parse_mode="HTML")
            )
        )
    await update.inline_query.answer(results)

# ============================================================
# MAIN APPLICATION EXECUTION
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))
    app.add_handler(InlineQueryHandler(inline_search))

    logger.info("GAMEFA FULL-FEATURED ENGINE STARTED SUCCESSFULLY")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
