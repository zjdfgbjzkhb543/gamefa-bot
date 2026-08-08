import asyncio
import json
import logging
import math
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
# GAMEFA AI BOT V2
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-4.1-mini"
).strip()

EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "text-embedding-3-small"
).strip()

DATA_DIR = Path(
    os.getenv("DATA_DIR", "/data")
)

MAX_NEWS = 1000

# ------------------------------------------------------------
# ADMIN IDS
# ------------------------------------------------------------

def get_admin_ids():
    raw = os.getenv("ADMIN_IDS", "")
    result = set()

    for item in raw.split(","):
        item = item.strip()

        if item.isdigit():
            result.add(int(item))

    return result


ADMIN_IDS = get_admin_ids()

PRIMARY_ADMIN_ID = int(
    os.getenv("PRIMARY_ADMIN_ID", "0") or 0
)

# ------------------------------------------------------------
# FILES
# ------------------------------------------------------------

try:
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )
except Exception:
    DATA_DIR = Path(".")
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


NEWS_FILE = DATA_DIR / "news.json"
SETTINGS_FILE = DATA_DIR / "settings.json"


# ------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("gamefa")


# ------------------------------------------------------------
# BOT
# ------------------------------------------------------------

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is missing."
    )


bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher()

router = Router()

dp.include_router(router)


# ------------------------------------------------------------
# OPENAI
# ------------------------------------------------------------

ai: Optional[AsyncOpenAI] = None

if OPENAI_API_KEY:

    ai = AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )


# ------------------------------------------------------------
# TEMP DATA
# ------------------------------------------------------------

PENDING = {}

AWAITING = {}


# ============================================================
# JSON STORAGE
# ============================================================

def load_json(path, default):

    try:

        if not path.exists():
            return default

        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        return data

    except Exception:

        logger.exception(
            "Could not read %s",
            path
        )

        return default


def save_json(path, data):

    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    temporary.replace(path)


# ============================================================
# NEWS DATABASE
# ============================================================

def load_news():

    data = load_json(
        NEWS_FILE,
        []
    )

    if not isinstance(data, list):
        return []

    return data


def save_news(news):

    news = news[-MAX_NEWS:]

    save_json(
        NEWS_FILE,
        news
    )


# ============================================================
# SETTINGS
# ============================================================

def load_settings():

    default = {

        "admins": sorted(
            ADMIN_IDS
        ),

        "primary_admin": PRIMARY_ADMIN_ID,

        "similarity_threshold": 0.72,

        "channel_id": ""

    }

    data = load_json(
        SETTINGS_FILE,
        default
    )

    if not isinstance(data, dict):
        data = default

    data.setdefault(
        "admins",
        sorted(ADMIN_IDS)
    )

    data.setdefault(
        "primary_admin",
        PRIMARY_ADMIN_ID
    )

    data.setdefault(
        "similarity_threshold",
        0.72
    )

    data.setdefault(
        "channel_id",
        ""
    )

    return data


SETTINGS = load_settings()


def save_settings():

    save_json(
        SETTINGS_FILE,
        SETTINGS
    )


# ============================================================
# ADMIN SYSTEM
# ============================================================

def admins():

    return {
        int(x)
        for x in SETTINGS.get(
            "admins",
            []
        )
        if str(x).isdigit()
    }


def is_admin(user_id):

    return user_id in admins()


def is_primary_admin(user_id):

    primary = int(
        SETTINGS.get(
            "primary_admin",
            0
        ) or 0
    )

    return user_id == primary


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize(text):

    if not text:
        return ""

    text = text.lower()

    replacements = {

        "ي": "ی",

        "ى": "ی",

        "ك": "ک",

        "ۀ": "ه",

        "\u200c": " ",

        "\u200f": " ",

        "\u200e": " "

    }

    for a, b in replacements.items():

        text = text.replace(
            a,
            b
        )

    text = re.sub(
        r"https?://\S+",
        " ",
        text
    )

    text = re.sub(
        r"[@#][\w_]+",
        " ",
        text
    )

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
# TEXT SIMILARITY
# ============================================================

def lexical_similarity(
    first,
    second
):

    first = normalize(first)

    second = normalize(second)

    if not first or not second:
        return 0.0

    first_words = set(
        first.split()
    )

    second_words = set(
        second.split()
    )

    union = (
        first_words |
        second_words
    )

    intersection = (
        first_words &
        second_words
    )

    jaccard = (
        len(intersection) /
        max(
            1,
            len(union)
        )
    )

    sequence = SequenceMatcher(
        None,
        first,
        second
    ).ratio()

    return (
        0.55 * jaccard +
        0.45 * sequence
    )


# ============================================================
# COSINE
# ============================================================

def cosine_similarity(
    first,
    second
):

    if not first or not second:
        return 0.0

    if len(first) != len(second):
        return 0.0

    first_length = math.sqrt(
        sum(
            value * value
            for value in first
        )
    )

    second_length = math.sqrt(
        sum(
            value * value
            for value in second
        )
    )

    if not first_length or not second_length:
        return 0.0

    value = sum(
        a * b
        for a, b in zip(
            first,
            second
        )
    )

    return value / (
        first_length *
        second_length
    )


