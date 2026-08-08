import logging
import sqlite3
import re
import hashlib
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import textdistance


# =========================================================
# تنظیمات
# =========================================================

BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"

# آیدی عددی ادمین‌ها
ADMIN_IDS = [
    123456789,
]

DATABASE_FILE = "news.db"

# حداقل درصد شباهت برای تشخیص خبر مشابه
SIMILARITY_THRESHOLD = 0.75

# فقط اخبار 24 ساعت اخیر نگهداری می‌شوند
KEEP_HOURS = 24

# حداکثر تعداد خبر برای مقایسه
MAX_NEWS_TO_CHECK = 100


# =========================================================
# لاگ
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# دیتابیس
# =========================================================

class Database:

    def __init__(self, filename):
        self.filename = filename
        self.init_database()

    def connect(self):
        return sqlite3.connect(self.filename)

    def init_database(self):

        with self.connect() as conn:

            conn.execute("""
                CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_created_at
                ON news(created_at)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_text_hash
                ON news(text_hash)
            """)

    def cleanup_old_news(self):

        cutoff = datetime.utcnow() - timedelta(hours=KEEP_HOURS)

        with self.connect() as conn:

            conn.execute(
                """
                DELETE FROM news
                WHERE created_at < ?
                """,
                (cutoff.isoformat(),)
            )

            conn.commit()

    def add_news(self, text):

        text_hash = hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()

        created_at = datetime.utcnow().isoformat()

        with self.connect() as conn:

            cursor = conn.execute(
                """
                INSERT INTO news
                (text, text_hash, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    text,
                    text_hash,
                    created_at,
                )
            )

            conn.commit()

            return cursor.lastrowid

    def get_recent_news(self):

        cutoff = datetime.utcnow() - timedelta(hours=KEEP_HOURS)

        with self.connect() as conn:

            cursor = conn.execute(
                """
                SELECT id, text, created_at
                FROM news
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (
                    cutoff.isoformat(),
                    MAX_NEWS_TO_CHECK,
                )
            )

            return cursor.fetchall()

    def exists_exact(self, text_hash):

        with self.connect() as conn:

            cursor = conn.execute(
                """
                SELECT id, text, created_at
                FROM news
                WHERE text_hash = ?
                LIMIT 1
                """,
                (text_hash,)
            )

            return cursor.fetchone()


# =========================================================
# نرمال‌سازی متن فارسی
# =========================================================

def normalize_text(text):

    if not text:
        return ""

    text = text.strip().lower()

    # یکسان‌سازی حروف عربی و فارسی
    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ۀ": "ه",
        "ة": "ه",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # تبدیل اعداد انگلیسی به فارسی
    english_digits = "0123456789"
    persian_digits = "۰۱۲۳۴۵۶۷۸۹"

    translation_table = str.maketrans(
        english_digits,
        persian_digits
    )

    text = text.translate(translation_table)

    # حذف لینک
    text = re.sub(
        r"https?://\S+|www\.\S+",
        " ",
        text
    )

    # حذف منشن
    text = re.sub(
        r"@\w+",
        " ",
        text
    )

    # حذف هشتگ از ابتدای کلمه
    text = re.sub(
        r"#",
        " ",
        text
    )

    # حذف ایموجی‌ها و علائم اضافی
    text = re.sub(
        r"[^\w\s\u0600-\u06FF]",
        " ",
        text
    )

    # حذف فاصله‌های اضافی
    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# =========================================================
# کلمات بی‌اهمیت
# =========================================================

STOPWORDS = {
    "و",
    "در",
    "به",
    "از",
    "با",
    "برای",
    "که",
    "این",
    "آن",
    "را",
    "است",
    "شد",
    "شده",
    "می",
    "شود",
    "خواهد",
    "یک",
    "اما",
    "اگر",
    "یا",
    "نیز",
    "هم",
    "بر",
    "تا",
    "پس",
    "روی",
    "درباره",
    "کرد",
    "کرده",
    "کند",
    "کنند",
    "هست",
    "هستند",
    "بود",
    "بودند",
    "ای",
    "های",
    "ها",
}


# =========================================================
# استخراج کلمات مهم
# =========================================================

def get_keywords(text):

    text = normalize_text(text)

    words = text.split()

    keywords = []

    for word in words:

        if len(word) < 3:
            continue

        if word in STOPWORDS:
            continue

        keywords.append(word)

    return set(keywords)


# =========================================================
# محاسبه شباهت
# =========================================================

def calculate_similarity(text1, text2):

    text1 = normalize_text(text1)
    text2 = normalize_text(text2)

    if not text1 or not text2:
        return 0.0

    # -----------------------------------------------------
    # شباهت کلمات
    # -----------------------------------------------------

    words1 = get_keywords(text1)
    words2 = get_keywords(text2)

    if words1 and words2:

        intersection = len(words1 & words2)
        union = len(words1 | words2)

        jaccard = intersection / union if union else 0

    else:
        jaccard = 0

    # -----------------------------------------------------
    # شباهت متنی
    # -----------------------------------------------------

    levenshtein = textdistance.levenshtein.normalized_similarity(
        text1,
        text2
    )

    # -----------------------------------------------------
    # ترکیب
    # -----------------------------------------------------

    similarity = (
        (jaccard * 0.65) +
        (levenshtein * 0.35)
    )

    return min(1.0, max(0.0, similarity))


# =========================================================
# دیتابیس
# =========================================================

db = Database(DATABASE_FILE)


# =========================================================
# بررسی ادمین
# =========================================================

def is_admin(user_id):

    return user_id in ADMIN_IDS


