import os
import re
import json
import hashlib
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import List, Dict, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from openai import AsyncOpenAI


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini").strip()

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()

SIMILARITY_THRESHOLD = float(
    os.getenv("SIMILARITY_THRESHOLD", "0.82")
)

MAX_STORED_NEWS = int(
    os.getenv("MAX_STORED_NEWS", "500")
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("GamefaDuplicateBot")


# =========================================================
# VALIDATION
# =========================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is missing."
    )


def parse_admins(value: str) -> set:
    result = set()

    for item in value.split(","):
        item = item.strip()

        if not item:
            continue

        try:
            result.add(int(item))
        except ValueError:
            logger.warning(
                "Invalid ADMIN_IDS value ignored: %s",
                item
            )

    return result


ADMIN_IDS = parse_admins(ADMIN_IDS_RAW)


# =========================================================
# OPENAI
# =========================================================

ai_client: Optional[AsyncOpenAI] = None

if OPENAI_API_KEY:
    ai_client = AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )

    logger.info("OpenAI AI detection is enabled.")

else:
    logger.warning(
        "OPENAI_API_KEY is not set. "
        "Bot will use local duplicate detection only."
    )


# =========================================================
# IN-MEMORY NEWS STORAGE
# =========================================================
#
# فقط دو فایل پروژه داریم.
# بنابراین دیتابیس جداگانه وجود ندارد.
#
# اخبار در RAM نگه داشته می‌شوند.
#
# نکته:
# با Restart شدن Railway، لیست اخبار از بین می‌رود.
# =========================================================

news_storage: List[Dict] = []


# =========================================================
# TEXT NORMALIZATION
# =========================================================

def normalize_text(text: str) -> str:
    """
    نرمال‌سازی بسیار جدی برای جلوگیری از مشکل
    تشخیص ندادن خبر کاملاً مشابه.
    """

    if not text:
        return ""

    text = str(text)

    # حذف URL
    text = re.sub(
        r"https?://\S+|www\.\S+",
        " ",
        text,
        flags=re.IGNORECASE,
    )

    # حذف منشن
    text = re.sub(
        r"@\w+",
        " ",
        text,
    )

    # تبدیل حروف عربی به فارسی
    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ئ": "ی",
        "ك": "ک",
        "ة": "ه",
        "ۀ": "ه",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا",
        "ـ": "",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # اعداد فارسی/عربی به انگلیسی
    persian_digits = "۰۱۲۳۴۵۶۷۸۹"
    arabic_digits = "٠١٢٣٤٥٦٧٨٩"
    english_digits = "0123456789"

    translation = {}

    for p, e in zip(persian_digits, english_digits):
        translation[ord(p)] = e

    for a, e in zip(arabic_digits, english_digits):
        translation[ord(a)] = e

    text = text.translate(translation)

    # حذف نیم‌فاصله
    text = text.replace("\u200c", " ")
    text = text.replace("\u200d", " ")

    # حذف ایموجی و کاراکترهای اضافی
    text = re.sub(
        r"[^\w\sآ-ی]",
        " ",
        text,
        flags=re.UNICODE,
    )

    # کوچک کردن حروف انگلیسی
    text = text.lower()

    # حذف فاصله‌های اضافی
    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


# =========================================================
# EXACT HASH
# =========================================================

def text_hash(text: str) -> str:
    normalized = normalize_text(text)

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


# =========================================================
# TOKENIZATION
# =========================================================

STOP_WORDS = {
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
    "شود",
    "کرد",
    "می",
    "نیز",
    "هم",
    "یا",
    "بر",
    "تا",
    "اما",
    "اگر",
    "یک",
    "های",
    "ها",
    "روی",
    "همین",
    "دارد",
    "دارد",
}


def get_words(text: str) -> set:
    normalized = normalize_text(text)

    words = normalized.split()

    return {
        word
        for word in words
        if len(word) >= 2
        and word not in STOP_WORDS
    }


