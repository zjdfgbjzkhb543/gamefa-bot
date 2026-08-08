import asyncio
import csv
import io
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
# GAMEFA AI BOT
# NO SQLITE
# JSON STORAGE
# AUTO IMPORT LAST 100 CHANNEL POSTS
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

# ============================================================
# TELETHON
# ============================================================

TELEGRAM_API_ID = os.getenv(
    "API_ID",
    ""
).strip()

TELEGRAM_API_HASH = os.getenv(
    "API_HASH",
    ""
).strip()

TELEGRAM_SESSION = os.getenv(
    "TELEGRAM_SESSION",
    "gamefa_user"
).strip()

CHANNEL_USERNAME = os.getenv(
    "CHANNEL_USERNAME",
    "@Gamefa_official"
).strip()

ARCHIVE_LIMIT = int(
    os.getenv(
        "ARCHIVE_LIMIT",
        "100"
    )
)

AUTO_ARCHIVE_CHANNEL = (
    os.getenv(
        "AUTO_ARCHIVE_CHANNEL",
        "true"
    ).lower()
    == "true"
)

# ============================================================
# DATA
# ============================================================

DATA_DIR = Path(
    os.getenv(
        "DATA_DIR",
        "/data"
    )
)

MAX_NEWS = int(
    os.getenv(
        "MAX_NEWS",
        "1000"
    )
)

NEWS_FILE = DATA_DIR / "news.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

# ============================================================
# ADMIN
# ============================================================

def get_admin_ids():
    raw = os.getenv(
        "ADMIN_IDS",
        ""
    )

    result = set()

    for item in raw.split(","):
        item = item.strip()

        if item.isdigit():
            result.add(int(item))

    return result


ADMIN_IDS = get_admin_ids()

PRIMARY_ADMIN_ID = int(
    os.getenv(
        "PRIMARY_ADMIN_ID",
        "0"
    ) or 0
)

# ============================================================
# DIRECTORY
# ============================================================

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

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(
    "gamefa"
)

# ============================================================
# BOT
# ============================================================

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

# ============================================================
# OPENAI
# ============================================================

ai: Optional[AsyncOpenAI] = None

if OPENAI_API_KEY:
    ai = AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )

# ============================================================
# TELETHON
# ============================================================

telethon_client = None

try:
    from telethon import TelegramClient
except ImportError:
    TelegramClient = None


# ============================================================
# TEMP STORAGE
# ============================================================

PENDING = {}
AWAITING = {}
IMPORT_SESSIONS = {}

# ============================================================
# JSON
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
# NEWS
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
        "channel_id": CHANNEL_USERNAME
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
        CHANNEL_USERNAME
    )

    return data


SETTINGS = load_settings()


def save_settings():

    save_json(
        SETTINGS_FILE,
        SETTINGS
    )


# ============================================================
# ADMIN
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
# NORMALIZE
# ============================================================

def normalize(text):

    if not text:
        return ""

    text = str(text).lower()

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
# SIMILARITY
# ============================================================

def lexical_similarity(first, second):

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


def cosine_similarity(
    first,
    second
):

    if not first or not second:
        return 0.0

    if len(first) != len(second):
        return 0.0

    first_length = sum(
        value * value
        for value in first
    ) ** 0.5

    second_length = sum(
        value * value
        for value in second
    ) ** 0.5

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
# AI ANALYSIS
# ============================================================

ANALYSIS_PROMPT = """
تو دستیار هوش مصنوعی تحریریه گیمفا هستی.

خبر را دقیق تحلیل کن.

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

category یکی از:

بازی
فیلم و سریال
فناوری
هوش مصنوعی
سایر

source_type یکی از:

رسمی
گزارش رسانه‌ای
شایعه
تأییدنشده
نامشخص

importance یکی از:

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
# REWRITE
# ============================================================

async def rewrite_news(text):

    prompt = """
تو ویراستار خبری گیمفا هستی.

