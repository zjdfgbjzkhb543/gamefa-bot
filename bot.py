import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from openai import AsyncOpenAI


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

OPENAI_MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-4.1-mini"
).strip()

EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "text-embedding-3-small"
).strip()


# ============================================================
# FILE STORAGE
# ============================================================

DATA_DIR = Path(os.getenv("DATA_DIR", "."))

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)

NEWS_FILE = DATA_DIR / "news.json"


if not NEWS_FILE.exists():
    NEWS_FILE.write_text(
        "[]",
        encoding="utf-8"
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("gamefa-bot")


# ============================================================
# VALIDATION
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not configured."
    )


# ============================================================
# TELEGRAM / OPENAI
# ============================================================

bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher()

router = Router()

dp.include_router(router)


openai_client: Optional[AsyncOpenAI] = None

if OPENAI_API_KEY:
    openai_client = AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )
else:
    logger.warning(
        "OPENAI_API_KEY is not configured. AI features will be disabled."
    )


# ============================================================
# TEMPORARY PENDING NEWS
# ============================================================

PENDING_NEWS = {}


# ============================================================
# DATABASE-LIKE JSON FUNCTIONS
# ============================================================

def load_news():
    try:
        if not NEWS_FILE.exists():
            return []

        content = NEWS_FILE.read_text(
            encoding="utf-8"
        ).strip()

        if not content:
            return []

        data = json.loads(content)

        if not isinstance(data, list):
            return []

        return data

    except Exception:
        logger.exception(
            "Failed to load news.json"
        )

        return []


def save_news(news):
    try:

        temporary_file = NEWS_FILE.with_suffix(
            ".tmp"
        )

        temporary_file.write_text(
            json.dumps(
                news,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        temporary_file.replace(
            NEWS_FILE
        )

        return True

    except Exception:
        logger.exception(
            "Failed to save news.json"
        )

        return False


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin(user_id: int) -> bool:

    return user_id in ADMIN_IDS


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize_text(text: str) -> str:

    if not text:
        return ""

    text = text.lower()

    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ۀ": "ه",
        "ة": "ه",
        "\u200c": " ",
        "\u200f": " ",
        "\u200e": " ",
    }

    for old, new in replacements.items():
        text = text.replace(
            old,
            new
        )

    # Remove URLs
    text = re.sub(
        r"https?://\S+",
        " ",
        text
    )

    # Remove Telegram usernames / hashtags
    text = re.sub(
        r"[@#][\w_]+",
        " ",
        text
    )

    # Keep Persian/English/numbers
    text = re.sub(
        r"[^\w\s\u0600-\u06ff]",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ============================================================
# LEXICAL SIMILARITY
# ============================================================

def lexical_similarity(
    text_a: str,
    text_b: str
) -> float:

    a = normalize_text(text_a)
    b = normalize_text(text_b)

    if not a or not b:
        return 0.0

    words_a = set(a.split())
    words_b = set(b.split())

    union = words_a | words_b
    intersection = words_a & words_b

    jaccard = (
        len(intersection) /
        max(1, len(union))
    )

    sequence = SequenceMatcher(
        None,
        a,
        b
    ).ratio()

    score = (
        jaccard * 0.55 +
        sequence * 0.45
    )

    return max(
        0.0,
        min(
            1.0,
            score
        )
    )


# ============================================================
# COSINE SIMILARITY
# ============================================================

def cosine_similarity(
    vector_a,
    vector_b
) -> float:

    if not vector_a or not vector_b:
        return 0.0

    if len(vector_a) != len(vector_b):
        return 0.0

    dot = sum(
        a * b
        for a, b in zip(
            vector_a,
            vector_b
        )
    )

    magnitude_a = sum(
        a * a
        for a in vector_a
    ) ** 0.5

    magnitude_b = sum(
        b * b
        for b in vector_b
    ) ** 0.5

    if magnitude_a == 0 or magnitude_b == 0:
        return 0.0

    return dot / (
        magnitude_a *
        magnitude_b
    )


# ============================================================
# OPENAI EMBEDDING
# ============================================================

async def create_embedding(
    text: str
):

    if not openai_client:
        return None

    try:

        text = text[:8000]

        response = await openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text
        )

        return response.data[0].embedding

    except Exception:
        logger.exception(
            "Embedding request failed"
        )

        return None


# ============================================================
# OPENAI NEWS ANALYSIS
# ============================================================

async def analyze_news_with_ai(
    text: str
):

    if not openai_client:
        return None

    system_prompt = """
تو دستیار هوشمند تحریریه گیمفا هستی.

متن خبر را تحلیل کن و فقط JSON معتبر برگردان.

ساختار JSON:

{
  "title": "",
  "category": "",
  "subject": "",
  "event": "",
  "entities": [],
  "keywords": [],
  "summary": ""
}

category فقط یکی از این موارد باشد:

بازی
فیلم و سریال
فناوری
هوش مصنوعی
سایر

title:
یک عنوان کوتاه فارسی برای خبر.

subject:
موضوع اصلی خبر.

event:
مهم‌ترین اتفاق خبر.

entities:
نام بازی‌ها، فیلم‌ها، سریال‌ها،
شرکت‌ها، استودیوها و افراد مهم.

keywords:
کلمات کلیدی مهم.

summary:
یک خلاصه کوتاه فارسی.

هیچ متن اضافه‌ای خارج از JSON ننویس.
"""

    try:

        response = await openai_client.chat.completions.create(

            model=OPENAI_MODEL,

            temperature=0,

            response_format={
                "type": "json_object"
            },

            messages=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": text[:12000]
                }
            ]
        )

        content = response.choices[0].message.content

        if not content:
            return None

        return json.loads(content)

    except Exception:
        logger.exception(
            "AI analysis failed"
        )

        return None