# ============================================================
# MESSAGE HELPERS
# ============================================================

def get_message_text(message):

    return (
        message.text or
        message.caption or
        ""
    ).strip()


def get_message_link(message):

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

    return ""


# ============================================================
# OPENAI EMBEDDING
# ============================================================

async def create_embedding(text):

    if not ai:
        return None

    try:

        result = await ai.embeddings.create(

            model=EMBEDDING_MODEL,

            input=text[:8000]

        )

        return result.data[0].embedding

    except Exception:

        logger.exception(
            "Embedding error"
        )

        return None


# ============================================================
# OPENAI JSON
# ============================================================

async def ask_ai_json(
    system_prompt,
    user_prompt
):

    if not ai:
        return None

    try:

        result = await ai.chat.completions.create(

            model=OPENAI_MODEL,

            temperature=0.1,

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
                    "content": user_prompt[:16000]
                }

            ]

        )

        content = (
            result
            .choices[0]
            .message
            .content
        )

        return json.loads(
            content
        )

    except Exception:

        logger.exception(
            "AI request failed"
        )

        return None


# ============================================================
# NEWS ANALYSIS
# ============================================================

ANALYSIS_PROMPT = """

تو دستیار هوش مصنوعی تحریریه گیمفا هستی.

خبر را تحلیل کن.

فقط JSON معتبر برگردان.

ساختار:

{
"title":"",
"category":"",
"subject":"",
"event":"",
"entities":[],
"keywords":[],
"source":"",
"source_type":"",
"importance":"",
"summary":"",
"reason":""
}

category باید یکی از این‌ها باشد:

بازی
فیلم و سریال
فناوری
هوش مصنوعی
سایر

source_type:

رسمی
گزارش رسانه‌ای
شایعه
تأییدنشده
نامشخص

importance:

کم
متوسط
زیاد
فوری

اطلاعاتی که در متن وجود ندارد را جعل نکن.

"""


async def analyze_news(text):

    return await ask_ai_json(
        ANALYSIS_PROMPT,
        text
    )


# ============================================================
# GAMEFA REWRITE
# ============================================================

async def rewrite_news(text):

    prompt = """

تو ویراستار خبری گیمفا هستی.

خبر زیر را به فارسی روان،
حرفه‌ای و مناسب انتشار در کانال
گیمفا بازنویسی کن.

هیچ اطلاعات جدیدی اضافه نکن.

خروجی فقط JSON:

{
"title":"",
"body":"",
"hashtags":[]
}

عنوان کوتاه و خبری باشد.

متن خبر حرفه‌ای و قابل انتشار باشد.

"""

    return await ask_ai_json(
        prompt,
        text
    )


# ============================================================
# TITLE
# ============================================================

async def generate_title(text):

    prompt = """

فقط JSON برگردان:

{
"title":""
}

برای این خبر یک تیتر خبری فارسی
کوتاه، جذاب و دقیق بساز.

"""

    return await ask_ai_json(
        prompt,
        text
    )


# ============================================================
# SUMMARY
# ============================================================

async def generate_summary(text):

    prompt = """

فقط JSON برگردان:

{
"summary":""
}

خبر را در حداکثر دو جمله
به فارسی خلاصه کن.

"""

    return await ask_ai_json(
        prompt,
        text
    )


# ============================================================
# HASHTAGS
# ============================================================

async def generate_hashtags(text):

    prompt = """

فقط JSON برگردان:

{
"hashtags":[]
}

۵ هشتگ مناسب برای این خبر
پیشنهاد بده.

"""

    return await ask_ai_json(
        prompt,
        text
    )


# ============================================================
# COMPARE
# ============================================================

async def compare_news(
    new_news,
    old_news
):

    prompt = """

دو خبر را مقایسه کن.

تمرکز روی:

- رویداد اصلی
- بازی/فیلم
- شخصیت‌ها
- شرکت‌ها
- ادعای اصلی
- تاریخ
- جزئیات مهم

فقط JSON:

{
"same_event":true,
"score":0,
"reason":"",
"differences":[]
}

score بین 0 تا 100.

"""

    user_prompt = (

        "خبر جدید:\n"
        + new_news
        + "\n\n"
        + "خبر قبلی:\n"
        + old_news

    )

    return await ask_ai_json(
        prompt,
        user_prompt
    )


# ============================================================
# WHY DUPLICATE
# ============================================================

async def explain_duplicate(
    new_news,
    old_news
):

    prompt = """

توضیح بده چرا این دو خبر مشابه هستند.

فقط JSON:

{
"reason":"",
"shared_entities":[],
"shared_event":"",
"important_difference":""
}

"""

    user_prompt = (

        "خبر جدید:\n"
        + new_news
        + "\n\n"
        + "خبر قبلی:\n"
        + old_news

    )

    return await ask_ai_json(
        prompt,
        user_prompt
    )


# ============================================================
# FIND SIMILAR NEWS
# ============================================================