# =========================================================
# LOCAL SIMILARITY
# =========================================================

def jaccard_similarity(
    text1: str,
    text2: str,
) -> float:

    words1 = get_words(text1)
    words2 = get_words(text2)

    if not words1 or not words2:
        return 0.0

    intersection = len(words1 & words2)
    union = len(words1 | words2)

    if union == 0:
        return 0.0

    return intersection / union


def sequence_similarity(
    text1: str,
    text2: str,
) -> float:

    a = normalize_text(text1)
    b = normalize_text(text2)

    if not a or not b:
        return 0.0

    return SequenceMatcher(
        None,
        a,
        b,
    ).ratio()


def local_similarity(
    text1: str,
    text2: str,
) -> float:

    jaccard = jaccard_similarity(
        text1,
        text2,
    )

    sequence = sequence_similarity(
        text1,
        text2,
    )

    # ترکیب دو الگوریتم
    return (
        jaccard * 0.55
        +
        sequence * 0.45
    )


# =========================================================
# REMOVE OLD NEWS
# =========================================================

def cleanup_old_news():
    """
    فقط اخبار مربوط به امروز و دیروز نگه داشته می‌شوند.
    """

    global news_storage

    now = datetime.now(timezone.utc)

    cutoff = now - timedelta(days=1)

    before = len(news_storage)

    news_storage = [
        news
        for news in news_storage
        if news["created_at"] >= cutoff
    ]

    removed = before - len(news_storage)

    if removed:
        logger.info(
            "Removed %s old news.",
            removed
        )


# =========================================================
# ADD NEWS
# =========================================================

def add_news(text: str, user_id: int):
    cleanup_old_news()

    item = {
        "id": hashlib.md5(
            f"{datetime.now().timestamp()}-{text}".encode()
        ).hexdigest()[:10],

        "text": text,

        "normalized": normalize_text(text),

        "hash": text_hash(text),

        "created_at": datetime.now(
            timezone.utc
        ),

        "user_id": user_id,
    }

    news_storage.insert(
        0,
        item,
    )

    # محدودیت امنیتی
    if len(news_storage) > MAX_STORED_NEWS:
        del news_storage[MAX_STORED_NEWS:]


# =========================================================
# FIND EXACT DUPLICATE
# =========================================================

def find_exact_duplicate(
    text: str,
) -> Optional[Dict]:

    current_hash = text_hash(text)

    for news in news_storage:

        if news["hash"] == current_hash:
            return news

    return None


# =========================================================
# FIND LOCAL SIMILAR NEWS
# =========================================================

def find_local_similar(
    text: str,
) -> List[Dict]:

    results = []

    for news in news_storage:

        score = local_similarity(
            text,
            news["text"],
        )

        if score >= 0.60:

            results.append(
                {
                    "news": news,
                    "score": score,
                }
            )

    results.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    return results[:5]


# =========================================================
# AI DUPLICATE DETECTION
# =========================================================