# ============================================================
# TITLE EXTRACTION
# ============================================================

def extract_title(text: str) -> str:

    if not text:
        return "بدون عنوان"

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines:
        return text[:200]

    return lines[0][:500]


# ============================================================
# TELEGRAM LINK
# ============================================================

def get_message_link(
    message: Message
):

    try:

        if (
            message.chat and
            message.chat.username
        ):

            return (
                f"https://t.me/"
                f"{message.chat.username}/"
                f"{message.message_id}"
            )

    except Exception:
        pass

    return None


# ============================================================
# MESSAGE TEXT
# ============================================================

def get_message_text(
    message: Message
):

    return (
        message.text or
        message.caption or
        ""
    ).strip()


# ============================================================
# FIND SIMILAR NEWS
# ============================================================

async def find_similar_news(
    text: str,
    limit: int = 5
):

    news = load_news()

    if not news:
        return []

    new_embedding = await create_embedding(
        text
    )

    results = []

    # آخر 2000 خبر برای جلوگیری از سنگینی بیش از حد
    recent_news = news[-2000:]

    for item in recent_news:

        old_text = item.get(
            "text",
            ""
        )

        lexical = lexical_similarity(
            text,
            old_text
        )

        semantic = 0.0

        if (
            new_embedding and
            item.get("embedding")
        ):

            semantic = cosine_similarity(
                new_embedding,
                item["embedding"]
            )

        if new_embedding and item.get("embedding"):

            final_score = (
                semantic * 0.75 +
                lexical * 0.25
            )

        else:

            final_score = lexical

        if final_score >= 0.30:

            results.append(
                {
                    "score": final_score,
                    "news": item
                }
            )

    results.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return results[:limit]


# ============================================================
# KEYBOARD
# ============================================================

def close_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ بستن",
                    callback_data="close"
                )
            ]
        ]
    )


def new_news_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ ثبت خبر",
                    callback_data="save_pending"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ لغو",
                    callback_data="close"
                )
            ]
        ]
    )


def duplicate_keyboard(
    index: int
):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 مشاهده خبر مشابه",
                    callback_data=f"view:{index}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚠️ این خبر متفاوت است",
                    callback_data="save_pending"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ رد کردن",
                    callback_data="close"
                )
            ]
        ]
    )


# ============================================================
# SAVE NEWS
# ============================================================