async def find_similar_news(text):

    news = load_news()

    if not news:
        return []

    candidates = []

    # مرحله اول:
    # بررسی سریع متنی

    for item in news:

        similarity = lexical_similarity(

            text,

            item.get(
                "text",
                ""
            )

        )

        if similarity >= 0.18:

            candidates.append(
                (
                    similarity,
                    item
                )
            )

    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )

    candidates = candidates[:40]

    # مرحله دوم:
    # Embedding

    embedding = await create_embedding(
        text
    )

    results = []

    for lexical, item in candidates:

        semantic = 0.0

        if (
            embedding and
            item.get("embedding")
        ):

            semantic = cosine_similarity(

                embedding,

                item["embedding"]

            )

        if embedding and item.get(
            "embedding"
        ):

            final_score = (

                0.75 * semantic +
                0.25 * lexical

            )

        else:

            final_score = lexical

        results.append({

            "score": final_score,

            "lexical": lexical,

            "semantic": semantic,

            "news": item

        })

    results.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return results[:5]


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="📰 بررسی خبر",
                    callback_data="menu_check"
                ),

                InlineKeyboardButton(
                    text="🔎 جست‌وجوی اخبار",
                    callback_data="menu_search"
                )

            ],

            [

                InlineKeyboardButton(
                    text="📚 آرشیو",
                    callback_data="menu_archive"
                ),

                InlineKeyboardButton(
                    text="📊 آمار",
                    callback_data="menu_stats"
                )

            ],

            [

                InlineKeyboardButton(
                    text="🧠 ابزارهای هوش مصنوعی",
                    callback_data="menu_ai"
                )

            ],

            [

                InlineKeyboardButton(
                    text="👥 مدیریت ادمین‌ها",
                    callback_data="menu_admins"
                ),

                InlineKeyboardButton(
                    text="⚙️ تنظیمات",
                    callback_data="menu_settings"
                )

            ]

        ]

    )


def ai_keyboard():

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="✍️ بازنویسی خبر",
                    callback_data="ai_rewrite"
                )

            ],

            [

                InlineKeyboardButton(
                    text="📰 ساخت تیتر",
                    callback_data="ai_title"
                ),

                InlineKeyboardButton(
                    text="📝 خلاصه",
                    callback_data="ai_summary"
                )

            ],

            [

                InlineKeyboardButton(
                    text="🏷️ هشتگ",
                    callback_data="ai_hashtags"
                ),

                InlineKeyboardButton(
                    text="🚨 تشخیص شایعه",
                    callback_data="ai_rumor"
                )

            ],

            [

                InlineKeyboardButton(
                    text="🔙 بازگشت",
                    callback_data="main_menu"
                )

            ]

        ]

    )


def admin_keyboard():

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="➕ افزودن ادمین",
                    callback_data="admin_add"
                ),

                InlineKeyboardButton(
                    text="➖ حذف ادمین",
                    callback_data="admin_remove"
                )

            ],

            [

                InlineKeyboardButton(
                    text="📋 لیست ادمین‌ها",
                    callback_data="admin_list"
                )

            ],

            [

                InlineKeyboardButton(
                    text="🔙 بازگشت",
                    callback_data="main_menu"
                )

            ]

        ]

    )


def new_news_keyboard():

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="✍️ بازنویسی گیمفا",
                    callback_data="pending_rewrite"
                )

            ],

            [

                InlineKeyboardButton(
                    text="📰 ساخت تیتر",
                    callback_data="pending_title"
                ),

                InlineKeyboardButton(
                    text="📝 خلاصه",
                    callback_data="pending_summary"
                )

            ],

            [

                InlineKeyboardButton(
                    text="🏷️ هشتگ",
                    callback_data="pending_hashtags"
                )

            ],

            [

                InlineKeyboardButton(
                    text="📢 آماده انتشار",
                    callback_data="pending_publish"
                )

            ],

            [

                InlineKeyboardButton(
                    text="💾 ثبت در آرشیو",
                    callback_data="pending_save"
                )

            ],

            [

                InlineKeyboardButton(
                    text="❌ رد",
                    callback_data="close"
                )

            ]

        ]

    )


def duplicate_keyboard(
    news_index
):

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="👀 مشاهده خبر قبلی",
                    callback_data=f"old_{news_index}"
                )

            ],

            [

                InlineKeyboardButton(
                    text="⚖️ مقایسه دو خبر",
                    callback_data=f"compare_{news_index}"
                )

            ],

            [

                InlineKeyboardButton(
                    text="🧠 چرا تکراریه؟",
                    callback_data=f"why_{news_index}"
                )

            ],

            [

                InlineKeyboardButton(
                    text="✍️ بازنویسی خبر",
                    callback_data="pending_rewrite"
                )

            ],

            [

                InlineKeyboardButton(
                    text="✅ این خبر جدید است",
                    callback_data="pending_save"
                )

            ],

            [

                InlineKeyboardButton(
                    text="❌ رد",
                    callback_data="close"
                )

            ]

        ]

    )


# ============================================================
# START
# ============================================================

