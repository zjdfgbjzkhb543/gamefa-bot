import os
import re
import json
import sqlite3
import hashlib
import logging
import asyncio
import unicodedata
from typing import Optional, List, Dict, Any
from difflib import SequenceMatcher

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_FILE = os.getenv("DB_FILE", "gamefa_duplicate.db")
ARCHIVE_SIZE = int(os.getenv("ARCHIVE_SIZE", "150"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
MAX_AI_CANDIDATES = int(os.getenv("MAX_AI_CANDIDATES", "5"))

# ============================================================
# MAIN REPLY KEYBOARD LAYOUT (چیدمان دقیق مانند تصویر)
# ============================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        # ردیف ۱: ۲ دکمه بزرگ اصلی
        [KeyboardButton("🔍 بررسی خبر جدید"), KeyboardButton("📊 آمار آرشیو")],
        
        # ردیف ۲: ۳ دکمه
        [KeyboardButton("🧠 وضعیت AI"), KeyboardButton("📋 راهنما"), KeyboardButton("📦 وضعیت دیتابیس")],
        
        # ردیف ۳: ۴ دکمه
        [KeyboardButton("⚙️ تنظیمات"), KeyboardButton("💬 پشتیبانی"), KeyboardButton("👥 ادمین‌ها"), KeyboardButton("📜 قوانین")],
        
        # ردیف ۴: ۱ دکمه عریض در پایین
        [KeyboardButton("🗑 پاکسازی کامل آرشیو")]
    ],
    resize_keyboard=True
)

# ============================================================
# LOGGING
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
# AI SCHEMA
# ============================================================

class AIResult(BaseModel):
    news_a_core: str = Field(description="حقایق کلیدی خبر جدید")
    news_b_core: str = Field(description="حقایق کلیدی خبر آرشیوی")
    key_differences: str = Field(description="تفاوت‌های بنیادی")
    same_event: bool = Field(description="آیا رویداد خبری یکسان است؟")
    same_claim: bool = Field(description="آیا ادعا یکی است؟")
    duplicate: bool = Field(description="آیا خبر تکراری است؟")
    confidence: float = Field(description="اطمینان بین 0.0 تا 1.0")
    explanation: str = Field(description="توضیح کوتاه به فارسی")

# ============================================================
# DATABASE SETUP
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
                embedding TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sha256 ON news(sha256)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_url ON news(url)")
        conn.commit()

# ============================================================
# NORMALIZATION & UTILS
# ============================================================

PERSIAN_ARABIC_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

def normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(PERSIAN_ARABIC_DIGITS)
    replacements = {"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "ؤ": "و", "إ": "ا", "أ": "ا", "ٱ": "ا", "ـ": ""}
    for a, b in replacements.items():
        text = text.replace(a, b)
    text = text.replace("\u200c", " ")
    text = re.sub(r"https?://\S+", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\U00010000-\U0010ffff]", " ", text)
    text = re.sub(r"[^\w\s\u0600-\u06ff]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()

def sha256_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()

URL_PATTERN = re.compile(r"https?://[^\s<>\]\)\"']+", re.I)

def extract_urls(text: str) -> List[str]:
    if not text:
        return []
    urls = [url.rstrip(".,،؛!?؟") for url in URL_PATTERN.findall(text)]
    return list(dict.fromkeys(urls))

def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    url = re.sub(r"[?&]utm_[^&]+", "", url, flags=re.I)
    return url.lower()

def get_article_url(text: str) -> str:
    urls = extract_urls(text)
    if not urls:
        return ""
    for url in urls:
        if "gamefa.com" in url.lower():
            return normalize_url(url)
    return normalize_url(urls[0])

def extract_title(text: str) -> str:
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    title = re.sub(r"^[^\w\u0600-\u06ff]+", "", lines[0], flags=re.UNICODE)
    return title[:600]

# ============================================================
# SIMILARITY & EMBEDDINGS
# ============================================================

def sequence_similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()

def word_jaccard(a: str, b: str) -> float:
    set_a = set(normalize(a).split())
    set_b = set(normalize(b).split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)

def text_similarity(a: str, b: str) -> float:
    seq = sequence_similarity(a, b)
    jaccard = word_jaccard(a, b)
    return (seq * 0.75) + (jaccard * 0.25)

async def make_embedding(text: str) -> Optional[List[float]]:
    if not openai_client:
        return None
    try:
        response = await openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text[:10000]
        )
        return response.data[0].embedding
    except Exception as e:
        logger.exception("Embedding API error: %s", e)
        return None

def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a)
    nb = sum(x * x for x in b)
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))

# ============================================================
# AI JUDGE & CANDIDATES
# ============================================================

async def ai_compare(new_text: str, old_text: str) -> Optional[AIResult]:
    if not openai_client:
        return None

    prompt = f"""
تو موتور هوشمند تشخیص اخبار تکراری گیمفا هستی.
خبر جدید:
----------------
{new_text[:8000]}
----------------
خبر آرشیوی:
----------------
{old_text[:8000]}
----------------
"""
    try:
        response = await openai_client.beta.chat.completions.parse(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "You are a strict, factual news duplicate detector."},
                {"role": "user", "content": prompt}
            ],
            response_format=AIResult
        )
        return response.choices[0].message.parsed
    except Exception as e:
        logger.exception("AI Reasoning error: %s", e)
        return None