async def ai_check_duplicate(
    new_text: str,
    old_news: List[Dict],
) -> Optional[Dict]:

    if not ai_client:
        return None

    if not old_news:
        return None

    candidates = []

    for index, item in enumerate(old_news):

        candidates.append(
            f"""
خبر قبلی شماره {index + 1}:
{item["text"]}
"""
        )

    candidates_text = "\n".join(
        candidates
    )

    prompt = f"""
تو سیستم تشخیص خبر تکراری یک کانال خبری گیمینگ هستی.

خبر جدید:
{new_text}

خبرهای قبلی:
{candidates_text}

وظیفه:
بررسی کن آیا خبر جدید درباره همان رویداد خبری
یکی از خبرهای قبلی است یا نه.

مهم:
- فقط به شباهت کلمات نگاه نکن.
- اگر دو متن با کلمات متفاوت یک اتفاق واحد را گزارش می‌کنند،
  آن‌ها را تکراری در نظر بگیر.
- اگر موضوع فقط مشابه است ولی رویداد متفاوت است،
  تکراری حساب نکن.
- اگر همان خبر با تیتر متفاوت نوشته شده،
  تکراری حساب کن.
- اگر فقط درباره یک بازی/فیلم/بازیگر مشابه صحبت می‌کنند
  ولی اتفاق متفاوت است، تکراری نیست.

فقط JSON معتبر برگردان:

{{
  "duplicate": true,
  "match_index": 1,
  "confidence": 0.95,
  "reason": "دلیل کوتاه فارسی"
}}

اگر هیچ خبر قبلی تکراری نیست:

{{
  "duplicate": false,
  "match_index": null,
  "confidence": 0.0,
  "reason": "خبر جدید است"
}}
"""

    try:

        response = await ai_client.responses.create(
            model=AI_MODEL,
            input=prompt,
        )

        result_text = response.output_text.strip()

        # حذف احتمالی Markdown
        result_text = re.sub(
            r"```json|```",
            "",
            result_text,
            flags=re.IGNORECASE,
        ).strip()

        result = json.loads(
            result_text
        )

        return result

    except Exception as e:

        logger.exception(
            "AI duplicate detection failed: %s",
            e,
        )

        return None


# =========================================================
# MAIN DUPLICATE ENGINE
# =========================================================

async def check_duplicate(
    text: str,
) -> Dict:

    cleanup_old_news()

    # -----------------------------------------------------
    # مرحله 1:
    # EXACT HASH
    # -----------------------------------------------------

    exact = find_exact_duplicate(
        text
    )

    if exact:

        return {
            "duplicate": True,
            "type": "exact",
            "confidence": 1.0,
            "match": exact,
            "reason": "این خبر دقیقاً قبلاً ارسال شده است.",
        }

    # -----------------------------------------------------
    # مرحله 2:
    # LOCAL SIMILARITY
    # -----------------------------------------------------

    similar = find_local_similar(
        text
    )

    # اگر شباهت خیلی بالا باشد،
    # بدون نیاز به AI هم تکراری محسوب می‌شود.
    if similar:

        best = similar[0]

        if best["score"] >= 0.90:

            return {
                "duplicate": True,
                "type": "local",
                "confidence": best["score"],
                "match": best["news"],
                "reason": "شباهت متنی بسیار بالا است.",
            }

    # -----------------------------------------------------
    # مرحله 3:
    # AI
    # -----------------------------------------------------

    candidates = []

    # فقط کاندیدهای نزدیک را به AI می‌دهیم
    for item in similar[:5]:
        candidates.append(
            item["news"]
        )

    # اگر هیچ کاندید مناسبی نبود،
    # AI را با تمام اخبار دیروز مقایسه می‌کنیم.
    if not candidates:
        candidates = news_storage[:20]

    if candidates:

        ai_result = await ai_check_duplicate(
            text,
            candidates,
        )

        if ai_result:

            if ai_result.get("duplicate") is True:

                index = ai_result.get(
                    "match_index"
                )

                match = None

                if (
                    isinstance(index, int)
                    and 1 <= index <= len(candidates)
                ):
                    match = candidates[
                        index - 1
                    ]

                return {
                    "duplicate": True,
                    "type": "ai",
                    "confidence": float(
                        ai_result.get(
                            "confidence",
                            0.0
                        )
                    ),
                    "match": match,
                    "reason": ai_result.get(
                        "reason",
                        "هوش مصنوعی این خبر را تکراری تشخیص داد."
                    ),
                }

    # -----------------------------------------------------
    # NEW
    # -----------------------------------------------------

    return {
        "duplicate": False,
        "type": "new",
        "confidence": 0.0,
        "match": None,
        "reason": "خبر جدید است.",
    }


# =========================================================
# AUTH
# =========================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# =========================================================
# KEYBOARD
# =========================================================

