import os
import sqlite3
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

from openai import OpenAI


# =========================================================
# تنظیمات
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
]

DATABASE = "news.db"

# فقط اخبار 24 ساعت اخیر
KEEP_HOURS = 24

# اگر AI احتمال تکراری بودن را از این مقدار بیشتر بداند
# خبر به عنوان تکراری نمایش داده می‌شود.
AI_THRESHOLD = 0.80


# =========================================================
# بررسی Variables
# =========================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN در Railway Variables تنظیم نشده است."
    )

if not OPENAI_API_KEY:
    raise RuntimeError(
        "OPENAI_API_KEY در Railway Variables تنظیم نشده است."
    )

if not ADMIN_IDS:
    raise RuntimeError(
        "ADMIN_IDS در Railway Variables تنظیم نشده است."
    )


# =========================================================
# OpenAI
# =========================================================

ai = OpenAI(
    api_key=OPENAI_API_KEY
)


# =========================================================
# Logging
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# =========================================================
# Database
# =========================================================

def init_database():

    conn = sqlite3.connect(DATABASE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


def cleanup_old_news():

    cutoff = datetime.now() - timedelta(hours=KEEP_HOURS)

    conn = sqlite3.connect(DATABASE)

    conn.execute(
        """
        DELETE FROM news
        WHERE created_at < ?
        """,
        (cutoff,)
    )

    conn.commit()
    conn.close()


def save_news(text):

    conn = sqlite3.connect(DATABASE)

    conn.execute(
        """
        INSERT INTO news (text)
        VALUES (?)
        """,
        (text,)
    )

    conn.commit()
    conn.close()


def get_recent_news():

    cutoff = datetime.now() - timedelta(hours=KEEP_HOURS)

    conn = sqlite3.connect(DATABASE)

    cursor = conn.execute(
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

    return rows


# =========================================================
# بررسی ادمین
# =========================================================

def is_admin(user_id):

    return user_id in ADMIN_IDS


# =========================================================
# هوش مصنوعی
# =========================================================

def ai_check_duplicate(new_text, old_news):

    """
    خبر جدید را با اخبار قبلی مقایسه می‌کند.

    خروجی:

    {
        "duplicate": True/False,
        "score": 0 تا 1,
        "news_id": شناسه خبر مشابه,
        "reason": توضیح کوتاه
    }
    """

    if not old_news:

        return {
            "duplicate": False,
            "score": 0,
            "news_id": None,
            "reason": "هیچ خبر قبلی در ۲۴ ساعت اخیر وجود ندارد."
        }


    # حداکثر 30 خبر اخیر
    old_news = old_news[:30]

    news_text = ""

    for news_id, text, created_at in old_news:

        news_text += (
            f"\n\n--- NEWS ID: {news_id} ---\n"
            f"{text[:2000]}"
        )


    prompt = f"""
تو سیستم تشخیص اخبار تکراری برای یک کانال خبری گیمینگ هستی.

وظیفه تو فقط این است که مشخص کنی آیا «خبر جدید»
از نظر موضوع و اتفاق اصلی، با یکی از اخبار قبلی تکراری است یا نه.

مهم:

دو خبر لازم نیست کلمات یکسان داشته باشند.

مثلاً:

خبر جدید:
«سونی تاریخ انتشار بازی Ghost of Yotei را اعلام کرد»

خبر قبلی:
«بازی Ghost of Yotei در تاریخ مشخصی منتشر خواهد شد»

این‌ها یک خبر هستند.

اما:

«بازی GTA 6 تأخیر خورد»

و:

«تریلر جدید GTA 6 منتشر شد»

تکراری نیستند، چون اتفاق اصلی متفاوت است.

فقط شباهت موضوعی کافی نیست.
باید اتفاق اصلی خبر یکی باشد.

خبر جدید:

{new_text[:4000]}

اخبار قبلی:

{news_text}

نتیجه را فقط با JSON زیر برگردان:

{{
    "duplicate": true یا false,
    "score": عدد بین 0 و 1,
    "news_id": شناسه خبر مشابه یا null,
    "reason": "توضیح بسیار کوتاه فارسی"
}}

اگر خبر جدید با یکی از اخبار قبلی درباره یک اتفاق اصلی باشد:
duplicate = true

اگر فقط درباره یک بازی، فیلم، شخص یا شرکت مشابه باشد
ولی اتفاق اصلی متفاوت باشد:
duplicate = false
"""


    try:

        response = ai.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "تو یک سیستم دقیق تشخیص اخبار تکراری هستی. "
                        "فقط JSON معتبر برگردان."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0
        )


        result_text = response.choices[0].message.content.strip()

        # حذف ```json در صورت وجود
        result_text = result_text.replace(
            "```json",
            ""
        ).replace(
            "```",
            ""
        ).strip()


        import json

        result = json.loads(result_text)


        duplicate = bool(
            result.get("duplicate", False)
        )

        score = float(
            result.get("score", 0)
        )

        news_id = result.get("news_id")

        reason = result.get(
            "reason",
            "بدون توضیح"
        )


        # اطمینان از محدوده
        score = max(
            0,
            min(1, score)
        )


        # اگر AI امتیاز بالا داده
        if score >= AI_THRESHOLD:

            duplicate = True


        return {
            "duplicate": duplicate,
            "score": score,
            "news_id": news_id,
            "reason": reason
        }


    except Exception as e:

        logger.error(
            f"AI ERROR: {e}"
        )

        return {
            "duplicate": False,
            "score": 0,
            "news_id": None,
            "reason": "خطا در بررسی هوش مصنوعی"
        }


# =========================================================
# /start
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update.effective_user.id):

        await update.message.reply_text(
            "⛔ شما دسترسی به این ربات را ندارید."
        )

        return


    keyboard = [
        [
            InlineKeyboardButton(
                "📰 بررسی خبر",
                callback_data="check_info"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="stats"
            ),
            InlineKeyboardButton(
                "🗑 پاکسازی",
                callback_data="cleanup"
            )
        ]
    ]


    await update.message.reply_text(
        "🤖 ربات تشخیص اخبار گیمفا فعال است.\n\n"
        "خبر را برای من ارسال کن.\n"
        "هوش مصنوعی فقط بررسی می‌کند که خبر "
        "در ۲۴ ساعت اخیر تکراری بوده یا نه.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# /help
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(update.effective_user.id):
        return


    await update.message.reply_text(
        "راهنمای ربات:\n\n"
        "📰 خبر را برای ربات بفرست.\n\n"
        "🧠 هوش مصنوعی آن را با اخبار ۲۴ ساعت اخیر "
        "مقایسه می‌کند.\n\n"
        "🟢 خبر جدید\n"
        "🔴 خبر تکراری\n\n"
        "هیچ خبری توسط ربات به کانال ارسال نمی‌شود."
    )


# =========================================================
# دریافت خبر
# =========================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id


    if not is_admin(user_id):

        await update.message.reply_text(
            "⛔ شما دسترسی ندارید."
        )

        return


    text = update.message.text


    if not text:

        await update.message.reply_text(
            "❌ فقط متن خبر را ارسال کن."
        )

        return


    text = text.strip()


    if len(text) < 10:

        await update.message.reply_text(
            "❌ متن خبر خیلی کوتاه است."
        )

        return


    await update.message.reply_text(
        "🧠 در حال بررسی خبر با هوش مصنوعی..."
    )


    # پاک کردن اخبار قدیمی
    cleanup_old_news()


    # دریافت اخبار 24 ساعت اخیر
    recent_news = get_recent_news()


    # بررسی AI
    result = ai_check_duplicate(
        text,
        recent_news
    )


    # =====================================================
    # خبر تکراری
    # =====================================================

    if result["duplicate"]:

        score = result["score"] * 100

        old_news_text = ""

        for news_id, old_text, created_at in recent_news:

            if str(news_id) == str(
                result["news_id"]
            ):

                old_news_text = old_text

                break


        keyboard = [
            [
                InlineKeyboardButton(
                    "🔴 تکراری",
                    callback_data="mark_duplicate"
                ),
                InlineKeyboardButton(
                    "🟢 خبر جدید",
                    callback_data="mark_new"
                )
            ]
        ]


        await update.message.reply_text(
            "🔴 خبر احتمالاً تکراری است.\n\n"
            f"📊 میزان اطمینان AI: {score:.1f}%\n"
            f"🆔 خبر مشابه: {result['news_id']}\n\n"
            f"🤖 دلیل:\n{result['reason']}\n\n"
            "📰 خبر قبلی:\n"
            f"{old_news_text[:1000] if old_news_text else 'پیدا نشد'}\n\n"
            "تصمیم نهایی را با دکمه انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        return


    # =====================================================
    # خبر جدید
    # =====================================================

    save_news(text)


    keyboard = [
        [
            InlineKeyboardButton(
                "✅ تأیید خبر جدید",
                callback_data="confirm_new"
            )
        ]
    ]


    await update.message.reply_text(
        "🟢 خبر جدید است!\n\n"
        f"🤖 اطمینان AI: "
        f"{(1 - result['score']) * 100:.1f}%\n\n"
        "این خبر مشابهی در ۲۴ ساعت اخیر ندارد.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# دکمه‌ها
# =========================================================

async def button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query


    if not is_admin(query.from_user.id):

        await query.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True
        )

        return


    await query.answer()


    # -----------------------------------------------------
    # اطلاعات
    # -----------------------------------------------------

    if query.data == "check_info":

        await query.edit_message_text(
            "📰 یک خبر متنی برای ربات بفرست.\n\n"
            "هوش مصنوعی فقط آن را با اخبار "
            "۲۴ ساعت اخیر مقایسه می‌کند."
        )


    # -----------------------------------------------------
    # آمار
    # -----------------------------------------------------

    elif query.data == "stats":

        cleanup_old_news()

        conn = sqlite3.connect(DATABASE)

        cursor = conn.execute(
            "SELECT COUNT(*) FROM news"
        )

        count = cursor.fetchone()[0]

        conn.close()


        await query.edit_message_text(
            "📊 آمار ربات\n\n"
            f"📰 اخبار ذخیره‌شده: {count}\n"
            f"⏱ بازه بررسی: {KEEP_HOURS} ساعت\n"
            f"🧠 تشخیص: هوش مصنوعی"
        )


    # -----------------------------------------------------
    # پاکسازی
    # -----------------------------------------------------

    elif query.data == "cleanup":

        cleanup_old_news()

        await query.edit_message_text(
            "🗑 اخبار قدیمی پاکسازی شدند."
        )


    # -----------------------------------------------------
    # تکراری
    # -----------------------------------------------------

    elif query.data == "mark_duplicate":

        await query.edit_message_text(
            "🔴 خبر به عنوان «تکراری» تأیید شد."
        )


    # -----------------------------------------------------
    # خبر جدید
    # -----------------------------------------------------

    elif query.data == "mark_new":

        await query.edit_message_text(
            "🟢 تصمیم AI اصلاح شد.\n\n"
            "این خبر به عنوان «خبر جدید» تأیید شد."
        )


    # -----------------------------------------------------
    # تأیید خبر جدید
    # -----------------------------------------------------

    elif query.data == "confirm_new":

        await query.edit_message_text(
            "✅ خبر جدید تأیید شد."
        )


# =========================================================
# اجرای ربات
# =========================================================

def main():

    logger.info(
        "🚀 Starting Gamefa AI News Bot..."
    )


    init_database()

    cleanup_old_news()


    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )


    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )


    application.add_handler(
        CallbackQueryHandler(
            button_callback
        )
    )


    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )


    logger.info(
        "🤖 Bot is running..."
    )


    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