خبر زیر را به فارسی روان،
حرفه‌ای و مناسب انتشار در کانال گیمفا
بازنویسی کن.

هیچ اطلاعات جدیدی اضافه نکن.

فقط JSON:

{
"title":"",
"body":"",
"hashtags":[]
}
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

برای این خبر یک تیتر خبری فارسی،
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

۵ هشتگ مناسب برای این خبر پیشنهاد بده.
"""

    return await ask_ai_json(
        prompt,
        text
    )


# ============================================================
# NEXT ID
# ============================================================

def next_news_id(news):

    ids = []

    for item in news:

        value = item.get(
            "id"
        )

        if isinstance(
            value,
            int
        ):
            ids.append(value)

    return max(
        ids or [0]
    ) + 1


# ============================================================
# TELETHON CHANNEL ARCHIVE
# ============================================================

async def create_telethon_client():

    global telethon_client

    if TelegramClient is None:

        logger.error(
            "Telethon is not installed."
        )

        return None

    if (
        not TELEGRAM_API_ID or
        not TELEGRAM_API_HASH
    ):

        logger.warning(
            "API_ID/API_HASH are missing. "
            "Automatic historical archive disabled."
        )

        return None

    try:

        api_id = int(
            TELEGRAM_API_ID
        )

    except ValueError:

        logger.error(
            "API_ID must be numeric."
        )

        return None

    session_path = str(
        DATA_DIR /
        TELEGRAM_SESSION
    )

    client = TelegramClient(
        session_path,
        api_id,
        TELEGRAM_API_HASH
    )

    await client.start()

    logger.info(
        "Telethon connected."
    )

    telethon_client = client

    return client


# ============================================================
# CONVERT TELETHON MESSAGE
# ============================================================

def telethon_message_to_text(
    message
):

    text = (
        getattr(
            message,
            "message",
            None
        )
        or ""
    )

    return text.strip()


def telethon_message_link(
    message,
    channel
):

    try:

        username = getattr(
            channel,
            "username",
            None
        )

        message_id = getattr(
            message,
            "id",
            None
        )

        if username and message_id:

            return (
                f"https://t.me/"
                f"{username}/"
                f"{message_id}"
            )

    except Exception:
        pass

    return ""


# ============================================================
# ARCHIVE ONE TELEGRAM POST
# ============================================================

async def archive_telegram_post(
    message,
    channel,
    news,
    added_by=None
):

    text = telethon_message_to_text(
        message
    )

    if not text:
        return None, "empty"

    normalized = normalize(
        text
    )

    if not normalized:
        return None, "empty"

    # Exact duplicate
    for item in news:

        if normalize(
            item.get(
                "text",
                ""
            )
        ) == normalized:

            return item, "duplicate"

    analysis = None
    embedding = None

    # AI analysis
    if ai:

        try:

            analysis = await analyze_news(
                text
            )

        except Exception:

            logger.exception(
                "AI analysis failed"
            )

        try:

            embedding = await create_embedding(
                text
            )

        except Exception:

            logger.exception(
                "Embedding failed"
            )

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if analysis:

        title = (
            analysis.get(
                "title"
            )
            or
            (
                lines[0][:300]
                if lines
                else
                "بدون عنوان"
            )
        )

    else:

        title = (
            lines[0][:300]
            if lines
            else
            "بدون عنوان"
        )

    telegram_message_id = getattr(
        message,
        "id",
        None
    )

    item = {

        "id": next_news_id(
            news
        ),

        "telegram_message_id":
            telegram_message_id,

        "title": title,

        "text": text,

        "url":
            telethon_message_link(
                message,
                channel
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

        "added_by": added_by,

        "created_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "imported": True,

        "source": "telegram_channel"

    }

    news.append(
        item
    )

    return item, "new"


# ============================================================
# AUTOMATIC LAST 100 POSTS
# ============================================================

async def import_last_channel_posts(
    limit=None
):

    if not AUTO_ARCHIVE_CHANNEL:

        logger.info(
            "AUTO_ARCHIVE_CHANNEL=false"
        )

        return {
            "new": 0,
            "duplicate": 0,
            "empty": 0,
            "error": None
        }

    limit = limit or ARCHIVE_LIMIT

    client = telethon_client

    if not client:

        logger.warning(
            "Telethon client is unavailable."
        )

        return {
            "new": 0,
            "duplicate": 0,
            "empty": 0,
            "error":
                "Telethon is not configured."
        }

    logger.info(
        "Fetching last %s posts from %s",
        limit,
        CHANNEL_USERNAME
    )

    news = load_news()

    result = {
        "new": 0,
        "duplicate": 0,
        "empty": 0,
        "error": None
    }

    try:

        channel = await client.get_entity(
            CHANNEL_USERNAME
        )

        messages = []

        async for message in client.iter_messages(
            channel,
            limit=limit
        ):

            messages.append(
                message
            )

        # قدیمی → جدید
        messages.reverse()

        total = len(
            messages
        )

        logger.info(
            "Found %s channel posts.",
            total
        )

        for index, message in enumerate(
            messages,
            start=1
        ):

            try:

                item, status = (
                    await archive_telegram_post(
                        message,
                        channel,
                        news
                    )
                )

                if status == "new":

                    result["new"] += 1

                    # ذخیره دوره‌ای
                    if (
                        result["new"] % 10 == 0
                    ):
                        save_news(
                            news
                        )

                elif status == "duplicate":

                    result["duplicate"] += 1

                elif status == "empty":

                    result["empty"] += 1

                logger.info(
                    "Archive progress: %s/%s",
                    index,
                    total
                )

                # فاصله برای API
                if ai:
                    await asyncio.sleep(
                        0.15
                    )

            except Exception:

                logger.exception(
                    "Failed to archive Telegram message %s",
                    getattr(
                        message,
                        "id",
                        "?"
                    )
                )

        save_news(
            news
        )

        logger.info(
            "Historical archive finished: "
            "new=%s duplicate=%s empty=%s total=%s",
            result["new"],
            result["duplicate"],
            result["empty"],
            len(news)
        )

    except Exception as exc:

        logger.exception(
            "Could not import Telegram archive."
        )

        result["error"] = str(
            exc
        )

    return result


# ============================================================
# TELETHON NEW POSTS
# ============================================================

async def telegram_event_listener():

    if not telethon_client:
        return

    try:

        from telethon import events

        channel = await telethon_client.get_entity(
            CHANNEL_USERNAME
        )

        @telethon_client.on(
            events.NewMessage(
                chats=channel
            )
        )
        async def new_channel_message(
            event
        ):

            try:

                message = event.message

                news = load_news()

                item, status = (
                    await archive_telegram_post(
                        message,
                        channel,
                        news
                    )
                )

                if status == "new":

                    save_news(
                        news
                    )

                    logger.info(
                        "New channel post archived: %s",
                        getattr(
                            message,
                            "id",
                            "?"
                        )
                    )

            except Exception:

                logger.exception(
                    "Channel event error"
                )

        logger.info(
            "Telegram channel listener started."
        )

    except Exception:

        logger.exception(
            "Could not start Telegram event listener."
        )


# ============================================================
# ARCHIVE MENU
# ============================================================

def archive_keyboard():

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="📥 ورود دستی آرشیو",
                    callback_data="archive_import"
                )

            ],

            [

                InlineKeyboardButton(
                    text="🔄 دریافت ۱۰۰ خبر آخر",
                    callback_data="archive_auto_import"
                )

            ],

            [

                InlineKeyboardButton(
                    text="📊 آمار آرشیو",
                    callback_data="menu_stats"
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


# ============================================================
# MAIN MENU
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
                    text="🔎 جست‌وجو",
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
                    text="🧠 هوش مصنوعی",
                    callback_data="menu_ai"
                )

            ],

            [

                InlineKeyboardButton(
                    text="👥 ادمین‌ها",
                    callback_data="menu_admins"
                ),

                InlineKeyboardButton(
                    text="⚙️ تنظیمات",
                    callback_data="menu_settings"
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
async def start_command(
    message
):

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
        "سیستم ضدخبرتکراری، آرشیو "
        "و دستیار هوش مصنوعی آماده است.",

        reply_markup=main_keyboard()

    )


# ============================================================
# HELP
# ============================================================

@router.message(
    Command("help")
)
async def help_command(
    message
):

    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    await message.answer(

        "راهنمای ربات گیمفا\n\n"

        "📰 خبر را برای ربات ارسال کن.\n"
        "ربات آن را با آرشیو مقایسه می‌کند.\n\n"

        "📚 برای آرشیو خودکار:\n"
        "پنل ← آرشیو ← دریافت ۱۰۰ خبر آخر\n\n"

        "🤖 هنگام اجرای ربات نیز "
        "۱۰۰ پست آخر کانال به‌صورت خودکار "
        "بررسی می‌شوند.\n\n"

        "📚 حداکثر ۱۰۰۰ خبر نگهداری می‌شود."

    )


# ============================================================
# ARCHIVE MENU
# ============================================================

@router.callback_query(
    F.data == "menu_archive"
)
async def archive_menu(
    callback: CallbackQuery
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    news = load_news()

    await callback.message.edit_text(

        "📚 مدیریت آرشیو گیمفا\n\n"

        f"تعداد اخبار فعلی: "
        f"{len(news)}/{MAX_NEWS}\n\n"

        f"کانال: {CHANNEL_USERNAME}\n"
        f"تعداد دریافت خودکار: {ARCHIVE_LIMIT}\n\n"

        "می‌توانی آرشیو را به‌صورت خودکار "
        "از کانال دریافت کنی.",

        reply_markup=archive_keyboard()

    )

    await callback.answer()


# ============================================================
# MANUAL ARCHIVE FILE
# ============================================================

def archive_import_keyboard():

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="❌ لغو",
                    callback_data="archive_cancel"
                )

            ]

        ]

    )


@router.callback_query(
    F.data == "archive_import"
)
async def archive_import_start(
    callback: CallbackQuery
):

    if not is_primary_admin(
        callback.from_user.id
    ):

        await callback.answer(
            "⛔ فقط ادمین اصلی.",
            show_alert=True
        )

        return

    IMPORT_SESSIONS[
        callback.from_user.id
    ] = {
        "status": "waiting_file"
    }

    await callback.message.answer(

        "📥 ورود دستی آرشیو\n\n"

        "فایل TXT، JSON یا CSV را بفرست.\n\n"

        "اما برای آرشیو کانال نیازی به این کار نیست؛ "
        "از گزینه «دریافت ۱۰۰ خبر آخر» استفاده کن.",

        reply_markup=archive_import_keyboard()

    )

    await callback.answer()


# ============================================================
# AUTO IMPORT BUTTON
# ============================================================

@router.callback_query(
    F.data == "archive_auto_import"
)
async def archive_auto_import(
    callback: CallbackQuery
):

    if not is_primary_admin(
        callback.from_user.id
    ):

        await callback.answer(
            "⛔ فقط ادمین اصلی.",
            show_alert=True
        )

        return

    if not telethon_client:

        await callback.message.answer(

            "❌ اتصال تلگرام فعال نیست.\n\n"

            "این موارد را در Railway Variables "
            "تنظیم کن:\n\n"

            "API_ID\n"
            "API_HASH\n"
            "CHANNEL_USERNAME\n"
            "TELEGRAM_SESSION\n\n"

            "همچنین باید Telethon نصب باشد."

        )

        await callback.answer()

        return

    await callback.message.answer(

        f"📥 در حال دریافت {ARCHIVE_LIMIT} "
        "پست آخر کانال...\n\n"
        "لطفاً صبر کن."

    )

    result = await import_last_channel_posts(
        ARCHIVE_LIMIT
    )

    if result["error"]:

        await callback.message.answer(

            "❌ دریافت آرشیو ناموفق بود.\n\n"

            f"خطا:\n{result['error']}"

        )

    else:

        await callback.message.answer(

            "✅ آرشیو کانال دریافت شد.\n\n"

            f"🆕 جدید: {result['new']}\n"
            f"🔁 تکراری: {result['duplicate']}\n"
            f"⚪ بدون متن: {result['empty']}\n\n"

            f"📚 آرشیو فعلی: "
            f"{len(load_news())}/{MAX_NEWS}"

        )

    await callback.answer()


# ============================================================
# CANCEL
# ============================================================

@router.callback_query(
    F.data == "archive_cancel"
)
async def archive_cancel(
    callback: CallbackQuery
):

    IMPORT_SESSIONS.pop(
        callback.from_user.id,
        None
    )

    await callback.message.answer(
        "❌ عملیات لغو شد."
    )

    await callback.answer()


# ============================================================
# STATS
# ============================================================

async def send_stats(
    message
):

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

        categories[
            category
        ] = (
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
            f"• {category}: {count}\n"
        )

    await message.answer(
        text
    )


@router.message(
    Command("stats")
)
async def stats_command(
    message
):

    if (
        message.from_user and
        is_admin(
            message.from_user.id
        )
    ):

        await send_stats(
            message
        )


@router.callback_query(
    F.data == "menu_stats"
)
async def menu_stats(
    callback: CallbackQuery
):

    if not is_admin(
        callback.from_user.id
    ):
        return

    await send_stats(
        callback.message
    )

    await callback.answer()


# ============================================================
# AI MENU
# ============================================================

def ai_keyboard():

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="✍️ بازنویسی",
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
                    text="🚨 بررسی شایعه",
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


@router.callback_query(
    F.data == "menu_ai"
)
async def menu_ai(
    callback: CallbackQuery
):

    await callback.message.edit_text(

        "🧠 ابزارهای هوش مصنوعی:",

        reply_markup=ai_keyboard()

    )

    await callback.answer()


# ============================================================
# ADMIN MENU
# ============================================================

def admin_keyboard():

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="➕ افزودن",
                    callback_data="admin_add"
                ),

                InlineKeyboardButton(
                    text="➖ حذف",
                    callback_data="admin_remove"
                )

            ],

            [

                InlineKeyboardButton(
                    text="📋 لیست",
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


@router.callback_query(
    F.data == "menu_admins"
)
async def menu_admins(
    callback: CallbackQuery
):

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
async def admin_list(
    callback: CallbackQuery
):

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
async def admin_add(
    callback: CallbackQuery
):

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
async def admin_remove(
    callback: CallbackQuery
):

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
async def menu_settings(
    callback: CallbackQuery
):

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

    await callback.message.answer(

        "⚙️ تنظیمات\n\n"

        f"📚 آرشیو: "
        f"{news_count}/{MAX_NEWS}\n\n"

        f"📊 آستانه شباهت: "
        f"{SETTINGS.get('similarity_threshold', 0.72)}\n\n"

        f"🧠 هوش مصنوعی: "
        f"{'فعال' if ai else 'غیرفعال'}\n\n"

        f"📡 کانال: "
        f"{CHANNEL_USERNAME}\n\n"

        f"📥 آرشیو خودکار: "
        f"{ARCHIVE_LIMIT} پست\n\n"

        f"📡 Telethon: "
        f"{'فعال' if telethon_client else 'غیرفعال'}"

    )

    await callback.answer()


# ============================================================
# AI TOOLS
# ============================================================

async def run_ai_tool(
    message,
    action,
    text
):

    if not ai:

        await message.answer(
            "⚠️ OPENAI_API_KEY تنظیم نشده."
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

                "🚨 بررسی خبر\n\n"

                f"وضعیت: "
                f"{result.get('source_type', 'نامشخص')}\n\n"

                f"منبع: "
                f"{result.get('source') or 'نامشخص'}\n\n"

                f"🧠 تحلیل:\n"
                f"{result.get('reason', '')}"

            )


@router.callback_query(
    F.data.startswith("ai_")
)
async def ai_tools(
    callback: CallbackQuery
):

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
# NEW NEWS KEYBOARD
# ============================================================

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


# ============================================================
# DUPLICATE KEYBOARD
# ============================================================

def duplicate_keyboard(
    news_index
):

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="👀 خبر قبلی",
                    callback_data=f"old_{news_index}"
                )

            ],

            [

                InlineKeyboardButton(
                    text="⚖️ مقایسه",
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
                    text="✍️ بازنویسی",
                    callback_data="pending_rewrite"
                )

            ],

            [

                InlineKeyboardButton(
                    text="✅ خبر جدید است",
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
# PENDING SAVE
# ============================================================

@router.callback_query(
    F.data == "pending_save"
)
async def pending_save(
    callback: CallbackQuery
):

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

    analysis = (
        pending.get(
            "analysis"
        )
        or {}
    )

    item = {

        "id": next_news_id(
            news
        ),

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

        "💾 خبر با موفقیت ثبت شد.\n\n"

        f"📚 آرشیو: "
        f"{len(load_news())}/{MAX_NEWS}"

    )

    await callback.answer()


# ============================================================
# PENDING AI BUTTONS
# ============================================================

async def run_pending_ai(
    message,
    action,
    text
):

    await run_ai_tool(
        message,
        action,
        text
    )


@router.callback_query(
    F.data == "pending_rewrite"
)
async def pending_rewrite(
    callback: CallbackQuery
):

    pending = PENDING.get(
        callback.from_user.id
    )

    if not pending:
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
async def pending_title(
    callback: CallbackQuery
):

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
async def pending_summary(
    callback: CallbackQuery
):

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
async def pending_hashtags(
    callback: CallbackQuery
):

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
async def pending_publish(
    callback: CallbackQuery
):

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
async def show_old_news(
    callback: CallbackQuery
):

    try:

        index = int(
            callback.data.split(
                "_"
            )[1]
        )

        news = load_news()

        item = news[index]

        await callback.message.answer(

            "📰 خبر قبلی:\n\n"
            +
            item.get(
                "text",
                ""
            )

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
    callback: CallbackQuery
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

        if not pending:
            raise ValueError(
                "Pending news not found"
            )

        prompt = """