@router.message(
    CommandStart()
)
async def start_command(message):

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):

        await message.answer(
            "⛔ شما به این ربات دسترسی ندارید."
        )

        return

    await message.answer(

        "🤖 پنل مدیریت گیمفا\n\n"
        "سیستم ضدخبرتکراری و دستیار "
        "هوش مصنوعی آماده است.",

        reply_markup=main_keyboard()

    )


# ============================================================
# HELP
# ============================================================

@router.message(
    Command("help")
)
async def help_command(message):

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    await message.answer(

        "راهنمای ربات گیمفا\n\n"

        "📰 خبر را برای ربات ارسال کن.\n"
        "ربات آن را با آرشیو ۱۰۰۰ خبر "
        "آخر مقایسه می‌کند.\n\n"

        "🧠 سپس هوش مصنوعی موضوع، "
        "منبع و وضعیت خبر را بررسی می‌کند.\n\n"

        "برای شروع /start را بزن."

    )


# ============================================================
# STATS
# ============================================================

async def send_stats(message):

    news = load_news()

    categories = {}

    for item in news:

        category = (
            item.get(
                "category"
            )
            or
            "نامشخص"
        )

        categories[category] = (
            categories.get(
                category,
                0
            )
            + 1
        )

    text = (

        "📊 آمار گیمفا\n\n"

        f"📚 آرشیو: "
        f"{len(news)}/{MAX_NEWS}\n\n"

        "📂 دسته‌بندی:\n"

    )

    for category, count in categories.items():

        text += (
            f"• {category}: "
            f"{count}\n"
        )

    await message.answer(
        text
    )


@router.message(
    Command("stats")
)
async def stats_command(message):

    if (
        message.from_user and
        is_admin(
            message.from_user.id
        )
    ):

        await send_stats(
            message
        )


# ============================================================
# MENU CALLBACKS
# ============================================================

@router.callback_query(
    F.data == "main_menu"
)
async def main_menu(callback):

    await callback.message.edit_text(

        "🤖 پنل مدیریت گیمفا",

        reply_markup=main_keyboard()

    )

    await callback.answer()


@router.callback_query(
    F.data == "menu_check"
)
async def menu_check(callback):

    await callback.message.answer(

        "📰 متن خبر را ارسال کن.\n\n"
        "ربات قبل از انتشار آن را "
        "بررسی می‌کند."

    )

    await callback.answer()


@router.callback_query(
    F.data == "menu_stats"
)
async def menu_stats(callback):

    await send_stats(
        callback.message
    )

    await callback.answer()


@router.callback_query(
    F.data == "menu_archive"
)
async def menu_archive(callback):

    news = load_news()

    if not news:

        await callback.message.answer(
            "📚 آرشیو خالی است."
        )

        await callback.answer()
        return

    output = (

        "📚 آخرین اخبار آرشیو:\n\n"

    )

    for item in reversed(
        news[-10:]
    ):

        output += (

            f"#{item.get('id')} "
            f"— "
            f"{item.get('title', 'بدون عنوان')}\n\n"

        )

    await callback.message.answer(
        output
    )

    await callback.answer()


@router.callback_query(
    F.data == "menu_ai"
)
async def menu_ai(callback):

    await callback.message.edit_text(

        "🧠 ابزارهای هوش مصنوعی گیمفا:",

        reply_markup=ai_keyboard()

    )

    await callback.answer()


# ============================================================
# ADMIN MENU
# ============================================================

@router.callback_query(
    F.data == "menu_admins"
)
async def menu_admins(callback):

    if not is_primary_admin(
        callback.from_user.id
    ):

        await callback.answer(
            "⛔ فقط ادمین اصلی.",
            show_alert=True
        )

        return

    await callback.message.edit_text(

        "👥 مدیریت ادمین‌ها",

        reply_markup=admin_keyboard()

    )

    await callback.answer()


@router.callback_query(
    F.data == "admin_list"
)
async def admin_list(callback):

    if not is_primary_admin(
        callback.from_user.id
    ):
        return

    current = sorted(
        admins()
    )

    text = (
        "👥 لیست ادمین‌ها:\n\n"
    )

    for user_id in current:

        text += (
            f"• {user_id}\n"
        )

    await callback.message.answer(
        text
    )

    await callback.answer()


@router.callback_query(
    F.data == "admin_add"
)
async def admin_add(callback):

    if not is_primary_admin(
        callback.from_user.id
    ):
        return

    AWAITING[
        callback.from_user.id
    ] = "add_admin"

    await callback.message.answer(

        "➕ آیدی عددی ادمین جدید را ارسال کن."

    )

    await callback.answer()


@router.callback_query(
    F.data == "admin_remove"
)
async def admin_remove(callback):

    if not is_primary_admin(
        callback.from_user.id
    ):
        return

    AWAITING[
        callback.from_user.id
    ] = "remove_admin"

    await callback.message.answer(

        "➖ آیدی عددی ادمینی که باید حذف شود را ارسال کن."

    )

    await callback.answer()


# ============================================================
# SETTINGS
# ============================================================