def main_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📰 بررسی خبر",
                    callback_data="check_info",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📊 آمار",
                    callback_data="stats",
                ),
                InlineKeyboardButton(
                    "🗑 پاکسازی",
                    callback_data="cleanup",
                ),
            ],
        ]
    )


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user:
        return

    if not is_admin(user.id):

        await update.message.reply_text(
            "⛔ شما دسترسی استفاده از این ربات را ندارید."
        )

        return

    cleanup_old_news()

    await update.message.reply_text(
        "🤖 ربات تشخیص اخبار تکراری گیمفا\n\n"
        "خبر را همین‌جا برای من بفرست.\n"
        "من بررسی می‌کنم که آیا قبلاً همین خبر "
        "یا همان رویداد با بیان متفاوت ارسال شده است یا نه.\n\n"
        "🧠 تشخیص دقیق + هوش مصنوعی\n"
        "📅 نگهداری اخبار: فقط دیروز\n"
        "📤 ارسال خودکار به کانال: غیرفعال",
        reply_markup=main_keyboard(),
    )


# =========================================================
# /STATS
# =========================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    cleanup_old_news()

    await update.message.reply_text(
        "📊 آمار ربات\n\n"
        f"📰 اخبار ذخیره‌شده: {len(news_storage)}\n"
        f"👥 تعداد ادمین‌ها: {len(ADMIN_IDS)}\n"
        f"🧠 هوش مصنوعی: "
        f"{'فعال' if ai_client else 'غیرفعال'}\n"
        f"📅 بازه نگهداری: ۱ روز",
        reply_markup=main_keyboard(),
    )


# =========================================================
# /CLEAN
# =========================================================

async def clean_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    before = len(news_storage)

    cleanup_old_news()

    removed = before - len(news_storage)

    await update.message.reply_text(
        f"🗑 پاکسازی انجام شد.\n\n"
        f"حذف‌شده: {removed}\n"
        f"باقی‌مانده: {len(news_storage)}"
    )


# =========================================================
# HANDLE NEWS
# =========================================================

async def handle_news(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user or not is_admin(user.id):

        await update.message.reply_text(
            "⛔ شما دسترسی ارسال خبر ندارید."
        )

        return

    text = (
        update.message.text
        or update.message.caption
        or ""
    ).strip()

    if not text:

        await update.message.reply_text(
            "❌ متن خبر پیدا نشد."
        )

        return

    if len(text) < 5:

        await update.message.reply_text(
            "❌ متن خبر خیلی کوتاه است."
        )

        return

    # پیام وضعیت
    checking_message = await update.message.reply_text(
        "🔎 در حال بررسی خبر...\n\n"
        "1️⃣ بررسی تکراری دقیق\n"
        "2️⃣ بررسی شباهت متنی\n"
        "3️⃣ بررسی هوش مصنوعی"
    )

    try:

        result = await check_duplicate(
            text
        )

        if result["duplicate"]:

            match = result.get(
                "match"
            )

            confidence = (
                result["confidence"]
                * 100
            )

            duplicate_type = result["type"]

            if duplicate_type == "exact":
                method = "تطبیق دقیق"
            elif duplicate_type == "local":
                method = "شباهت متنی"
            else:
                method = "هوش مصنوعی"

            old_text = (
                match["text"]
                if match
                else "خبر قبلی پیدا نشد."
            )

            if len(old_text) > 800:
                old_text = old_text[:800] + "..."

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔍 بررسی دوباره با AI",
                            callback_data="ai_again",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🆕 این خبر جدید است",
                            callback_data="force_new",
                        )
                    ],
                ]
            )

            await checking_message.edit_text(
                "🔴 خبر تکراری است!\n\n"
                f"🎯 روش تشخیص: {method}\n"
                f"📊 اطمینان: {confidence:.1f}%\n\n"
                f"🧠 دلیل:\n"
                f"{result['reason']}\n\n"
                "📰 خبر قبلی:\n"
                f"{old_text}",
                reply_markup=keyboard,
            )

        else:

            add_news(
                text,
                user.id,
            )

            await checking_message.edit_text(
                "🟢 خبر جدید است!\n\n"
                "این خبر در آرشیو دیروز پیدا نشد "
                "و می‌تواند به عنوان خبر جدید استفاده شود.\n\n"
                "📥 خبر برای بررسی‌های بعدی ذخیره شد.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "📊 آمار",
                                callback_data="stats",
                            ),
                        ]
                    ]
                ),
            )

    except Exception as e:

        logger.exception(
            "Error processing news: %s",
            e,
        )

        await checking_message.edit_text(
            "❌ هنگام بررسی خبر خطایی رخ داد.\n\n"
            "لطفاً دوباره تلاش کن."
        )