def get_candidates_sync(new_text: str, new_embedding: Optional[List[float]]):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (ARCHIVE_SIZE,)).fetchall()

    candidates = []
    new_title = extract_title(new_text)

    for row in rows:
        old_text = row["text"]
        lexical = text_similarity(new_text, old_text)
        title_score = sequence_similarity(new_title, row["title"] or "")

        semantic = 0.0
        if new_embedding and row["embedding"]:
            try:
                old_embedding = json.loads(row["embedding"])
                semantic = cosine_similarity(new_embedding, old_embedding)
            except Exception:
                semantic = 0.0

        ranking = (semantic * 0.50) + (title_score * 0.35) + (lexical * 0.15)
        candidates.append((ranking, semantic, lexical, title_score, row))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[:MAX_AI_CANDIDATES]

async def check_duplicate(text: str) -> Dict[str, Any]:
    normalized = normalize(text)
    fingerprint = sha256_hash(text)
    url = get_article_url(text)

    def fast_checks():
        with get_db() as conn:
            row = conn.execute("SELECT * FROM news WHERE sha256 = ? LIMIT 1", (fingerprint,)).fetchone()
            if row:
                return {"duplicate": True, "reason": "exact_hash", "confidence": 1.0, "row": row}

            if url:
                row = conn.execute("SELECT * FROM news WHERE url = ? LIMIT 1", (url,)).fetchone()
                if row:
                    return {"duplicate": True, "reason": "exact_url", "confidence": 1.0, "row": row}

            rows = conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (ARCHIVE_SIZE,)).fetchall()
            for r in rows:
                score = text_similarity(text, r["text"])
                if score >= 0.90:
                    return {"duplicate": True, "reason": "near_exact_text", "confidence": score, "row": r}
        return None

    quick_res = await asyncio.to_thread(fast_checks)
    if quick_res:
        return quick_res

    embedding = await make_embedding(normalized)
    candidates = await asyncio.to_thread(get_candidates_sync, text, embedding)

    tasks = []
    candidate_meta = []

    for ranking, semantic, lexical, title_score, row in candidates:
        if semantic < 0.40 and lexical < 0.25 and title_score < 0.35:
            continue
        tasks.append(ai_compare(text, row["text"]))
        candidate_meta.append((row, semantic, lexical, title_score))

    if tasks:
        results = await asyncio.gather(*tasks)
        best_decision = None

        for result, (row, semantic, lexical, title_score) in zip(results, candidate_meta):
            if not result:
                continue

            conf = max(0.0, min(1.0, float(result.confidence)))
            current = (conf, result, row)

            if best_decision is None or conf > best_decision[0]:
                best_decision = current

        if best_decision:
            conf, result, row = best_decision
            if result.duplicate and result.same_event and result.same_claim and conf >= 0.90:
                return {"duplicate": True, "reason": "ai_high_confidence", "confidence": conf, "row": row, "explanation": result.explanation}
            if result.duplicate and conf >= 0.70:
                return {"duplicate": True, "reason": "ai_ambiguous", "confidence": conf, "row": row, "explanation": result.explanation}

    return {"duplicate": False, "reason": "new_news", "confidence": 0.0, "row": None}

async def save_news(text: str) -> int:
    normalized = normalize(text)
    title = extract_title(text)
    url = get_article_url(text)
    fingerprint = sha256_hash(text)

    embedding = await make_embedding(normalized)
    embedding_json = json.dumps(embedding, ensure_ascii=False) if embedding else None

    def db_save():
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT INTO news (telegram_id, text, normalized, title, url, sha256, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("", text, normalized, title, url, fingerprint, embedding_json)
                )
            except sqlite3.IntegrityError:
                pass

            conn.execute("DELETE FROM news WHERE id NOT IN (SELECT id FROM news ORDER BY id DESC LIMIT ?)", (ARCHIVE_SIZE,))
            conn.commit()
            return conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]

    return await asyncio.to_thread(db_save)

# ============================================================
# TELEGRAM HANDLERS
# ============================================================

def is_allowed(update: Update) -> bool:
    if ADMIN_ID == 0:
        return True
    user = update.effective_user
    return bool(user and user.id == ADMIN_ID)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    # ارسال پیام خوش‌آمدگویی همراه با کیبورد ثابت
    await update.message.reply_text(
        "🤖 **ربات تشخیص خبر تکراری گیمفا**\n\n"
        "یکی از گزینه‌های منو را انتخاب کنید یا متن خبر را بفرستید:",
        reply_markup=MAIN_KEYBOARD
    )