دو خبر را مقایسه کن.

فقط JSON:

{
"same_event":true,
"score":0,
"reason":"",
"differences":[]
}

score بین 0 تا 100.
"""

        result = await ask_ai_json(

            prompt,

            "خبر جدید:\n"
            + pending["text"]
            + "\n\nخبر قبلی:\n"
            + old_news.get(
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

            f"📊 شباهت: "
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
    callback: CallbackQuery
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

        if not pending:
            raise ValueError(
                "Pending news not found"
            )

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

        result = await ask_ai_json(

            prompt,

            "خبر جدید:\n"
            + pending["text"]
            + "\n\nخبر قبلی:\n"
            + old_news.get(
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
    callback: CallbackQuery
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
            "مثال:\n/search GTA VI"
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

            f"#{item.get('id')} — "
            f"{item.get('title', 'بدون عنوان')}\n\n"

        )

    await message.answer(
        output
    )


# ============================================================
# MAIN MENU
# ============================================================

@router.callback_query(
    F.data == "main_menu"
)
async def main_menu(
    callback: CallbackQuery
):

    await callback.message.edit_text(

        "🤖 پنل مدیریت گیمفا",

        reply_markup=main_keyboard()

    )

    await callback.answer()


# ============================================================
# CHECK MENU
# ============================================================

@router.callback_query(
    F.data == "menu_check"
)
async def menu_check(
    callback: CallbackQuery
):

    await callback.message.answer(

        "📰 متن خبر را ارسال کن.\n\n"
        "ربات قبل از انتشار آن را بررسی می‌کند."

    )

    await callback.answer()


# ============================================================
# PROCESS NEWS
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

    waiting = AWAITING.get(
        user_id
    )

    # ========================================================
    # ADD ADMIN
    # ========================================================

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

        SETTINGS[
            "admins"
        ] = sorted(
            current
        )

        save_settings()

        AWAITING.pop(
            user_id,
            None
        )

        await message.answer(
            "✅ ادمین اضافه شد."
        )

        return

    # ========================================================
    # REMOVE ADMIN
    # ========================================================

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

        SETTINGS[
            "admins"
        ] = sorted(
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

    # ========================================================
    # AI TOOL
    # ========================================================

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

        await run_ai_tool(
            message,
            action,
            text
        )

        return

    # ========================================================
    # COMMAND
    # ========================================================

    if text.startswith("/"):
        return

    # ========================================================
    # EXACT DUPLICATE
    # ========================================================

    news = load_news()

    for item in news:

        if normalize(
            item.get(
                "text",
                ""
            )
        ) == normalize(text):

            await message.answer(

                "🔴 این خبر قبلاً در آرشیو وجود دارد.\n\n"

                f"📰 خبر قبلی:\n\n"
                f"{item.get('text', '')}"

            )

            return

    # ========================================================
    # AI
    # ========================================================

    await message.answer(
        "🧠 در حال بررسی خبر..."
    )

    analysis = await analyze_news(
        text
    )

    embedding = await create_embedding(
        text
    )

    # ========================================================
    # SIMILAR NEWS
    # ========================================================

    matches = []

    for item in news:

        lexical = lexical_similarity(

            text,

            item.get(
                "text",
                ""
            )

        )

        semantic = 0.0

        if (
            embedding and
            item.get(
                "embedding"
            )
        ):

            semantic = cosine_similarity(

                embedding,

                item["embedding"]

            )

        if (
            embedding and
            item.get(
                "embedding"
            )
        ):

            score = (
                0.75 * semantic +
                0.25 * lexical
            )

        else:

            score = lexical

        if score >= 0.18:

            matches.append({

                "score": score,

                "lexical": lexical,

                "semantic": semantic,

                "news": item

            })

    matches.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    matches = matches[:5]

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

    # ========================================================
    # DUPLICATE
    # ========================================================

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

                if item.get("id")
                ==
                old_news.get("id")

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

        await message.answer(

            output,

            reply_markup=duplicate_keyboard(
                index
            )

        )

        return

    # ========================================================
    # NEW
    # ========================================================

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

            "\n\n📰 تیتر:\n"
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


# ============================================================
# TEXT HANDLER
# ============================================================

@router.message(
    F.text | F.caption
)
async def text_handler(
    message: Message
):

    await process_message(
        message
    )


# ============================================================
# RUN
# ============================================================

async def main():

    global telethon_client

    logger.info(
        "======================================"
    )

    logger.info(
        "GAMEFA AI BOT STARTING"
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
        "Channel: %s",
        CHANNEL_USERNAME
    )

    logger.info(
        "Archive limit: %s",
        ARCHIVE_LIMIT
    )

    logger.info(
        "Data directory: %s",
        DATA_DIR
    )

    logger.info(
        "Existing news: %s",
        len(load_news())
    )

    logger.info(
        "======================================"
    )

    # ========================================================
    # TELETHON
    # ========================================================

    telethon_client = (
        await create_telethon_client()
    )

    if telethon_client:

        # دریافت ۱۰۰ خبر آخر
        await import_last_channel_posts(
            ARCHIVE_LIMIT
        )

        # فعال کردن پست‌های جدید
        await telegram_event_listener()

    else:

        logger.warning(
            "Automatic Telegram archive is disabled."
        )

    # ========================================================
    # BOT POLLING
    # ========================================================

    await dp.start_polling(
        bot,
        allowed_updates=
            dp.resolve_used_update_types()
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