@router.callback_query(
    F.data == "menu_settings"
)
async def menu_settings(callback):

    if not is_primary_admin(
        callback.from_user.id
    ):

        await callback.answer(
            "⛔ فقط ادمین اصلی.",
            show_alert=True
        )

        return

    news_count = len(
        load_news()
    )

    ai_status = (
        "فعال"
        if ai
        else
        "غیرفعال"
    )

    text = (

        "⚙️ تنظیمات ربات\n\n"

        f"📚 آرشیو: "
        f"{news_count}/{MAX_NEWS}\n\n"

        f"📊 آستانه شباهت: "
        f"{SETTINGS.get('similarity_threshold', 0.72)}\n\n"

        f"🧠 هوش مصنوعی: "
        f"{ai_status}\n\n"

        f"💾 مسیر ذخیره‌سازی:\n"
        f"{DATA_DIR}"

    )

    await callback.message.answer(
        text
    )

    await callback.answer()


# ============================================================
# AI MENU
# ============================================================

@router.callback_query(
    F.data.startswith("ai_")
)
async def ai_tools(callback):

    if not ai:

        await callback.answer(
            "⚠️ OPENAI_API_KEY تنظیم نشده.",
            show_alert=True
        )

        return

    action = callback.data.replace(
        "ai_",
        ""
    )

    AWAITING[
        callback.from_user.id
    ] = (
        "ai_tool",
        action
    )

    await callback.message.answer(

        "🧠 متن خبر را ارسال کن."

    )

    await callback.answer()


# ============================================================
# PENDING ACTIONS
# ============================================================

@router.callback_query(
    F.data == "pending_save"
)
async def pending_save(callback):

    user_id = callback.from_user.id

    pending = PENDING.get(
        user_id
    )

    if not pending:

        await callback.answer(
            "خبر موقت پیدا نشد.",
            show_alert=True
        )

        return

    news = load_news()

    text = pending[
        "text"
    ]

    # جلوگیری از ثبت دقیقاً تکراری

    for item in news:

        if normalize(
            item.get(
                "text",
                ""
            )
        ) == normalize(text):

            await callback.answer(
                "این خبر قبلاً ثبت شده.",
                show_alert=True
            )

            return

    ids = [

        item.get(
            "id",
            0
        )

        for item in news

        if isinstance(
            item.get(
                "id",
                0
            ),
            int
        )

    ]

    next_id = (
        max(ids or [0])
        + 1
    )

    analysis = (
        pending.get(
            "analysis"
        )
        or {}
    )

    item = {

        "id": next_id,

        "title": (
            analysis.get(
                "title"
            )
            or
            text.splitlines()[0][:300]
        ),

        "text": text,

        "url": get_message_link(
            pending["message"]
        ),

        "category": analysis.get(
            "category"
        ),

        "subject": analysis.get(
            "subject"
        ),

        "event": analysis.get(
            "event"
        ),

        "analysis": analysis,

        "embedding": pending.get(
            "embedding"
        ),

        "added_by": user_id,

        "created_at":
            datetime.now(
                timezone.utc
            ).isoformat()

    }

    news.append(
        item
    )

    save_news(
        news
    )

    PENDING.pop(
        user_id,
        None
    )

    await callback.message.answer(

        "💾 خبر با موفقیت در آرشیو ثبت شد.\n\n"
        f"📚 تعداد اخبار آرشیو: "
        f"{len(news)}/{MAX_NEWS}"

    )

    await callback.answer()


# ============================================================
# AI ACTIONS FOR PENDING NEWS
# ============================================================

async def run_pending_ai(
    message,
    action,
    text
):

    if not ai:

        await message.answer(
            "⚠️ هوش مصنوعی فعال نیست."
        )

        return

    if action == "rewrite":

        result = await rewrite_news(
            text
        )

        if result:

            hashtags = " ".join(
                result.get(
                    "hashtags",
                    []
                )
            )

            await message.answer(

                f"📰 {result.get('title', '')}\n\n"
                f"{result.get('body', '')}\n\n"
                f"{hashtags}"

            )

        return

    if action == "title":

        result = await generate_title(
            text
        )

        await message.answer(

            "📰 تیتر پیشنهادی:\n\n"
            +
            (
                result.get(
                    "title",
                    ""
                )
                if result
                else
                "خطا"
            )

        )

        return

    if action == "summary":

        result = await generate_summary(
            text
        )

        await message.answer(

            "📝 خلاصه:\n\n"
            +
            (
                result.get(
                    "summary",
                    ""
                )
                if result
                else
                "خطا"
            )

        )

        return

    if action == "hashtags":

        result = await generate_hashtags(
            text
        )

        hashtags = (
            result.get(
                "hashtags",
                []
            )
            if result
            else []
        )

        await message.answer(

            "🏷️ هشتگ‌ها:\n\n"
            +
            " ".join(
                hashtags
            )

        )

        return

    if action == "rumor":

        result = await analyze_news(
            text
        )

        if result:

            await message.answer(

                "🚨 بررسی وضعیت خبر\n\n"

                f"وضعیت: "
                f"{result.get('source_type', 'نامشخص')}\n\n"

                f"منبع: "
                f"{result.get('source') or 'نامشخص'}\n\n"

                f"🧠 تحلیل:\n"
                f"{result.get('reason', '')}"

            )