async def send_stats(update: Update):
    def get_stats():
        with get_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
            embeddings = conn.execute("SELECT COUNT(*) FROM news WHERE embedding IS NOT NULL").fetchone()[0]
            return total, embeddings

    total, embeddings = await asyncio.to_thread(get_stats)
    await update.message.reply_text(
        f"📊 **وضعیت آرشیو ربات**\n\n"
        f"📦 اخبار ذخیره‌شده: {total}/{ARCHIVE_SIZE}\n"
        f"🧠 تعداد Embedding: {embeddings}/{total}\n"
        f"🤖 وضعیت مدل AI: {'فعال ✅' if openai_client else 'غیرفعال ❌'}",
        reply_markup=MAIN_KEYBOARD
    )

async def clear_archive(update: Update):
    def db_clear():
        with get_db() as conn:
            conn.execute("DELETE FROM news")
            conn.commit()

    await asyncio.to_thread(db_clear)
    await update.message.reply_text("🗑 آرشیو اخبار کاملاً پاکسازی شد.", reply_markup=MAIN_KEYBOARD)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    text = (update.message.text or update.message.caption or "").strip()
    if not text or text.startswith("/"):
        return

    # ----------------------------------------------------
    # مدیریت کلیک روی دکمه‌های کیبورد اصلی
    # ----------------------------------------------------
    if text == "📊 آمار آرشیو" or text == "📦 وضعیت دیتابیس":
        await send_stats(update)
        return

    elif text == "🗑 پاکسازی کامل آرشیو":
        await clear_archive(update)
        return

    elif text == "🧠 وضعیت AI":
        status_txt = "فعال ✅" if openai_client else "غیرفعال ❌"
        await update.message.reply_text(f"🧠 **مدل هوش مصنوعی:** {AI_MODEL}\nوضعیت: {status_txt}")
        return

    elif text in ["📋 راهنما", "🔍 بررسی خبر جدید"]:
        await update.message.reply_text(
            "ℹ️ **راهنمای استفاده:**\n\n"
            "کافی است متن کامل یا پست تلگرامی خبر را مستقیماً ارسال کنید تا ربات آن را با آرشیو مقایسه کند."
        )
        return

    elif text in ["💬 پشتیبانی", "👥 ادمین‌ها", "⚙️ تنظیمات", "📜 قوانین"]:
        await update.message.reply_text(f"بخش {text} فعال است.")
        return

    # ----------------------------------------------------
    # پردازش متن خبر ارسال شده
    # ----------------------------------------------------
    if len(text.split()) < 5 or len(text) < 25:
        await update.message.reply_text("⚠️ متن ارسال‌شده بسیار کوتاه است. حداقل ۵ کلمه بفرستید.")
        return

    status = await update.message.reply_text("🔎 در حال آنالیز چندلایه‌ای خبر...")

    try:
        result = await check_duplicate(text)

        if result["duplicate"]:
            conf = result["confidence"] * 100
            reason = result["reason"]

            if reason in ["exact_hash", "exact_url", "near_exact_text"]:
                await status.edit_text("♻️ **خبر تکراری است** (اطمینان ۱۰۰٪)\n\n⛔ خبر ذخیره نشد.")
                return

            elif reason == "ai_high_confidence":
                explanation = result.get("explanation", "AI همان رویداد را تشخیص داد.")
                await status.edit_text(
                    f"♻️ **خبر تکراری است**\n\n"
                    f"🎯 درصد اطمینان: {conf:.1f}%\n"
                    f"💡 دلیل AI: {explanation}\n\n"
                    f"⛔ خبر ذخیره نشد."
                )
                return

            elif reason == "ai_ambiguous":
                context.user_data["pending_news"] = text
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ ذخیره شود (خبر جدید است)", callback_data="force_save"),
                        InlineKeyboardButton("❌ رد شود (تکراری است)", callback_data="force_discard")
                    ]
                ])
                explanation = result.get("explanation", "شباهت بالایی دارد.")
                await status.edit_text(
                    f"⚠️ **نیازمند بررسی ادمین**\n\n"
                    f"🎯 درصد شباهت: {conf:.1f}%\n"
                    f"💡 تحلیل AI: {explanation}\n\n"
                    f"آیا خبر ذخیره شود؟",
                    reply_markup=keyboard
                )
                return

        total = await save_news(text)
        await status.edit_text(
            f"🆕 **خبر جدید است**\n\n"
            f"✅ ذخیره شد.\n"
            f"📦 آرشیو: {total}/{ARCHIVE_SIZE}"
        )

    except Exception as e:
        logger.exception("Processing Error")
        await status.edit_text(f"❌ خطا در پردازش:\n{str(e)}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    pending_text = context.user_data.get("pending_news")

    if query.data == "force_save" and pending_text:
        total = await save_news(pending_text)
        await query.edit_message_text(f"✅ خبر با دستور ادمین ذخیره شد.\n📦 آرشیو: {total}/{ARCHIVE_SIZE}")
    elif query.data == "force_discard":
        await query.edit_message_text("❌ خبر تکراری تشخیص داده شد و رد گردید.")

    context.user_data.pop("pending_news", None)

# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("GAMEFA DUPLICATE ENGINE STARTED WITH MAIN KEYBOARD")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