# =========================================================
# CALLBACKS
# =========================================================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    user = update.effective_user

    if not user or not is_admin(user.id):

        await query.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True,
        )

        return

    data = query.data

    # -----------------------------------------------------
    # INFO
    # -----------------------------------------------------

    if data == "check_info":

        await query.edit_message_text(
            "📰 بررسی خبر\n\n"
            "کافی است متن خبر را برای ربات ارسال کنی.\n\n"
            "ربات ابتدا Hash دقیق را بررسی می‌کند، "
            "بعد شباهت متنی و در نهایت هوش مصنوعی "
            "را برای تشخیص خبرهایی که با بیان متفاوت "
            "همان اتفاق را گزارش می‌کنند به کار می‌گیرد."
        )

        return

    # -----------------------------------------------------
    # STATS
    # -----------------------------------------------------

    if data == "stats":

        cleanup_old_news()

        await query.edit_message_text(
            "📊 آمار ربات\n\n"
            f"📰 اخبار ذخیره‌شده: {len(news_storage)}\n"
            f"👥 ادمین‌ها: {len(ADMIN_IDS)}\n"
            f"🧠 AI: "
            f"{'فعال' if ai_client else 'غیرفعال'}\n"
            "📅 نگهداری: فقط دیروز",
            reply_markup=main_keyboard(),
        )

        return

    # -----------------------------------------------------
    # CLEANUP
    # -----------------------------------------------------

    if data == "cleanup":

        before = len(news_storage)

        cleanup_old_news()

        removed = before - len(news_storage)

        await query.edit_message_text(
            "🗑 پاکسازی انجام شد.\n\n"
            f"حذف‌شده: {removed}\n"
            f"باقی‌مانده: {len(news_storage)}",
            reply_markup=main_keyboard(),
        )

        return

    # -----------------------------------------------------
    # FORCE NEW
    # -----------------------------------------------------

    if data == "force_new":

        await query.edit_message_text(
            "🟢 ثبت شد.\n\n"
            "این خبر به عنوان خبر جدید پذیرفته شد."
        )

        return

    # -----------------------------------------------------
    # AI AGAIN
    # -----------------------------------------------------

    if data == "ai_again":

        await query.edit_message_text(
            "🧠 برای بررسی دوباره، "
            "خود خبر را مجدداً ارسال کن."
        )

        return


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "Unhandled exception:",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    logger.info(
        "Starting Gamefa Duplicate News Bot..."
    )

    logger.info(
        "Admins: %s",
        ADMIN_IDS,
    )

    logger.info(
        "AI: %s",
        "enabled" if ai_client else "disabled",
    )

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            stats,
        )
    )

    app.add_handler(
        CommandHandler(
            "clean",
            clean_command,
        )
    )

    # Buttons
    app.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    # News
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_news,
        )
    )

    # Errors
    app.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot is running..."
    )

    app.run_polling(
        drop_pending_updates=True
    )


# =========================================================

if __name__ == "__main__":
    main()