@router.callback_query(
    F.data == "pending_rewrite"
)
async def pending_rewrite(callback):

    pending = PENDING.get(
        callback.from_user.id
    )

    if not pending:

        await callback.answer(
            "خبر پیدا نشد.",
            show_alert=True
        )

        return

    await run_pending_ai(

        callback.message,

        "rewrite",

        pending["text"]

    )

    await callback.answer()


@router.callback_query(
    F.data == "pending_title"
)
async def pending_title(callback):

    pending = PENDING.get(
        callback.from_user.id
    )

    if not pending:
        return

    await run_pending_ai(
        callback.message,
        "title",
        pending["text"]
    )

    await callback.answer()


@router.callback_query(
    F.data == "pending_summary"
)
async def pending_summary(callback):

    pending = PENDING.get(
        callback.from_user.id
    )

    if not pending:
        return

    await run_pending_ai(
        callback.message,
        "summary",
        pending["text"]
    )

    await callback.answer()


@router.callback_query(
    F.data == "pending_hashtags"
)
async def pending_hashtags(callback):

    pending = PENDING.get(
        callback.from_user.id
    )

    if not pending:
        return

    await run_pending_ai(
        callback.message,
        "hashtags",
        pending["text"]
    )

    await callback.answer()


@router.callback_query(
    F.data == "pending_publish"
)
async def pending_publish(callback):

    pending = PENDING.get(
        callback.from_user.id
    )

    if not pending:
        return

    result = await rewrite_news(
        pending["text"]
    )

    if not result:

        await callback.message.answer(
            "⚠️ ساخت نسخه آماده انتشار انجام نشد."
        )

        await callback.answer()
        return

    hashtags = " ".join(
        result.get(
            "hashtags",
            []
        )
    )

    await callback.message.answer(

        "📢 نسخه آماده انتشار:\n\n"

        f"{result.get('title', '')}\n\n"

        f"{result.get('body', '')}\n\n"

        f"{hashtags}"

    )

    await callback.answer()


# ============================================================
# OLD NEWS
# ============================================================

@router.callback_query(
    F.data.startswith("old_")
)
async def show_old_news(callback):

    try:

        index = int(
            callback.data.split(
                "_"
            )[1]
        )

        news = load_news()

        item = news[index]

        text = (

            "📰 خبر قبلی:\n\n"

            + item.get(
                "text",
                ""
            )

        )

        if item.get(
            "url"
        ):

            text += (

                "\n\n🔗 "
                + item["url"]

            )

        await callback.message.answer(
            text
        )

    except Exception:

        await callback.answer(
            "خبر پیدا نشد.",
            show_alert=True
        )

        return

    await callback.answer()


# ============================================================
# COMPARE
# ============================================================

@router.callback_query(
    F.data.startswith("compare_")
)
async def compare_callback(
    callback
):

    try:

        index = int(
            callback.data.split(
                "_"
            )[1]
        )

        pending = PENDING.get(
            callback.from_user.id
        )

        news = load_news()

        old_news = news[index]

        result = await compare_news(

            pending["text"],

            old_news.get(
                "text",
                ""
            )

        )

        if not result:

            await callback.message.answer(
                "⚠️ تحلیل مقایسه انجام نشد."
            )

            await callback.answer()
            return

        differences = "\n".join(

            "• " + str(item)

            for item in result.get(
                "differences",
                []
            )

        )

        await callback.message.answer(

            "⚖️ مقایسه دو خبر\n\n"

            f"📊 شباهت AI: "
            f"{result.get('score', 0)}%\n\n"

            f"🧠 نتیجه:\n"
            f"{result.get('reason', '')}\n\n"

            f"🔹 تفاوت‌ها:\n"
            f"{differences}"

        )

    except Exception:

        logger.exception(
            "Comparison error"
        )

        await callback.answer(
            "خطا در مقایسه.",
            show_alert=True
        )

        return

    await callback.answer()


# ============================================================
# WHY DUPLICATE
# ============================================================

@router.callback_query(
    F.data.startswith("why_")
)
async def why_callback(
    callback
):

    try:

        index = int(
            callback.data.split(
                "_"
            )[1]
        )

        pending = PENDING.get(
            callback.from_user.id
        )

        news = load_news()

        old_news = news[index]

        result = await explain_duplicate(

            pending["text"],

            old_news.get(
                "text",
                ""
            )

        )

        if not result:

            await callback.message.answer(
                "⚠️ تحلیل انجام نشد."
            )

            await callback.answer()
            return

        entities = ", ".join(

            result.get(
                "shared_entities",
                []
            )

        )

        await callback.message.answer(

            "🧠 چرا تکراریه؟\n\n"

            f"{result.get('reason', '')}\n\n"

            f"📌 رویداد مشترک:\n"
            f"{result.get('shared_event', '')}\n\n"

            f"👤 موجودیت‌های مشترک:\n"
            f"{entities}\n\n"

            f"🔹 تفاوت مهم:\n"
            f"{result.get('important_difference', '')}"

        )

    except Exception:

        logger.exception(
            "Duplicate explanation error"
        )

        await callback.answer(
            "خطا.",
            show_alert=True
        )

        return

    await callback.answer()