async def save_news_item(
    message: Message,
    text: str,
    analysis=None
):

    news = load_news()

    normalized = normalize_text(
        text
    )

    # Prevent exact duplicates
    for item in news:

        if normalize_text(
            item.get("text", "")
        ) == normalized:

            return False

    embedding = await create_embedding(
        text
    )

    next_id = 1

    if news:

        ids = [
            item.get("id", 0)
            for item in news
            if isinstance(
                item.get("id", 0),
                int
            )
        ]

        if ids:
            next_id = max(ids) + 1

    item = {

        "id": next_id,

        "title": (
            analysis.get("title")
            if analysis
            else extract_title(text)
        ),

        "text": text,

        "url": get_message_link(
            message
        ),

        "category": (
            analysis.get("category")
            if analysis
            else None
        ),

        "subject": (
            analysis.get("subject")
            if analysis
            else None
        ),

        "event": (
            analysis.get("event")
            if analysis
            else None
        ),

        "analysis": analysis,

        "embedding": embedding,

        "added_by": (
            message.from_user.id
            if message.from_user
            else None
        ),

        "created_at": (
            datetime.now(
                timezone.utc
            ).isoformat()
        )
    }

    news.append(item)

    return save_news(news)


# ============================================================
# PROCESS INCOMING NEWS
# ============================================================

async def process_news(
    message: Message
):

    text = get_message_text(
        message
    )

    if not text:

        await message.answer(
            "❌ متن خبر پیدا نشد."
        )

        return

    logger.info(
        "Checking news from user %s",
        message.from_user.id
        if message.from_user
        else "unknown"
    )

    # AI analysis
    analysis = await analyze_news_with_ai(
        text
    )

    # Similarity search
    matches = await find_similar_news(
        text
    )

    # Store pending request
    if message.from_user:

        PENDING_NEWS[
            message.from_user.id
        ] = {

            "text": text,

            "analysis": analysis,

            "message": message
        }

    # --------------------------------------------------------
    # NO MATCH
    # --------------------------------------------------------

    if not matches:

        response = [
            "🧠 تحلیل هوشمند گیمفا",
            "",
            "🟢 خبر جدید به نظر می‌رسد",
            "",
            "📊 شباهت به اخبار ثبت‌شده: پایین"
        ]

        if analysis:

            response += [
                "",
                f"📂 دسته: "
                f"{analysis.get('category', 'نامشخص')}",

                f"🎯 موضوع: "
                f"{analysis.get('subject', 'نامشخص')}",

                f"📌 رویداد: "
                f"{analysis.get('event', 'نامشخص')}",
            ]

            summary = analysis.get(
                "summary"
            )

            if summary:

                response += [
                    "",
                    f"📝 خلاصه: {summary}"
                ]

        if not openai_client:

            response += [
                "",
                "⚠️ API هوش مصنوعی تنظیم نشده است."
            ]

        await message.answer(
            "\n".join(response),
            reply_markup=new_news_keyboard()
        )

        return

    # --------------------------------------------------------
    # MATCH FOUND
    # --------------------------------------------------------

    top = matches[0]

    score = top["score"]

    similar_item = top["news"]

    percentage = round(
        score * 100
    )

    if score >= 0.70:

        status = (
            "🔴 احتمال تکراری بودن زیاد است"
        )

    elif score >= 0.50:

        status = (
            "🟠 خبر مشابه پیدا شد؛ بررسی شود"
        )

    else:

        status = (
            "🟡 شباهت نسبتاً پایین است"
        )

    response = [

        "🧠 تحلیل هوشمند گیمفا",

        "",

        status,

        f"📊 میزان شباهت: {percentage}%",

        "",

        "📰 خبر مشابه:",

        similar_item.get(
            "title",
            "بدون عنوان"
        )
    ]

    if analysis:

        response += [

            "",

            f"📂 دسته: "
            f"{analysis.get('category', 'نامشخص')}",

            f"🎯 موضوع: "
            f"{analysis.get('subject', 'نامشخص')}",

            f"📌 رویداد: "
            f"{analysis.get('event', 'نامشخص')}"
        ]

    if similar_item.get("url"):

        response += [

            "",

            f"🔗 {similar_item['url']}"
        ]

    # Find index
    all_news = load_news()

    similar_index = -1

    for index, item in enumerate(
        all_news
    ):

        if item.get("id") == similar_item.get(
            "id"
        ):

            similar_index = index

            break

    await message.answer(

        "\n".join(response),

        reply_markup=duplicate_keyboard(
            similar_index
        )
    )


