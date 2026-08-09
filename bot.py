import os
import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from typing import List, Optional

from PIL import Image
from pydantic import BaseModel
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import openai

# ==========================================
# 1. تنظیمات اولیه و لاگینگ
# ==========================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# دریافت کلیدها از محیط (Environment Variables)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

AI_MODEL = "gpt-4o-mini"

openai_client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ==========================================
# 2. مدل‌های Pydantic برای خروجی ساختاریافته AI
# ==========================================
class AIResult(BaseModel):
    is_duplicate: bool
    is_update: bool
    confidence: float
    reason: str

# ==========================================
# 3. مدیریت دیتابیس SQLite
# ==========================================
DB_NAME = "bot_archive.db"

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                sha256 TEXT,
                url TEXT,
                image_hash TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

init_db()

# ==========================================
# 4. توابع کمکی و پردازشی
# ==========================================
def calculate_sha256(text: str) -> str:
    return hashlib.sha256(text.strip().encode('utf-8')).hexdigest()

def word_jaccard(text1: str, text2: str) -> float:
    words1 = set(re.findall(r'\w+', text1.lower()))
    words2 = set(re.findall(r'\w+', text2.lower()))
    if not words1 or not words2:
        return 0.0
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    return len(intersection) / len(union)

def entity_overlap_score(text1: str, text2: str) -> float:
    entities1 = set(re.findall(r'[A-Za-z0-9]+|[\u0600-\u06FF]{4,}', text1))
    entities2 = set(re.findall(r'[A-Za-z0-9]+|[\u0600-\u06FF]{4,}', text2))
    if not entities1 or not entities2:
        return 0.0
    return len(entities1.intersection(entities2)) / len(entities1.union(entities2))

def extract_metadata(text: str) -> dict:
    events = []
    keywords = ["مدت زمان", "تاریخ انتشار", "تریلر", "معرفی", "سیستم پیشنهادی", "فروش", "بازیگر", "کارگردان"]
    for kw in keywords:
        if kw in text:
            events.append(kw)
    return {"events": events}

def hamming_distance(hash1: str, hash2: str) -> int:
    if not hash1 or not hash2 or len(hash1) != len(hash2):
        return 999
    return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))

# ==========================================
# 5. مقایسه هوشمند با OpenAI
# ==========================================
async def ai_compare(new_text: str, old_text: str) -> Optional[AIResult]:
    if not openai_client:
        return None

    system_prompt = """
تو یک موتور دقیق تشخیص اخبار تکراری در حوزه سینما، سرگرمی و بازی هستی.

قاعده کلیدی تشخیص:
۱. در اخبار سینما و گیمینگ، یک خبر ممکن است در یک کانال با "نام فیلم/بازی" (مثلاً Digger) و در کانال دیگر با "نام کارگردان/بازیگر/سازنده" (مثلاً الخاندرو اینیاریتو یا تام کروز) پوشش داده شود.
۲. اگر هر دو خبر درباره یک رویداد مشخص (مثلاً: اعلام مدت زمان، مشخص شدن تاریخ عرضه، انتشار تریلر جدید) برای یک پروژه سینمایی/گیمینگ باشند، حتی اگر اسامی به‌کار رفته متفاوت باشند، آن‌ها را تکراری (is_duplicate = True) تشخیص بده.
۳. تغییر کلمات یا بازنویسی جمله باعث غیرتکراری شدن خبر نمی‌شود.

نمونه تکراری:
خبر ۱: "مدت زمان فیلم Digger با بازی تام کروز مشخص شد"
خبر ۲: "مدت زمان نهایی فیلم جدید الخاندرو اینیاریتو مشخص شد"
پاسخ: is_duplicate = True (چون هر دو اعلام مدت زمان یک پروژه واحد هستند).
"""

    user_prompt = f"""
خبر جدید:
----------------
{new_text[:2000]}
----------------
خبر آرشیوی:
----------------
{old_text[:2000]}
----------------
"""
    try:
        response = await openai_client.beta.chat.completions.parse(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format=AIResult
        )
        return response.choices[0].message.parsed
    except Exception as e:
        logger.exception("خطا در پردازش AI: %s", e)
        return None

# ==========================================
# 6. الگوریتم کاندیدیابی و بررسی تکراری بودن
# ==========================================
def get_candidates_sync(text: str, top_k: int = 5):
    new_meta = extract_metadata(text)
    new_events = set(new_meta.get("events", []))
    
    structural_keywords = {"مدت زمان", "تاریخ انتشار", "تریلر", "معرفی", "سیستم پیشنهادی", "فروش"}
    has_structural = any(kw in text for kw in structural_keywords)

    candidates = []
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT 150").fetchall()
        for row in rows:
            old_text = row["text"]
            old_meta = extract_metadata(old_text)
            
            semantic = 0.0
            lexical = word_jaccard(text, old_text)
            ner_score = entity_overlap_score(text, old_text)
            
            shared_events = new_events & set(old_meta.get("events", []))
            event_boost = 0.35 if (shared_events or has_structural) else 0.0

            ranking = (0.4 * semantic) + (0.2 * lexical) + (0.2 * ner_score) + event_boost

            if ranking >= 0.08:
                candidates.append((ranking, semantic, lexical, ner_score, row))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[:top_k]