# ============================================================
# CLOSE
# ============================================================

@router.callback_query(
    F.data == "close"
)
async def close_callback(
    callback
):

    try:

        await callback.message.edit_reply_markup(
            reply_markup=None
        )

    except Exception:
        pass

    await callback.answer(
        "بسته شد."
    )


# ============================================================
# TEXT PROCESSING
# ============================================================

async def process_message(
    message
):

    if not message.from_user:
        return

    user_id = (
        message.from_user.id
    )

    if not is_admin(
        user_id
    ):
        return

    text = get_message_text(
        message
    )

    if not text:
        return

    # --------------------------------------------------------
    # ADMIN ADD
    # --------------------------------------------------------

    waiting = AWAITING.get(
        user_id
    )

    if waiting == "add_admin":

        if not is_primary_admin(
            user_id
        ):
            return

        if not text.isdigit():

            await message.answer(
                "❌ آیدی باید عددی باشد."
            )

            return

        new_admin = int(
            text
        )

        current = admins()

        current.add(
            new_admin
        )

        SETTINGS["admins"] = sorted(
            current
        )

        save_settings()

        AWAITING.pop(
            user_id,
            None
        )

        await message.answer(
            "✅ ادمین با موفقیت اضافه شد."
        )

        return

    # --------------------------------------------------------
    # ADMIN REMOVE
    # --------------------------------------------------------

    if waiting == "remove_admin":

        if not is_primary_admin(
            user_id
        ):
            return

        if not text.isdigit():

            await message.answer(
                "❌ آیدی باید عددی باشد."
            )

            return

        remove_id = int(
            text
        )

        if remove_id == int(
            SETTINGS.get(
                "primary_admin",
                0
            )
        ):

            await message.answer(
                "❌ ادمین اصلی قابل حذف نیست."
            )

            return

        current = admins()

        current.discard(
            remove_id
        )

        SETTINGS["admins"] = sorted(
            current
        )

        save_settings()

        AWAITING.pop(
            user_id,
            None
        )

        await message.answer(
            "✅ ادمین حذف شد."
        )

        return

    # --------------------------------------------------------
    # AI TOOL
    # --------------------------------------------------------

    if (
        isinstance(
            waiting,
            tuple
        )
        and
        waiting[0] == "ai_tool"
    ):

        action = waiting[1]

        AWAITING.pop(
            user_id,
            None
        )

        await run_pending_ai(
            message,
            action,
            text
        )

        return

    # --------------------------------------------------------
    # IGNORE COMMANDS
    # --------------------------------------------------------

    if text.startswith("/"):
        return

    # --------------------------------------------------------
    # EXACT DUPLICATE
    # --------------------------------------------------------

    news = load_news()

    for item in news:

        if normalize(
            item.get(
                "text",
                ""
            )
        ) == normalize(text):

            await message.answer(

                "🔴 این خبر دقیقاً "
                "در آرشیو وجود دارد.\n\n"

                f"📰 خبر قبلی:\n\n"
                f"{item.get('text', '')}"

            )

            return

    # --------------------------------------------------------
    # AI ANALYSIS
    # --------------------------------------------------------

    await message.answer(
        "🧠 در حال بررسی خبر..."
    )

    analysis = await analyze_news(
        text
    )

    embedding = await create_embedding(
        text
    )

    # --------------------------------------------------------
    # FIND SIMILAR
    # --------------------------------------------------------

    matches = await find_similar_news(
        text
    )

    PENDING[user_id] = {

        "message": message,

        "text": text,

        "analysis": analysis,

        "embedding": embedding,

        "matches": matches

    }

    threshold = float(
        SETTINGS.get(
            "similarity_threshold",
            0.72
        )
    )

    top = (
        matches[0]
        if matches
        else None
    )

    # --------------------------------------------------------
    # DUPLICATE
    # --------------------------------------------------------

    if (
        top and
        top["score"] >= threshold
    ):

        old_news = top["news"]

        all_news = load_news()

        index = next(

            (
                i

                for i, item
                in enumerate(all_news)

                if item.get(
                    "id"
                )
                ==
                old_news.get(
                    "id"
                )

            ),

            -1

        )

        output = (

            "🔴 خبر مشابه پیدا شد.\n\n"

            f"📊 میزان شباهت: "
            f"{round(top['score'] * 100)}%\n\n"

            "📰 متن خبر قبلی:\n\n"

            f"{old_news.get('text', '')}"

        )

        if old_news.get(
            "url"
        ):

            output += (

                "\n\n🔗 "
                + old_news["url"]

            )

        if analysis:

            output += (

                "\n\n📂 دسته: "
                + str(
                    analysis.get(
                        "category",
                        "نامشخص"
                    )
                )

                +

                "\n🚨 وضعیت: "
                + str(
                    analysis.get(
                        "source_type",
                        "نامشخص"
                    )
                )

            )

        await message.answer(

            output,

            reply_markup=duplicate_keyboard(
                index
            )

        )

        return

    # --------------------------------------------------------
    # NEW NEWS
    # --------------------------------------------------------

    output = (
        "🟢 خبر جدید به نظر می‌رسد."
    )

    if top:

        output += (

            "\n\n📊 بیشترین شباهت: "
            f"{round(top['score'] * 100)}%"

        )

    if analysis:

        output += (

            "\n\n📰 تیتر پیشنهادی:\n"
            f"{analysis.get('title', '')}"

            "\n\n📂 دسته: "
            f"{analysis.get('category', 'نامشخص')}"

            "\n🚨 وضعیت: "
            f"{analysis.get('source_type', 'نامشخص')}"

            "\n📌 اهمیت: "
            f"{analysis.get('importance', 'نامشخص')}"

        )

    await message.answer(

        output,

        reply_markup=new_news_keyboard()

    )