# ============================================================
# /START
# ============================================================

@router.message(
    CommandStart()
)
async def start_command(
    message: Message
):

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):

        await message.answer(
            "⛔ این ربات فقط برای ادمین‌های مجاز گیمفا فعال است."
        )

        return

    await message.answer(

        "🤖 دستیار هوشمند گیمفا فعال است!\n\n"

        "📰 یک خبر را برای من بفرست یا Forward کن.\n"
        "من آن را با اخبار ثبت‌شده مقایسه می‌کنم.\n\n"

        "🧠 تحلیل AI\n"
        "🔎 تشخیص خبر تکراری\n"
        "🎮 تشخیص موضوع\n"
        "📊 محاسبه شباهت\n\n"

        "دستورات:\n"
        "/stats — آمار اخبار\n"
        "/search — جست‌وجو\n"
        "/help — راهنما"
    )


# ============================================================
# /HELP
# ============================================================

@router.message(
    Command("help")
)
async def help_command(
    message: Message
):

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    await message.answer(

        "📚 راهنمای ربات گیمفا\n\n"

        "1️⃣ یک خبر را برای ربات بفرست.\n\n"

        "2️⃣ ربات خبر را بررسی می‌کند.\n\n"

        "3️⃣ اگر مشابه باشد، خبر قبلی را نمایش می‌دهد.\n\n"

        "4️⃣ اگر جدید باشد، دکمه ثبت خبر نمایش داده می‌شود.\n\n"

        "دستورات:\n\n"

        "/start\n"
        "/stats\n"
        "/search عبارت\n"
        "/help"
    )


# ============================================================
# /STATS
# ============================================================

@router.message(
    Command("stats")
)
async def stats_command(
    message: Message
):

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    news = load_news()

    categories = {}

    for item in news:

        category = (
            item.get("category")
            or "نامشخص"
        )

        categories[category] = (
            categories.get(
                category,
                0
            ) + 1
        )

    response = [

        "📊 آمار گیمفا",

        "",

        f"📰 کل اخبار: {len(news)}"
    ]

    if categories:

        response += [
            "",
            "📂 دسته‌بندی:"
        ]

        for category, count in categories.items():

            response.append(
                f"• {category}: {count}"
            )

    await message.answer(
        "\n".join(response)
    )


# ============================================================
# /SEARCH
# ============================================================

@router.message(
    Command("search")
)
async def search_command(
    message: Message
):

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    query = (
        message.text or ""
    ).partition(" ")[2].strip()

    if not query:

        await message.answer(
            "مثال:\n/search GTA VI"
        )

        return

    normalized_query = normalize_text(
        query
    )

    news = load_news()

    results = []

    for item in news:

        searchable = normalize_text(
            " ".join(
                [
                    item.get("title", ""),
                    item.get("text", ""),
                    item.get("subject", ""),
                    item.get("event", "")
                ]
            )
        )

        if normalized_query in searchable:

            results.append(
                item
            )

    results = results[-10:]

    if not results:

        await message.answer(
            "🔎 هیچ خبری پیدا نشد."
        )

        return

    output = [
        f"🔎 نتایج جست‌وجو برای: {query}",
        ""
    ]

    for item in results:

        output.append(
            f"#{item.get('id')}"
        )

        output.append(
            item.get(
                "title",
                "بدون عنوان"
            )
        )

        if item.get("url"):

            output.append(
                item["url"]
            )

        output.append("")

    await message.answer(
        "\n".join(output)
    )


# ============================================================
# CLOSE BUTTON
# ============================================================

@router.callback_query(
    F.data == "close"
)
async def close_callback(
    callback: CallbackQuery
):

    try:

        await callback.message.edit_reply_markup(
            reply_markup=None
        )

    except Exception:
        pass

    await callback.answer(
        "بسته شد"
    )


# ============================================================
# SAVE PENDING NEWS
# ============================================================

