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
# GAMEFA AI BOT V2 + ARCHIVE IMPORT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OPENAI_API_KEY = os.getenv(
    "OPENAI_API_KEY",
    ""
).strip()

OPENAI_MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-4.1-mini"
).strip()

EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "text-embedding-3-small"
).strip()

DATA_DIR = Path(
    os.getenv(
        "DATA_DIR",
        "/data"
    )
)

MAX_NEWS = 1000

# ============================================================
# ADMIN IDS
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
# FILES
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

NEWS_FILE = DATA_DIR / "news.json"
SETTINGS_FILE = DATA_DIR / "settings.json"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("gamefa")


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
# TEMP STORAGE
# ============================================================

PENDING = {}

AWAITING = {}

IMPORT_SESSIONS = {}


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

        "primary_admin":
            PRIMARY_ADMIN_ID,

        "similarity_threshold":
            0.72,

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
# COSINE SIMILARITY
# ============================================================

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
# AI NEWS ANALYSIS
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
حرفه‌ای و مناسب انتشار در کانال
گیمفا بازنویسی کن.

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

۵ هشتگ مناسب برای این خبر
پیشنهاد بده.

"""

    return await ask_ai_json(
        prompt,
        text
    )


# ============================================================
# ARCHIVE IMPORT
# ============================================================

def clean_import_text(text):

    if not text:
        return ""

    text = str(text)

    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    # حذف فاصله‌های غیرضروری
    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    # حذف خطوط کاملاً خالی ابتدا و انتها
    text = text.strip()

    return text


def extract_news_from_txt(text):

    text = clean_import_text(
        text
    )

    if not text:
        return []

    # --------------------------------------------------------
    # حالت ۱:
    # هر خبر با خط خالی جدا شده
    # --------------------------------------------------------

    blocks = re.split(
        r"\n\s*\n+",
        text
    )

    blocks = [
        clean_import_text(block)
        for block in blocks
        if clean_import_text(block)
    ]

    # --------------------------------------------------------
    # اگر فایل فقط یک متن بزرگ بود،
    # آن را به خطوط تبدیل نمی‌کنیم.
    # --------------------------------------------------------

    if len(blocks) == 1:

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        # اگر تعداد خطوط زیاد باشد،
        # هر خط را خبر در نظر می‌گیریم.
        if len(lines) >= 10:

            return lines

    return blocks


def extract_news_from_json(data):

    results = []

    # --------------------------------------------------------
    # لیست ساده
    # --------------------------------------------------------

    if isinstance(data, list):

        for item in data:

            if isinstance(
                item,
                str
            ):

                text = clean_import_text(
                    item
                )

                if text:
                    results.append(text)

            elif isinstance(
                item,
                dict
            ):

                text = extract_text_from_dict(
                    item
                )

                if text:
                    results.append(text)

        return results

    # --------------------------------------------------------
    # دیکشنری
    # --------------------------------------------------------

    if isinstance(data, dict):

        # آرشیوهای رایج
        for key in (
            "messages",
            "news",
            "items",
            "posts",
            "data",
            "results"
        ):

            value = data.get(
                key
            )

            if isinstance(
                value,
                list
            ):

                return extract_news_from_json(
                    value
                )

        # اگر خود دیکشنری یک خبر باشد
        text = extract_text_from_dict(
            data
        )

        if text:
            results.append(text)

    return results


def extract_text_from_dict(item):

    if not isinstance(
        item,
        dict
    ):
        return ""

    # اولویت متن کامل خبر
    possible_keys = [

        "text",
        "body",
        "content",
        "message",
        "caption",
        "description",
        "news"

    ]

    for key in possible_keys:

        value = item.get(
            key
        )

        # Telegram JSON ممکن است
        # text را به صورت list داشته باشد.
        if isinstance(
            value,
            list
        ):

            parts = []

            for part in value:

                if isinstance(
                    part,
                    str
                ):

                    parts.append(
                        part
                    )

                elif isinstance(
                    part,
                    dict
                ):

                    inner = part.get(
                        "text",
                        ""
                    )

                    if inner:
                        parts.append(
                            str(inner)
                        )

            value = "".join(
                parts
            )

        if isinstance(
            value,
            str
        ):

            value = clean_import_text(
                value
            )

            if value:
                return value

    # اگر متن پیدا نشد، title + summary
    title = str(
        item.get(
            "title",
            ""
        )
    ).strip()

    summary = str(
        item.get(
            "summary",
            ""
        )
    ).strip()

    if title or summary:

        return clean_import_text(

            title
            + "\n\n"
            + summary

        )

    return ""


def extract_news_from_csv(text):

    results = []

    stream = io.StringIO(
        text
    )

    try:

        reader = csv.DictReader(
            stream
        )

        for row in reader:

            item = dict(row)

            value = extract_text_from_dict(
                item
            )

            if value:
                results.append(
                    value
                )

    except Exception:

        logger.exception(
            "CSV parsing failed"
        )

    return results


def parse_archive_file(
    filename,
    raw_bytes
):

    extension = Path(
        filename
    ).suffix.lower()

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    if extension == ".json":

        text = raw_bytes.decode(
            "utf-8-sig",
            errors="ignore"
        )

        data = json.loads(
            text
        )

        return extract_news_from_json(
            data
        )

    # --------------------------------------------------------
    # CSV
    # --------------------------------------------------------

    if extension == ".csv":

        text = raw_bytes.decode(
            "utf-8-sig",
            errors="ignore"
        )

        return extract_news_from_csv(
            text
        )

    # --------------------------------------------------------
    # TXT
    # --------------------------------------------------------

    if extension in (
        ".txt",
        ".text",
        ""
    ):

        text = raw_bytes.decode(
            "utf-8-sig",
            errors="ignore"
        )

        return extract_news_from_txt(
            text
        )

    raise ValueError(
        "فرمت فایل پشتیبانی نمی‌شود."
    )


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

            ids.append(
                value
            )

    return max(
        ids or [0]
    ) + 1


async def prepare_import_news(
    texts,
    user_id
):

    current_news = load_news()

    existing_normalized = {
        normalize(
            item.get(
                "text",
                ""
            )
        )
        for item in current_news
    }

    # حذف موارد خالی و تکراری داخل خود فایل
    unique_texts = []

    seen = set()

    for text in texts:

        text = clean_import_text(
            text
        )

        if not text:
            continue

        normalized = normalize(
            text
        )

        if not normalized:
            continue

        if normalized in seen:
            continue

        seen.add(
            normalized
        )

        unique_texts.append(
            text
        )

    new_texts = []

    duplicate_count = 0

    for text in unique_texts:

        normalized = normalize(
            text
        )

        if normalized in existing_normalized:

            duplicate_count += 1

            continue

        new_texts.append(
            text
        )

    return (
        current_news,
        new_texts,
        duplicate_count
    )


async def build_import_items(
    texts,
    current_news
):

    items = []

    current_id = next_news_id(
        current_news
    )

    total = len(texts)

    for number, text in enumerate(
        texts,
        start=1
    ):

        logger.info(
            "Importing archive %s/%s",
            number,
            total
        )

        analysis = None
        embedding = None

        # ----------------------------------------------------
        # AI
        # ----------------------------------------------------

        if ai:

            analysis = await analyze_news(
                text
            )

            embedding = await create_embedding(
                text
            )

        # ----------------------------------------------------
        # TITLE
        # ----------------------------------------------------

        if analysis:

            title = (
                analysis.get(
                    "title"
                )
                or
                text.splitlines()[0][:300]
            )

        else:

            title = (
                text.splitlines()[0][:300]
                if text.splitlines()
                else
                "بدون عنوان"
            )

        item = {

            "id": current_id,

            "title": title,

            "text": text,

            "url": "",

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

            "added_by": user_id,

            "created_at":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "imported": True

        }

        items.append(
            item
        )

        current_id += 1

        # برای جلوگیری از فشار شدید روی API
        if ai:
            await asyncio.sleep(
                0.15
            )

    return items


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


def archive_confirm_keyboard():

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="✅ شروع ورود",
                    callback_data="archive_confirm"
                ),

                InlineKeyboardButton(
                    text="❌ لغو",
                    callback_data="archive_cancel"
                )

            ]

        ]

    )


# ============================================================
# ARCHIVE MENU
# ============================================================

def archive_keyboard():

    return InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(
                    text="📥 ورود آرشیو",
                    callback_data="archive_import"
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


@router.callback_query(
    F.data == "menu_archive"
)
async def archive_menu(
    callback
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

        "از این قسمت می‌توانی آرشیو "
        "قبلی را وارد کنی.",

        reply_markup=archive_keyboard()

    )

    await callback.answer()


# ============================================================
# START ARCHIVE IMPORT
# ============================================================

@router.callback_query(
    F.data == "archive_import"
)
async def archive_import_start(
    callback
):

    if not is_primary_admin(
        callback.from_user.id
    ):

        await callback.answer(
            "⛔ فقط ادمین اصلی می‌تواند آرشیو وارد کند.",
            show_alert=True
        )

        return

    IMPORT_SESSIONS[
        callback.from_user.id
    ] = {

        "status": "waiting_file"

    }

    await callback.message.answer(

        "📥 ورود آرشیو گیمفا\n\n"

        "فایل آرشیو را همینجا برای ربات "
        "ارسال کن.\n\n"

        "فرمت‌های پشتیبانی‌شده:\n"
        "• TXT\n"
        "• JSON\n"
        "• CSV\n\n"

        "💡 برای فایل TXT بهتر است هر خبر "
        "با یک خط خالی از خبر بعدی جدا شده باشد.\n\n"

        "⚠️ ربات حداکثر ۱۰۰۰ خبر را نگه می‌دارد.\n"
        "خبرهای تکراری وارد نمی‌شوند.",

        reply_markup=archive_import_keyboard()

    )

    await callback.answer()


# ============================================================
# ARCHIVE CANCEL
# ============================================================

@router.callback_query(
    F.data == "archive_cancel"
)
async def archive_cancel(
    callback
):

    IMPORT_SESSIONS.pop(
        callback.from_user.id,
        None
    )

    await callback.message.answer(
        "❌ ورود آرشیو لغو شد."
    )

    await callback.answer()


# ============================================================
# RECEIVE ARCHIVE FILE
# ============================================================

@router.message(
    F.document
)
async def archive_document_handler(
    message: Message
):

    if not message.from_user:
        return

    user_id = (
        message.from_user.id
    )

    if not is_primary_admin(
        user_id
    ):
        return

    session = IMPORT_SESSIONS.get(
        user_id
    )

    if not session:
        return

    document = message.document

    if not document:
        return

    filename = (
        document.file_name
        or
        "archive.txt"
    )

    extension = Path(
        filename
    ).suffix.lower()

    if extension not in (
        ".txt",
        ".json",
        ".csv"
    ):

        await message.answer(

            "❌ فرمت فایل پشتیبانی نمی‌شود.\n\n"
            "فقط TXT، JSON و CSV قابل قبول هستند."

        )

        return

    # محدودیت منطقی برای فایل
    # حدود 25MB
    if document.file_size:

        if document.file_size > 25 * 1024 * 1024:

            await message.answer(

                "❌ حجم فایل بیشتر از ۲۵ مگابایت است."

            )

            return

    IMPORT_SESSIONS[
        user_id
    ] = {

        "status": "downloading",

        "filename": filename

    }

    status_message = await message.answer(
        "📥 در حال دریافت فایل..."
    )

    try:

        file = await bot.get_file(
            document.file_id
        )

        buffer = io.BytesIO()

        await bot.download_file(
            file.file_path,
            buffer
        )

        raw_bytes = buffer.getvalue()

        await status_message.edit_text(
            "🔍 فایل دریافت شد؛ در حال استخراج خبرها..."
        )

        texts = parse_archive_file(
            filename,
            raw_bytes
        )

        if not texts:

            IMPORT_SESSIONS.pop(
                user_id,
                None
            )

            await status_message.edit_text(

                "❌ هیچ خبری از فایل استخراج نشد.\n\n"

                "اگر TXT می‌فرستی، بهتر است "
                "هر خبر با یک خط خالی جدا شده باشد."

            )

            return

        # ----------------------------------------------------
        # حذف تکراری‌ها
        # ----------------------------------------------------

        current_news, new_texts, duplicate_count = (
            await prepare_import_news(
                texts,
                user_id
            )
        )

        if not new_texts:

            IMPORT_SESSIONS.pop(
                user_id,
                None
            )

            await status_message.edit_text(

                "⚠️ هیچ خبر جدیدی پیدا نشد.\n\n"

                f"📄 تعداد خبرهای فایل: "
                f"{len(texts)}\n"

                f"🔁 تکراری: "
                f"{duplicate_count}"

            )

            return

        # ----------------------------------------------------
        # محدودیت ۱۰۰۰ خبر
        # ----------------------------------------------------

        available_slots = max(
            0,
            MAX_NEWS - len(current_news)
        )

        # اگر آرشیو پر باشد، خبرهای قدیمی حذف می‌شوند
        # تا خبرهای واردشده جدیدتر باقی بمانند.

        will_import = new_texts

        # ----------------------------------------------------
        # Confirmation
        # ----------------------------------------------------

        IMPORT_SESSIONS[
            user_id
        ] = {

            "status": "confirmation",

            "texts": will_import,

            "current_news": current_news,

            "filename": filename,

            "duplicate_count":
                duplicate_count

        }

        await status_message.edit_text(

            "📦 آرشیو آماده ورود است.\n\n"

            f"📄 خبرهای استخراج‌شده: "
            f"{len(texts)}\n"

            f"🆕 خبرهای جدید: "
            f"{len(will_import)}\n"

            f"🔁 تکراری‌ها: "
            f"{duplicate_count}\n\n"

            f"📚 آرشیو فعلی: "
            f"{len(current_news)}/{MAX_NEWS}\n\n"

            "⚠️ با تأیید، خبرهای جدید وارد آرشیو "
            "می‌شوند و در صورت عبور از ظرفیت ۱۰۰۰، "
            "قدیمی‌ترین خبرها حذف خواهند شد.\n\n"

            "آیا ادامه می‌دهی؟",

            reply_markup=archive_confirm_keyboard()

        )

    except json.JSONDecodeError:

        IMPORT_SESSIONS.pop(
            user_id,
            None
        )

        await status_message.edit_text(

            "❌ فایل JSON معتبر نیست.\n\n"
            "ساختار فایل را بررسی کن."

        )

    except UnicodeDecodeError:

        IMPORT_SESSIONS.pop(
            user_id,
            None
        )

        await status_message.edit_text(

            "❌ encoding فایل قابل خواندن نیست.\n\n"
            "فایل را با UTF-8 ذخیره کن."

        )

    except Exception:

        IMPORT_SESSIONS.pop(
            user_id,
            None
        )

        logger.exception(
            "Archive import failed"
        )

        await status_message.edit_text(

            "❌ هنگام خواندن آرشیو خطایی رخ داد.\n\n"
            "جزئیات خطا در Railway Logs ثبت شده است."

        )


# ============================================================
# CONFIRM ARCHIVE IMPORT
# ============================================================

@router.callback_query(
    F.data == "archive_confirm"
)
async def archive_confirm(
    callback
):

    user_id = callback.from_user.id

    if not is_primary_admin(
        user_id
    ):

        await callback.answer(
            "⛔ فقط ادمین اصلی.",
            show_alert=True
        )

        return

    session = IMPORT_SESSIONS.get(
        user_id
    )

    if not session:

        await callback.answer(
            "جلسه ورود آرشیو پیدا نشد.",
            show_alert=True
        )

        return

    if session.get(
        "status"
    ) != "confirmation":

        await callback.answer(
            "این آرشیو آماده ورود نیست.",
            show_alert=True
        )

        return

    texts = session[
        "texts"
    ]

    current_news = session[
        "current_news"
    ]

    await callback.message.edit_text(

        "🚀 ورود آرشیو شروع شد...\n\n"

        f"📰 تعداد خبرها: {len(texts)}\n\n"

        "لطفاً تا پایان عملیات صبر کن."

    )

    try:

        # ----------------------------------------------------
        # اگر تعداد خیلی زیاد است،
        # embedding و AI ممکن است زمان ببرد.
        # ----------------------------------------------------

        imported_items = []

        current_id = next_news_id(
            current_news
        )

        total = len(texts)

        for number, text in enumerate(
            texts,
            start=1
        ):

            analysis = None
            embedding = None

            if ai:

                analysis = await analyze_news(
                    text
                )

                embedding = await create_embedding(
                    text
                )

            lines = [
                line.strip()
                for line in text.splitlines()
                if line.strip()
            ]

            title = (
                analysis.get(
                    "title"
                )
                if analysis
                else
                (
                    lines[0][:300]
                    if lines
                    else
                    "بدون عنوان"
                )
            )

            item = {

                "id": current_id,

                "title": title,

                "text": text,

                "url": "",

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

                "added_by": user_id,

                "created_at":
                    datetime.now(
                        timezone.utc
                    ).isoformat(),

                "imported": True

            }

            imported_items.append(
                item
            )

            current_id += 1

            # هر 10 خبر یک پیام وضعیت
            if (
                number == 1
                or
                number % 10 == 0
                or
                number == total
            ):

                try:

                    await callback.message.edit_text(

                        "🚀 در حال وارد کردن آرشیو...\n\n"

                        f"📥 پیشرفت: "
                        f"{number}/{total}\n\n"

                        f"🧠 هوش مصنوعی: "
                        f"{'فعال' if ai else 'غیرفعال'}"

                    )

                except Exception:
                    pass

            # کمی فاصله برای API
            if ai:

                await asyncio.sleep(
                    0.15
                )

        # ----------------------------------------------------
        # ذخیره
        # ----------------------------------------------------

        final_news = (
            current_news
            +
            imported_items
        )

        # فقط ۱۰۰۰ خبر آخر
        if len(final_news) > MAX_NEWS:

            final_news = final_news[
                -MAX_NEWS:
            ]

        save_news(
            final_news
        )

        IMPORT_SESSIONS.pop(
            user_id,
            None
        )

        removed = max(
            0,
            len(current_news)
            +
            len(imported_items)
            -
            MAX_NEWS
        )

        await callback.message.edit_text(

            "✅ ورود آرشیو با موفقیت انجام شد.\n\n"

            f"📥 واردشده: "
            f"{len(imported_items)}\n"

            f"🔁 تکراری‌های حذف‌شده: "
            f"{session.get('duplicate_count', 0)}\n"

            f"🗑 حذف‌شده به دلیل ظرفیت ۱۰۰۰: "
            f"{removed}\n\n"

            f"📚 آرشیو فعلی: "
            f"{len(final_news)}/{MAX_NEWS}\n\n"

            f"🧠 هوش مصنوعی: "
            f"{'فعال' if ai else 'غیرفعال'}"

        )

    except Exception:

        logger.exception(
            "Archive processing failed"
        )

        IMPORT_SESSIONS.pop(
            user_id,
            None
        )

        await callback.message.edit_text(

            "❌ ورود آرشیو با خطا مواجه شد.\n\n"
            "Railway Logs را بررسی کن."

        )

    await callback.answer()


# ============================================================
# MAIN KEYBOARD
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
# AI KEYBOARD
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


# ============================================================
# ADMIN KEYBOARD
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


# ============================================================
# NEWS KEYBOARDS
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
        "ربات آن را با آرشیو مقایسه می‌کند.\n\n"

        "📥 برای ورود آرشیو:\n"
        "پنل ← آرشیو ← ورود آرشیو\n\n"

        "📚 حداکثر ۱۰۰۰ خبر نگهداری می‌شود.\n\n"

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
            f"• {category}: {count}\n"
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
# CALLBACK: MAIN MENU
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


# ============================================================
# CALLBACK: CHECK
# ============================================================

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


# ============================================================
# CALLBACK: STATS
# ============================================================

@router.callback_query(
    F.data == "menu_stats"
)
async def menu_stats(callback):

    await send_stats(
        callback.message
    )

    await callback.answer()


# ============================================================
# CALLBACK: AI
# ============================================================

@router.callback_query(
    F.data == "menu_ai"
)
async def menu_ai(callback):

    await callback.message.edit_text(

        "🧠 ابزارهای هوش مصنوعی:",

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

    await callback.message.answer(

        "⚙️ تنظیمات\n\n"

        f"📚 آرشیو: "
        f"{news_count}/{MAX_NEWS}\n\n"

        f"📊 آستانه شباهت: "
        f"{SETTINGS.get('similarity_threshold', 0.72)}\n\n"

        f"🧠 هوش مصنوعی: "
        f"{'فعال' if ai else 'غیرفعال'}\n\n"

        f"💾 مسیر: "
        f"{DATA_DIR}"

    )

    await callback.answer()


# ============================================================
# AI CALLBACK
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
# PENDING SAVE
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

    item_id = next_news_id(
        news
    )

    analysis = (
        pending.get(
            "analysis"
        )
        or {}
    )

    item = {

        "id": item_id,

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
        f"{len(news)}/{MAX_NEWS}"

    )

    await callback.answer()


# ============================================================
# PENDING AI
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

                "🚨 بررسی خبر\n\n"

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
            +
            item.get(
                "text",
                ""
            )

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
# GENERAL TEXT PROCESSING
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

    # --------------------------------------------------------
    # ADD ADMIN
    # --------------------------------------------------------

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
            "✅ ادمین اضافه شد."
        )

        return

    # --------------------------------------------------------
    # REMOVE ADMIN
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
    # COMMAND
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

                "🔴 این خبر قبلاً در آرشیو وجود دارد.\n\n"

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

        if embedding and item.get(
            "embedding"
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

    # --------------------------------------------------------
    # NEW
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

    if os.getenv(
        "ANALYZE_CHANNEL_POSTS",
        "false"
    ).lower() == "true":

        analysis = await analyze_news(
            text
        )

    item = {

        "id":
            next_news_id(
                news
            ),

        "title": (
            analysis.get(
                "title"
            )
            if analysis
            else
            text.splitlines()[0][:300]
        ),

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
        allowed_updates=
            dp.resolve_used_update_types()
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