@router.message(
    F.text | F.caption
)
async def text_handler(
    message
):

    await process_message(
        message
    )


# ============================================================
# CHANNEL AUTO ARCHIVE
# ============================================================

@router.channel_post()
async def channel_post_handler(
    message
):

    text = get_message_text(
        message
    )

    if not text:
        return

    news = load_news()

    # جلوگیری از ثبت تکراری

    for item in news:

        if normalize(
            item.get(
                "text",
                ""
            )
        ) == normalize(text):

            return

    embedding = await create_embedding(
        text
    )

    analysis = None

    # برای کاهش هزینه AI،
    # تحلیل کامل پست‌های کانال
    # به صورت پیش‌فرض خاموش است.

    if (
        os.getenv(
            "ANALYZE_CHANNEL_POSTS",
            "false"
        ).lower()
        ==
        "true"
    ):

        analysis = await analyze_news(
            text
        )

    ids = [

        item.get(
            "id",
            0
        )

        for item in news

        if isinstance(
            item.get(
                "id",
                0
            ),
            int
        )

    ]

    next_id = (
        max(ids or [0])
        + 1
    )

    title = (

        analysis.get(
            "title"
        )
        if analysis
        else
        text.splitlines()[0][:300]

    )

    item = {

        "id": next_id,

        "title": title,

        "text": text,

        "url": get_message_link(
            message
        ),

        "category": (
            analysis.get(
                "category"
            )
            if analysis
            else None
        ),

        "subject": (
            analysis.get(
                "subject"
            )
            if analysis
            else None
        ),

        "event": (
            analysis.get(
                "event"
            )
            if analysis
            else None
        ),

        "analysis": analysis,

        "embedding": embedding,

        "added_by": None,

        "created_at":
            datetime.now(
                timezone.utc
            ).isoformat()

    }

    news.append(
        item
    )

    save_news(
        news
    )

    logger.info(
        "Channel post archived: %s",
        message.message_id
    )


# ============================================================
# SEARCH
# ============================================================

@router.message(
    Command("search")
)
async def search_command(
    message
):

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    query = (
        message.text
        .partition(" ")[2]
        .strip()
    )

    if not query:

        await message.answer(

            "مثال:\n"
            "/search GTA VI"

        )

        return

    normalized_query = normalize(
        query
    )

    results = []

    for item in load_news():

        searchable = normalize(

            " ".join([

                item.get(
                    "title",
                    ""
                ),

                item.get(
                    "text",
                    ""
                ),

                item.get(
                    "subject",
                    ""
                ),

                item.get(
                    "event",
                    ""
                )

            ])

        )

        if normalized_query in searchable:

            results.append(
                item
            )

    results = results[-10:]

    if not results:

        await message.answer(
            "🔎 نتیجه‌ای پیدا نشد."
        )

        return

    output = (
        "🔎 نتایج جست‌وجو:\n\n"
    )

    for item in reversed(
        results
    ):

        output += (

            f"#{item.get('id')} "
            f"— "
            f"{item.get('title', 'بدون عنوان')}\n"

        )

        if item.get(
            "url"
        ):

            output += (
                item["url"]
                + "\n"
            )

        output += "\n"

    await message.answer(
        output
    )


# ============================================================
# RUN
# ============================================================

async def main():

    logger.info(
        "======================================"
    )

    logger.info(
        "GAMEFA AI BOT V2 STARTING"
    )

    logger.info(
        "Admins: %s",
        admins()
    )

    logger.info(
        "AI: %s",
        bool(ai)
    )

    logger.info(
        "OpenAI model: %s",
        OPENAI_MODEL
    )

    logger.info(
        "Embedding model: %s",
        EMBEDDING_MODEL
    )

    logger.info(
        "Data directory: %s",
        DATA_DIR
    )

    logger.info(
        "News count: %s",
        len(load_news())
    )

    logger.info(
        "======================================"
    )

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types()
    )


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped."
        )