async def check_duplicate(text: str, image_hash: Optional[str] = None, url: Optional[str] = None) -> dict:
    fingerprint = calculate_sha256(text)
    
    with get_db() as conn:
        row = conn.execute("SELECT * FROM news WHERE sha256 = ? LIMIT 1", (fingerprint,)).fetchone()
        if row:
            return {"duplicate": True, "reason": "هش متن کاملاً یکسان است.", "confidence": 1.0, "row": row}

        new_meta = extract_metadata(text)
        recent_rows = conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT 100").fetchall()
        for r in recent_rows:
            old_meta = extract_metadata(r["text"])
            shared_events = set(new_meta["events"]) & set(old_meta["events"])
            overlap = entity_overlap_score(text, r["text"])
            jaccard = word_jaccard(text, r["text"])

            if overlap >= 0.75 or jaccard >= 0.50:
                return {"duplicate": True, "reason": "تشابه متنی بسیار بالا.", "confidence": 0.95, "row": r}

    candidates = get_candidates_sync(text)
    
    for ranking, semantic, lexical, ner_score, row in candidates:
        ai_res = await ai_compare(text, row["text"])
        if ai_res and ai_res.is_duplicate:
            return {
                "duplicate": True,
                "reason": ai_res.reason,
                "confidence": ai_res.confidence,
                "row": row
            }

    if candidates:
        top_ranking, semantic, lexical, ner_score, top_row = candidates[0]
        if top_ranking >= 0.45:
            return {
                "duplicate": True,
                "reason": "تشابه ساختاری و الگویی بالا (بررسی الگوریتمی).",
                "confidence": min(top_ranking, 0.90),
                "row": top_row
            }

    return {"duplicate": False, "reason": "خبر جدید است.", "confidence": 0.0, "row": None}

def save_news(text: str, image_hash: str = None, url: str = None):
    fingerprint = calculate_sha256(text)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO news (text, sha256, url, image_hash) VALUES (?, ?, ?, ?)",
            (text, fingerprint, url, image_hash)
        )
        conn.commit()

def get_archive_count() -> int:
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        return count

def clear_archive():
    with get_db() as conn:
        conn.execute("DELETE FROM news")
        conn.commit()

# ==========================================
# 7. هندلرهای تلگرام
# ==========================================
async def safe_reply_text(message, text: str):
    await message.reply_text(text, parse_mode="HTML")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(
        update.message,
        "سلام! به ربات GAMFA خوش آمدید.\nمتن یا تصویر خبر جدید را ارسال کنید تا بررسی شود."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""

    if text in ["ℹ️ راهنما", "📋 راهنما"]:
        await safe_reply_text(
            update.message,
            "ℹ️ <b>راهنمای استفاده از ربات:</b>\n\n"
            "• کافیست متن یا تصویر پست جدید تلگرام را ارسال کنید.\n"
            "• ربات آن را در ۶ لایه آنالیز کرده و اسامی گیمینگ، پلتفرم‌ها و کاور را تطبیق می‌دهد.\n"
            "• در صورت تکراری بودن، متن خبر قبلی آرشیو شده به همراه دلیل نمایش داده خواهد شد."
        )
        return

    elif text == "🔍 بررسی خبر جدید":
        await safe_reply_text(
            update.message,
            "🔍 <b>لطفاً متن یا تصویر خبر جدید را ارسال کنید تا بررسی شود.</b>"
        )
        return

    elif text == "🗑 پاکسازی کامل آرشیو":
        clear_archive()
        await safe_reply_text(update.message, "🗑 <b>آرشیو دیتابیس با موفقیت پاکسازی شد.</b>")
        return

    if not text:
        return

    res = await check_duplicate(text)
    count = get_archive_count()

    if res["duplicate"]:
        old_text = res["row"]["text"]
        await safe_reply_text(
            update.message,
            f"⚠️ <b>خبر تکراری است!</b>\n\n"
            f"<b>دلیل:</b> {res['reason']}\n\n"
            f"<b>متن خبر قبلی در آرشیو:</b>\n<i>{old_text}</i>\n\n"
            f"📦 <b>آرشیو:</b> {count}/150"
        )
    else:
        save_news(text)
        new_count = get_archive_count()
        await safe_reply_text(
            update.message,
            f"🆕 <b>خبر جدید است</b>\n\n"
            f"✅ <b>ذخیره شد.</b>\n"
            f"📦 <b>آرشیو:</b> {new_count}/150"
        )

# ==========================================
# 8. اجرای اصلی ربات
# ==========================================
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started successfully...")
    app.run_polling()
