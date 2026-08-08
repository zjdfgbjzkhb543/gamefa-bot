import os
import re
import sqlite3
import hashlib
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# تنظیمات
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
]

DATABASE = "news.db"

# فقط اخبار 24 ساعت اخیر بررسی می‌شوند
KEEP_HOURS = 24

# درصد شباهت برای تشخیص خبر مشابه
SIMILARITY_THRESHOLD = 0.75

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# =========================
# بررسی تنظیمات
# =========================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN در Variables تنظیم نشده است."
    )

if not ADMIN_IDS:
    raise RuntimeError(
        "ADMIN_IDS در Variables تنظیم نشده است."
    )


# =========================
# دیتابیس
# =========================

def init_database():
    conn = sqlite3.connect(DATABASE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


def normalize_text(text):
    text = text.lower()

    # تبدیل اعداد انگلیسی به فارسی
    translation = str.maketrans(
        "0123456789",
        "۰۱۲۳۴۵۶۷۸۹"
    )
    text = text.translate(translation)

    # حذف لینک
    text = re.sub(r"https?://\S+", " ", text)

    # حذف کاراکترهای اضافی
    text = re.sub(r"[^\w\s\u0600-\u06FF]", " ", text)

    # حذف فاصله‌های اضافه
    text = " ".join(text.split())

    return text


def text_hash(text):
    normalized = normalize_text(text)

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def cleanup_old_news():
    conn = sqlite3.connect(DATABASE)

    cutoff = datetime.now() - timedelta(hours=KEEP_HOURS)

    conn.execute(
        """
        DELETE FROM news
        WHERE created_at < ?
        """,
        (cutoff,)
    )

    conn.commit()
    conn.close()


# =========================
# تشخیص شباهت
# =========================

def similarity(text1, text2):
    text1 = normalize_text(text1)
    text2 = normalize_text(text2)

    words1 = set(text1.split())
    words2 = set(text2.split())

    if not words1 or not words2:
        return 0

    intersection = words1.intersection(words2)
    union = words1.union(words2)

    return len(intersection) / len(union)


def find_duplicate(text):
    cleanup_old_news()

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cutoff = datetime.now() - timedelta(hours=KEEP_HOURS)

    cursor.execute(
        """
        SELECT id, text, created_at
        FROM news
        WHERE created_at >= ?
        ORDER BY created_at DESC
        """,
        (cutoff,)
    )

    rows = cursor.fetchall()

    conn.close()

    current_hash = text_hash(text)

    for news_id, old_text, created_at in rows:

        # بررسی دقیق
        if text_hash(old_text) == current_hash:
            return {
                "duplicate": True,
                "score": 1.0,
                "id": news_id,
                "text": old_text,
                "created_at": created_at
            }

        # بررسی شباهت
        score = similarity(text, old_text)

        if score >= SIMILARITY_THRESHOLD:
            return {
                "duplicate": True,
                "score": score,
                "id": news_id,
                "text": old_text,
                "created_at": created_at
            }

    return {
        "duplicate": False
    }


def save_news(text):
    conn = sqlite3.connect(DATABASE)

    conn.execute(
        """
        INSERT INTO news (text, text_hash)
        VALUES (?, ?)
        """,
        (
            text,
            text_hash(text)
        )
    )

    conn.commit()
    conn.close()


# =========================
# بررسی ادمین
# =========================

def is_admin(user_id):
    return user_id in ADMIN_IDS


# =========================
# /start
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ شما دسترسی استفاده از این ربات را ندارید."
        )
        return

    await update.message.reply_text(
        "🤖 ربات تشخیص اخبار گیمفا فعال است.\n\n"
        "خبر را برای من ارسال کن تا بررسی کنم که "
        "در ۲۴ ساعت اخیر تکراری بوده یا نه.\n\n"
        "🟢 خبر جدید\n"
        "🔴 خبر تکراری"
    )


# =========================
# /help
# =========================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "راهنمای ربات:\n\n"
        "خبر را مستقیماً برای ربات ارسال کن.\n\n"
        "🟢 اگر مشابه خبری در ۲۴ ساعت اخیر نباشد:\n"
        "خبر جدید اعلام می‌شود.\n\n"
        "🔴 اگر مشابه باشد:\n"
        "خبر تکراری اعلام می‌شود.\n\n"
        "🗑️ اخبار قدیمی‌تر از ۲۴ ساعت به صورت خودکار حذف می‌شوند."
    )


# =========================
# دریافت خبر
# =========================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text(
            "⛔ شما دسترسی ندارید."
        )
        return

    text = update.message.text

    if not text:
        await update.message.reply_text(
            "❌ فقط پیام متنی ارسال کن."
        )
        return

    text = text.strip()

    if len(text) < 10:
        await update.message.reply_text(
            "❌ متن خبر خیلی کوتاه است."
        )
        return

    # پاک کردن اخبار قدیمی
    cleanup_old_news()

    result = find_duplicate(text)

    if result["duplicate"]:

        score = result["score"] * 100

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "❌ تکراری",
                    callback_data="duplicate"
                ),
                InlineKeyboardButton(
                    "✅ خبر جدید",
                    callback_data="new"
                )
            ]
        ])

        await update.message.reply_text(
            "🔴 خبر مشابه پیدا شد!\n\n"
            f"📊 میزان شباهت: {score:.1f}%\n"
            f"🆔 خبر قبلی: {result['id']}\n"
            f"📅 تاریخ: {result['created_at']}\n\n"
            "متن خبر قبلی:\n"
            f"{result['text'][:500]}",
            reply_markup=keyboard
        )

        return

    # ذخیره خبر جدید
    save_news(text)

    await update.message.reply_text(
        "🟢 خبر جدید است!\n\n"
        "این خبر در ۲۴ ساعت اخیر مشابهی نداشته است."
    )


# =========================
# دکمه‌ها
# =========================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True
        )
        return

    await query.answer()

    if query.data == "duplicate":

        await query.edit_message_text(
            "🔴 این خبر به عنوان «تکراری» علامت‌گذاری شد."
        )

    elif query.data == "new":

        await query.edit_message_text(
            "🟢 این خبر به عنوان «خبر جدید» تایید شد."
        )


# =========================
# /stats
# =========================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        return

    cleanup_old_news()

    conn = sqlite3.connect(DATABASE)

    cursor = conn.execute(
        "SELECT COUNT(*) FROM news"
    )

    count = cursor.fetchone()[0]

    conn.close()

    await update.message.reply_text(
        "📊 آمار ربات\n\n"
        f"📰 اخبار ذخیره‌شده: {count}\n"
        f"⏱ بازه بررسی: {KEEP_HOURS} ساعت\n"
        f"🎯 آستانه شباهت: {SIMILARITY_THRESHOLD * 100:.0f}%"
    )


# =========================
# اجرای ربات
# =========================

def main():

    logger.info("Starting Gamefa News Bot...")

    init_database()
    cleanup_old_news()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

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
        CallbackQueryHandler(button_callback)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    logger.info("Bot is running...")

    # مهم:
    # اینجا نباید asyncio.run یا await استفاده شود.
    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