@router.callback_query(
    F.data == "save_pending"
)
async def save_pending_callback(
    callback: CallbackQuery
):

    user = callback.from_user

    pending = PENDING_NEWS.get(
        user.id
    )

    if not pending:

        await callback.answer(
            "⚠️ خبر موقت پیدا نشد. خبر را دوباره ارسال کن.",
            show_alert=True
        )

        return

    message = pending["message"]

    text = pending["text"]

    analysis = pending.get(
        "analysis"
    )

    success = await save_news_item(

        message,

        text,

        analysis
    )

    if not success:

        await callback.answer(
            "⚠️ این خبر قبلاً ثبت شده است.",
            show_alert=True
        )

        return

    PENDING_NEWS.pop(
        user.id,
        None
    )

    try:

        await callback.message.edit_text(
            "✅ خبر با موفقیت ثبت شد.\n\n"
            "📁 در آرشیو محلی ربات ذخیره شد."
        )

    except Exception:

        await callback.message.answer(
            "✅ خبر ثبت شد."
        )

    await callback.answer(
        "ثبت شد"
    )


# ============================================================
# VIEW SIMILAR NEWS
# ============================================================

@router.callback_query(
    F.data.startswith("view:")
)
async def view_callback(
    callback: CallbackQuery
):

    try:

        index = int(
            callback.data.split(
                ":"
            )[1]
        )

        news = load_news()

        if index < 0 or index >= len(news):

            raise IndexError

        item = news[index]

        text = item.get(
            "text",
            ""
        )

        output = [

            "📰 خبر مشابه",

            "",

            item.get(
                "title",
                "بدون عنوان"
            ),

            "",

            text[:3500]
        ]

        if item.get("url"):

            output += [
                "",
                f"🔗 {item['url']}"
            ]

        await callback.message.answer(
            "\n".join(output)
        )

        await callback.answer()

    except Exception:

        await callback.answer(
            "خبر پیدا نشد.",
            show_alert=True
        )


# ============================================================
# TEXT / FORWARD NEWS
# ============================================================

@router.message(
    F.text | F.caption
)
async def incoming_news(
    message: Message
):

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    # Ignore commands
    if message.text and message.text.startswith("/"):
        return

    await process_news(
        message
    )


# ============================================================
# CHANNEL POSTS
# ============================================================

@router.channel_post()
async def channel_post(
    message: Message
):

    text = get_message_text(
        message
    )

    if not text:
        return

    news = load_news()

    normalized = normalize_text(
        text
    )

    # Exact duplicate protection
    for item in news:

        if normalize_text(
            item.get("text", "")
        ) == normalized:

            logger.info(
                "Duplicate channel post ignored."
            )

            return

    # Channel posts are automatically archived.
    # AI analysis is intentionally skipped here
    # to reduce API usage.
    embedding = await create_embedding(
        text
    )

    next_id = 1

    if news:

        ids = [
            item.get("id", 0)
            for item in news
            if isinstance(
                item.get("id", 0),
                int
            )
        ]

        if ids:

            next_id = max(ids) + 1

    item = {

        "id": next_id,

        "title": extract_title(
            text
        ),

        "text": text,

        "url": get_message_link(
            message
        ),

        "category": None,

        "subject": None,

        "event": None,

        "analysis": None,

        "embedding": embedding,

        "added_by": None,

        "created_at": (
            datetime.now(
                timezone.utc
            ).isoformat()
        )
    }

    news.append(
        item
    )

    save_news(
        news
    )

    logger.info(
        "Channel post %s saved.",
        message.message_id
    )


# ============================================================
# ERROR HANDLER
# ============================================================

@router.error()
async def error_handler(
    event
):

    logger.exception(
        "Unhandled Telegram error: %s",
        event.exception
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "========================================"
    )

    logger.info(
        "Starting Gamefa AI Bot"
    )

    logger.info(
        "AI enabled: %s",
        bool(openai_client)
    )

    logger.info(
        "AI model: %s",
        OPENAI_MODEL
    )

    logger.info(
        "Embedding model: %s",
        EMBEDDING_MODEL
    )

    logger.info(
        "Admins configured: %d",
        len(ADMIN_IDS)
    )

    logger.info(
        "News file: %s",
        NEWS_FILE
    )

    logger.info(
        "========================================"
    )

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types()
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped."
        )