# =========================================================
# /start
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if not is_admin(user_id):

        await update.message.reply_text(
            "⛔ شما اجازه استفاده از این ربات را ندارید."
        )

        return

    await update.message.reply_text(
        "🤖 ربات تشخیص اخبار گیمفا فعال است.\n\n"
        "خبر را برای من ارسال کن تا بررسی کنم.\n\n"
        "🟢 خبر جدید → ثبت می‌شود\n"
        "🟡 خبر مشابه → درصد شباهت نمایش داده می‌شود\n"
        "🔴 خبر تکراری → اعلام می‌شود\n\n"
        "📆 فقط اخبار ۲۴ ساعت اخیر بررسی می‌شوند."
    )


# =========================================================
# /help
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if not is_admin(user_id):
        return

    await update.message.reply_text(
        "📚 راهنمای ربات\n\n"
        "خبر را مستقیماً برای ربات بفرست.\n\n"
        "🟢 خبر جدید:\n"
        "خبر قبلاً در ۲۴ ساعت اخیر ثبت نشده.\n\n"
        "🟡 خبر مشابه:\n"
        "خبر مشابهی در ۲۴ ساعت اخیر پیدا شده.\n\n"
        "🔴 خبر تکراری:\n"
        "متن خبر تقریباً یکسان است.\n\n"
        "/stats - آمار ربات\n"
        "/cleanup - پاکسازی دستی\n"
        "/help - راهنما"
    )


# =========================================================
# /stats
# =========================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if not is_admin(user_id):
        return

    db.cleanup_old_news()

    news = db.get_recent_news()

    await update.message.reply_text(
        f"📊 آمار ربات\n\n"
        f"📰 اخبار ذخیره‌شده در ۲۴ ساعت اخیر: {len(news)}\n"
        f"🎯 آستانه شباهت: {SIMILARITY_THRESHOLD * 100:.0f}%\n"
        f"📆 مدت نگهداری: {KEEP_HOURS} ساعت"
    )


# =========================================================
# /cleanup
# =========================================================

async def cleanup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if not is_admin(user_id):
        return

    db.cleanup_old_news()

    await update.message.reply_text(
        "🧹 اخبار قدیمی پاک شدند.\n"
        "📆 فقط اخبار ۲۴ ساعت اخیر نگه داشته می‌شوند."
    )


# =========================================================
# پردازش خبر
# =========================================================

async def process_news(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if not is_admin(user_id):

        await update.message.reply_text(
            "⛔ شما اجازه ارسال خبر ندارید."
        )

        return

    # -----------------------------------------------------
    # دریافت متن
    # -----------------------------------------------------

    text = update.message.text

    if not text:

        await update.message.reply_text(
            "❌ متن خبر پیدا نشد."
        )

        return

    text = text.strip()

    if len(text) < 10:

        await update.message.reply_text(
            "❌ متن خبر خیلی کوتاه است."
        )

        return

    # -----------------------------------------------------
    # پاک کردن اخبار قدیمی
    # -----------------------------------------------------

    db.cleanup_old_news()

    # -----------------------------------------------------
    # نرمال‌سازی
    # -----------------------------------------------------

    normalized = normalize_text(text)

    if not normalized:

        await update.message.reply_text(
            "❌ متن قابل بررسی نیست."
        )

        return

    # -----------------------------------------------------
    # بررسی هش دقیق
    # -----------------------------------------------------

    text_hash = hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()

    exact_match = db.exists_exact(text_hash)

    if exact_match:

        await update.message.reply_text(
            "🔴 خبر تکراری است!\n\n"
            f"📊 شباهت: 100%\n"
            f"🆔 خبر قبلی: {exact_match[0]}\n"
            f"📅 ثبت شده: {exact_match[2][:19]}"
        )

        return

    # -----------------------------------------------------
    # بررسی شباهت
    # -----------------------------------------------------

    previous_news = db.get_recent_news()

    best_similarity = 0
    best_news = None

    for news_id, old_text, created_at in previous_news:

        similarity = calculate_similarity(
            normalized,
            old_text
        )

        if similarity > best_similarity:

            best_similarity = similarity
            best_news = (
                news_id,
                old_text,
                created_at
            )

    # -----------------------------------------------------
    # خبر مشابه
    # -----------------------------------------------------

    if best_news and best_similarity >= SIMILARITY_THRESHOLD:

        similarity_percent = best_similarity * 100

        await update.message.reply_text(
            "🟡 خبر مشابه پیدا شد!\n\n"
            f"📊 میزان شباهت: {similarity_percent:.1f}%\n"
            f"🆔 خبر قبلی: {best_news[0]}\n"
            f"📅 تاریخ: {best_news[2][:19]}\n\n"
            "⚠️ قبل از انتشار بررسی کن که خبر تکراری نباشد."
        )

        return

    # -----------------------------------------------------
    # خبر جدید
    # -----------------------------------------------------

    news_id = db.add_news(normalized)

    await update.message.reply_text(
        "🟢 خبر جدید است!\n\n"
        f"🆔 شناسه: {news_id}\n"
        f"📊 بیشترین شباهت: {best_similarity * 100:.1f}%\n"
        "📆 محدوده بررسی: ۲۴ ساعت اخیر\n\n"
        "✅ می‌توانی خبر را منتشر کنی."
    )


# =========================================================
# اجرای ربات
# =========================================================

def main():

    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":

        print(
            "❌ ابتدا BOT_TOKEN را در bot.py وارد کنید."
        )

        return

    if not ADMIN_IDS:

        print(
            "❌ حداقل یک ADMIN_ID وارد کنید."
        )

        return

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # دستورات
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("stats", stats)
    )

    application.add_handler(
        CommandHandler("cleanup", cleanup)
    )

    # دریافت خبر
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            process_news
        )
    )

    print("================================")
    print("🤖 Gamfa News Checker")
    print("🚀 Bot started")
    print("================================")

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
