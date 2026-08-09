import os
import re
import json
import zipfile
import hashlib
import sqlite3
import logging
import tempfile
import unicodedata

from difflib import SequenceMatcher
from urllib.parse import urlparse

from openai import OpenAI
from pydantic import BaseModel

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# تنظیمات
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# اگر 0 باشد همه می‌توانند از ربات استفاده کنند
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# تعداد اخبار ذخیره‌شده
MAX_ARCHIVE = int(os.getenv("MAX_ARCHIVE", "100"))

# دیتابیس
DB_PATH = os.getenv(
    "DB_PATH",
    "gamefa_archive.db"
)

# مدل AI
AI_MODEL = os.getenv(
    "AI_MODEL",
    "gpt-5.5"
)


# ============================================================
# لاگ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(
    "GAMEFA_DUPLICATE_AI"
)


# ============================================================
# OpenAI
# ============================================================

client = None

if OPENAI_API_KEY:

    client = OpenAI(
        api_key=OPENAI_API_KEY
    )


# ============================================================
# خروجی ساختاریافته AI
# ============================================================

class DuplicateDecision(BaseModel):

    decision: str

    confidence: float

    reason: str

    same_event: bool

    same_claim: bool


# ============================================================
# DATABASE
# ============================================================

def get_db():

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

    conn.execute(
        "PRAGMA synchronous=NORMAL"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            source_id TEXT,

            date TEXT,

            text TEXT NOT NULL,

            raw_text TEXT NOT NULL,

            title TEXT,

            url TEXT,

            exact_hash TEXT UNIQUE,

            imported_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_exact_hash
        ON messages(exact_hash)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_url
        ON messages(url)
        """
    )

    conn.commit()

    return conn


# ============================================================
# دسترسی
# ============================================================

async def allowed(update):

    if ADMIN_ID == 0:
        return True

    user = update.effective_user

    if not user:
        return False

    return user.id == ADMIN_ID


# ============================================================
# NORMALIZE
# ============================================================

def normalize_text(text):

    if text is None:
        return ""

    text = str(text)

    text = unicodedata.normalize(
        "NFC",
        text
    )

    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    # نیم‌فاصله
    text = text.replace(
        "\u200c",
        " "
    )

    # حروف عربی → فارسی
    replacements = {

        "ي": "ی",
        "ى": "ی",

        "ك": "ک",

        "ة": "ه",
        "ۀ": "ه",

        "ؤ": "و",

        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا"
    }

    for old, new in replacements.items():

        text = text.replace(
            old,
            new
        )

    text = re.sub(
        r"[ \t\f\v]+",
        " ",
        text
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


def compact(text):

    return re.sub(
        r"\s+",
        " ",
        normalize_text(text)
    ).strip()


# ============================================================
# SHA256
# ============================================================

def exact_hash(text):

    return hashlib.sha256(
        normalize_text(
            text
        ).encode("utf-8")
    ).hexdigest()


# ============================================================
# URL
# ============================================================

URL_RE = re.compile(
    r"https?://[^\s<>\]\)\"']+",
    re.IGNORECASE
)


def extract_urls(text):

    result = []

    for url in URL_RE.findall(
        text or ""
    ):

        url = url.rstrip(
            ".,؛،!?؟"
        )

        try:

            parsed = urlparse(
                url
            )

            if not parsed.netloc:
                continue

            scheme = (
                parsed.scheme.lower()
            )

            domain = (
                parsed.netloc.lower()
            )

            path = (
                parsed.path.rstrip("/")
            )

            canonical = (
                scheme
                + "://"
                + domain
                + path
            )

            if parsed.query:

                canonical += (
                    "?"
                    + parsed.query
                )

            result.append(
                canonical
            )

        except Exception:

            continue

    return list(
        dict.fromkeys(result)
    )


def article_url(text):

    urls = extract_urls(
        text
    )

    if not urls:
        return ""

    # اولویت لینک Gamefa
    for url in urls:

        if "gamefa.com" in url.lower():

            return url

    return urls[0]


# ============================================================
# TELEGRAM EXPORT TEXT
# ============================================================

def extract_text(value):

    if isinstance(
        value,
        str
    ):

        return value

    if isinstance(
        value,
        list
    ):

        result = []

        for item in value:

            if isinstance(
                item,
                dict
            ):

                result.append(
                    str(
                        item.get(
                            "text",
                            ""
                        )
                    )
                )

            else:

                result.append(
                    str(item)
                )

        return "".join(
            result
        )

    if value is None:

        return ""

    return str(value)


# ============================================================
# TITLE
# ============================================================

def extract_title(text):

    if not text:
        return ""

    lines = []

    for line in text.splitlines():

        line = compact(
            line
        )

        if line:

            lines.append(
                line
            )

    if not lines:

        return ""

    return lines[0][:500]


# ============================================================
# PARSE MESSAGE
# ============================================================

def parse_message(raw):

    text = extract_text(
        raw.get(
            "text",
            ""
        )
    ).strip()

    return {

        "source_id":
            str(
                raw.get(
                    "id",
                    ""
                )
            ),

        "date":
            str(
                raw.get(
                    "date",
                    ""
                )
            ),

        "text":
            text,

        "title":
            extract_title(
                text
            ),

        "url":
            article_url(
                text
            )
    }


# ============================================================
# TOKENIZATION
# ============================================================

def tokens(text):

    return set(
        re.findall(
            r"[\w\u0600-\u06ff]+",
            compact(text).lower(),
            re.UNICODE
        )
    )


# ============================================================
# JACCARD
# ============================================================

def jaccard(a, b):

    A = tokens(a)
    B = tokens(b)

    if not A or not B:

        return 0.0

    return len(
        A & B
    ) / len(
        A | B
    )


# ============================================================
# SEQUENCE
# ============================================================

def sequence_score(a, b):

    return SequenceMatcher(
        None,
        compact(a).lower(),
        compact(b).lower()
    ).ratio()


# ============================================================
# CLASSIC SIMILARITY
# ============================================================

def classic_similarity(
    new_message,
    old_message
):

    a = new_message["text"]
    b = old_message["text"]

    if normalize_text(a) == normalize_text(b):

        return 1.0

    # خبر کوتاه را با fuzzy تشخیص نمی‌دهیم
    if (
        len(compact(a)) < 120
        or
        len(compact(b)) < 120
    ):

        return 0.0

    seq = sequence_score(
        a,
        b
    )

    jac = jaccard(
        a,
        b
    )

    return (
        seq * 0.70
        +
        jac * 0.30
    )


# ============================================================
# AI DUPLICATE ANALYSIS
# ============================================================

def ai_compare(
    new_message,
    old_message
):

    if client is None:

        logger.warning(
            "OPENAI_API_KEY تنظیم نشده."
        )

        return {

            "decision":
                "UNCERTAIN",

            "confidence":
                0.0,

            "reason":
                "AI unavailable",

            "same_event":
                False,

            "same_claim":
                False
        }


    new_title = new_message[
        "title"
    ]

    old_title = old_message[
        "title"
    ]

    new_text = new_message[
        "text"
    ]

    old_text = old_message[
        "text"
    ]


    prompt = f"""
دو خبر زیر را با دقت بسیار زیاد مقایسه کن.

هدف:
تشخیص بده آیا این دو خبر درباره
«همان اتفاق خبری و همان ادعای اصلی»
هستند یا خیر.

قوانین بسیار مهم:

- فقط شباهت موضوع کافی نیست.
- فقط نام یکسان بازی، فیلم، شرکت یا شخص کافی نیست.
- اگر دو خبر درباره یک بازی هستند اما دو اتفاق متفاوت را گزارش می‌کنند،
  نتیجه NEW است.
- اگر متن‌ها متفاوت باشند ولی هر دو دقیقاً همان اتفاق را گزارش کنند،
  DUPLICATE است.
- ترجمه متفاوت، بازنویسی، کوتاه‌تر یا طولانی‌تر بودن متن می‌تواند همچنان
  Duplicate باشد.
- اگر خبر دوم اطلاعات جدیدی درباره یک اتفاق قبلی دارد، با دقت بررسی کن
  که آیا واقعاً همان خبر است یا یک رویداد/ادعای جدید.
- شایعه و خبر رسمی را بدون دلیل یکی نکن.
- دو مصاحبه متفاوت را یکی نکن.
- دو گزارش متفاوت درباره یک شخصیت را یکی نکن.
- دو خبر با موضوع مشترک ولی ادعای متفاوت را یکی نکن.
- اگر مطمئن نیستی، UNCERTAIN بده.
- برای جلوگیری از False Positive محافظه‌کار باش.

خبر اول:

عنوان:
{new_title}

متن:
{new_text}


خبر دوم:

عنوان:
{old_title}

متن:
{old_text}


فیلدها را دقیق تعیین کن:

DUPLICATE:
همان اتفاق خبری و همان ادعای اصلی.

NEW:
اتفاق یا ادعای اصلی متفاوت.

UNCERTAIN:
اطلاعات کافی برای تصمیم قطعی وجود ندارد.
"""


    try:

        response = client.responses.parse(

            model=AI_MODEL,

            input=[

                {
                    "role":
                        "system",

                    "content":
                        (
                            "You are a highly "
                            "conservative news "
                            "duplicate detector."
                        )
                },

                {
                    "role":
                        "user",

                    "content":
                        prompt
                }
            ],

            text_format=DuplicateDecision
        )


        for output in response.output:

            if output.type != "message":
                continue

            for item in output.content:

                if item.type != "output_text":
                    continue

                if item.parsed:

                    result = item.parsed

                    decision = (
                        result.decision
                        .upper()
                        .strip()
                    )

                    if decision not in (
                        "DUPLICATE",
                        "NEW",
                        "UNCERTAIN"
                    ):

                        decision = "UNCERTAIN"


                    confidence = float(
                        result.confidence
                    )


                    confidence = max(
                        0.0,
                        min(
                            1.0,
                            confidence
                        )
                    )


                    return {

                        "decision":
                            decision,

                        "confidence":
                            confidence,

                        "reason":
                            result.reason,

                        "same_event":
                            bool(
                                result.same_event
                            ),

                        "same_claim":
                            bool(
                                result.same_claim
                            )
                    }


    except Exception as error:

        logger.exception(
            "AI ERROR: %s",
            error
        )


    return {

        "decision":
            "UNCERTAIN",

        "confidence":
            0.0,

        "reason":
            "AI comparison failed",

        "same_event":
            False,

        "same_claim":
            False
    }


# ============================================================
# FIND DUPLICATE
# ============================================================

def find_duplicate(
    conn,
    message
):

    # --------------------------------------------------------
    # 1. EXACT TEXT
    # --------------------------------------------------------

    fingerprint = exact_hash(
        message["text"]
    )

    row = conn.execute(
        """
        SELECT *
        FROM messages
        WHERE exact_hash = ?
        LIMIT 1
        """,
        (
            fingerprint,
        )
    ).fetchone()


    if row:

        return (
            True,
            "EXACT_TEXT"
        )


    # --------------------------------------------------------
    # 2. SAME URL
    # --------------------------------------------------------

    if message["url"]:

        row = conn.execute(
            """
            SELECT *
            FROM messages
            WHERE url = ?
            LIMIT 1
            """,
            (
                message["url"],
            )
        ).fetchone()


        if row:

            return (
                True,
                "SAME_URL"
            )


    # --------------------------------------------------------
    # 3. GET CANDIDATES
    # --------------------------------------------------------

    rows = conn.execute(
        """
        SELECT *
        FROM messages
        """
    ).fetchall()


    candidates = []


    for row in rows:

        old = dict(
            row
        )

        score = classic_similarity(
            message,
            old
        )

        title_score = sequence_score(
            message["title"],
            old["title"]
        )


        # امتیاز ترکیبی برای انتخاب کاندید
        combined = (
            score * 0.75
            +
            title_score * 0.25
        )


        candidates.append(
            (
                combined,
                old
            )
        )


    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )


    if not candidates:

        return (
            False,
            "NEW"
        )


    # --------------------------------------------------------
    # 4. موارد کاملاً متفاوت
    # --------------------------------------------------------

    if candidates[0][0] < 0.30:

        return (
            False,
            "NEW"
        )


    # --------------------------------------------------------
    # 5. AI
    # --------------------------------------------------------

    # فقط چند کاندید نزدیک
    for score, old in candidates[:5]:

        title_score = sequence_score(
            message["title"],
            old["title"]
        )


        # فقط مواردی که احتمال ارتباط دارند
        if (
            score < 0.35
            and
            title_score < 0.60
        ):

            continue


        result = ai_compare(
            message,
            old
        )


        logger.info(
            "AI RESULT = %s | confidence=%s | event=%s | claim=%s",
            result["decision"],
            result["confidence"],
            result["same_event"],
            result["same_claim"]
        )


        # ----------------------------------------------------
        # DUPLICATE
        # ----------------------------------------------------

        if (

            result["decision"]
            ==
            "DUPLICATE"

            and

            result["confidence"]
            >=
            0.90

            and

            result["same_event"]

            and

            result["same_claim"]
        ):

            return (
                True,
                "AI_DUPLICATE"
            )


        # ----------------------------------------------------
        # NEW
        # ----------------------------------------------------

        if (

            result["decision"]
            ==
            "NEW"

            and

            result["confidence"]
            >=
            0.90
        ):

            # این کاندید متفاوت است،
            # اما ممکن است کاندید دیگری Duplicate باشد.
            continue


        # ----------------------------------------------------
        # UNCERTAIN
        # ----------------------------------------------------

        continue


    return (
        False,
        "NEW"
    )


# ============================================================
# SAVE MESSAGE
# ============================================================

def save_message(
    conn,
    message
):

    fingerprint = exact_hash(
        message["text"]
    )


    conn.execute(
        """
        INSERT INTO messages
        (
            source_id,
            date,
            text,
            raw_text,
            title,
            url,
            exact_hash,
            imported_at
        )
        VALUES
        (
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            datetime('now')
        )
        """,
        (
            message["source_id"],
            message["date"],
            normalize_text(
                message["text"]
            ),
            message["text"],
            message["title"],
            message["url"],
            fingerprint
        )
    )


# ============================================================
# LIMIT ARCHIVE
# ============================================================

def limit_archive(
    conn
):

    rows = conn.execute(
        """
        SELECT id
        FROM messages

        ORDER BY
            CASE
                WHEN date = ''
                THEN imported_at
                ELSE date
            END DESC,

            id DESC
        """
    ).fetchall()


    if len(rows) <= MAX_ARCHIVE:
        return


    for row in rows[
        MAX_ARCHIVE:
    ]:

        conn.execute(
            """
            DELETE FROM messages
            WHERE id = ?
            """,
            (
                row["id"],
            )
        )


# ============================================================
# PROCESS SINGLE NEWS
# ============================================================

def process_news(
    message
):

    conn = get_db()


    try:

        duplicate, reason = (
            find_duplicate(
                conn,
                message
            )
        )


        if duplicate:

            return (
                True,
                reason,
                None
            )


        try:

            save_message(
                conn,
                message
            )

        except sqlite3.IntegrityError:

            return (
                True,
                "EXACT_TEXT",
                None
            )


        limit_archive(
            conn
        )


        conn.commit()


        total = conn.execute(
            """
            SELECT COUNT(*)
            FROM messages
            """
        ).fetchone()[0]


        return (
            False,
            "NEW",
            total
        )


    finally:

        conn.close()


# ============================================================
# IMPORT EXPORT
# ============================================================

def import_messages(
    messages
):

    added = 0
    duplicates = 0
    skipped = 0


    for raw in messages:

        try:

            message = parse_message(
                raw
            )


            if not message["text"]:

                skipped += 1

                continue


            duplicate, reason, total = (
                process_news(
                    message
                )
            )


            if duplicate:

                duplicates += 1

            else:

                added += 1


        except Exception as error:

            logger.exception(
                "IMPORT ERROR: %s",
                error
            )

            skipped += 1


    return (
        added,
        duplicates,
        skipped
    )


# ============================================================
# LOAD JSON
# ============================================================

def load_json(path):

    with open(
        path,
        "r",
        encoding="utf-8-sig"
    ) as f:

        data = json.load(f)


    if isinstance(
        data,
        dict
    ):

        return data.get(
            "messages",
            []
        )


    return data


# ============================================================
# LOAD ZIP
# ============================================================

def load_zip(path):

    with zipfile.ZipFile(
        path,
        "r"
    ) as archive:

        names = archive.namelist()


        candidates = [
            name
            for name in names
            if name.lower().endswith(
                "result.json"
            )
        ]


        if not candidates:

            candidates = [
                name
                for name in names
                if name.lower().endswith(
                    ".json"
                )
            ]


        if not candidates:

            raise ValueError(
                "result.json پیدا نشد."
            )


        with archive.open(
            candidates[0]
        ) as f:

            data = json.loads(
                f.read().decode(
                    "utf-8-sig"
                )
            )


    if isinstance(
        data,
        dict
    ):

        return data.get(
            "messages",
            []
        )


    return data


# ============================================================
# LOAD EXPORT
# ============================================================

def load_export(path):

    if path.lower().endswith(
        ".json"
    ):

        return load_json(
            path
        )


    if path.lower().endswith(
        ".zip"
    ):

        return load_zip(
            path
        )


    raise ValueError(
        "فقط ZIP یا JSON مجاز است."
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await allowed(
        update
    ):
        return


    await update.message.reply_text(
        """
🤖 موتور هوشمند تشخیص اخبار تکراری گیمفا

تمرکز اصلی ربات:

تشخیص اینکه آیا دو خبر واقعاً
درباره یک اتفاق خبری هستند یا نه.

روش بررسی:

✓ مقایسه متن
✓ SHA-256
✓ بررسی لینک
✓ مقایسه تیتر
✓ تحلیل شباهت
✓ تحلیل معنایی با AI
✓ تشخیص رویداد اصلی
✓ تشخیص ادعای اصلی

🛡 اگر AI مطمئن نباشد،
خبر تکراری محسوب نمی‌شود.

📌 می‌توانی:
• متن خبر را مستقیم بفرستی
• ZIP خروجی Telegram Desktop بفرستی
• JSON خروجی Telegram Desktop بفرستی

برای آمار:
/stats

برای پاک کردن آرشیو:
/clear
"""
    )


# ============================================================
# STATS
# ============================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await allowed(
        update
    ):
        return


    conn = get_db()


    total = conn.execute(
        """
        SELECT COUNT(*)
        FROM messages
        """
    ).fetchone()[0]


    conn.close()


    await update.message.reply_text(
        f"""
📊 آمار موتور

📦 اخبار آرشیو:
{total}

🎯 حداکثر:
{MAX_ARCHIVE}

🤖 هوش مصنوعی:
{"فعال ✅" if client else "غیرفعال ❌"}
"""
    )


# ============================================================
# CLEAR
# ============================================================

async def clear(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await allowed(
        update
    ):
        return


    conn = get_db()

    conn.execute(
        "DELETE FROM messages"
    )

    conn.commit()

    conn.close()


    await update.message.reply_text(
        "🗑 آرشیو اخبار پاک شد."
    )


# ============================================================
# DIRECT TEXT NEWS
# ============================================================

async def receive_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await allowed(
        update
    ):
        return


    if not update.message:
        return


    text = (
        update.message.text
        or ""
    ).strip()


    if not text:
        return


    # دستورات قبلاً توسط CommandHandler
    # پردازش می‌شوند
    if text.startswith("/"):
        return


    status = await update.message.reply_text(
        "🧠 در حال تحلیل خبر..."
    )


    try:

        message = parse_message(
            {
                "id":
                    update.message.message_id,

                "date":
                    (
                        update.message.date.isoformat()
                        if update.message.date
                        else ""
                    ),

                "text":
                    text
            }
        )


        duplicate, reason, total = (
            process_news(
                message
            )
        )


        if duplicate:

            await status.edit_text(
                "♻️ خبر تکراری تشخیص داده شد.\n\n"
                f"🔎 روش تشخیص: {reason}\n\n"
                "⛔ به آرشیو اضافه نشد."
            )

            return


        await status.edit_text(
            "🆕 خبر جدید تشخیص داده شد.\n\n"
            "✅ به آرشیو اضافه شد.\n\n"
            f"📦 آرشیو: {total}/{MAX_ARCHIVE}"
        )


    except Exception as error:

        logger.exception(
            "DIRECT TEXT ERROR"
        )


        await status.edit_text(
            "❌ خطا هنگام تحلیل خبر:\n\n"
            f"{error}"
        )


# ============================================================
# ZIP / JSON
# ============================================================

async def receive_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await allowed(
        update
    ):
        return


    document = (
        update.message.document
    )


    if not document:
        return


    filename = (
        document.file_name
        or "export"
    )


    if not filename.lower().endswith(
        (
            ".zip",
            ".json"
        )
    ):

        await update.message.reply_text(
            "❌ فقط ZIP یا JSON ارسال کن."
        )

        return


    status = await update.message.reply_text(
        "⏳ فایل دریافت شد..."
    )


    try:

        tg_file = (
            await context.bot.get_file(
                document.file_id
            )
        )


        with tempfile.TemporaryDirectory() as temp:

            path = os.path.join(
                temp,
                filename
            )


            await tg_file.download_to_drive(
                path
            )


            await status.edit_text(
                "🔍 در حال خواندن آرشیو..."
            )


            messages = load_export(
                path
            )


            if not messages:

                await status.edit_text(
                    "⚠️ هیچ پیامی پیدا نشد."
                )

                return


            await status.edit_text(
                f"🧠 {len(messages):,} پیام پیدا شد.\n\n"
                "در حال بررسی Duplicate با AI..."
            )


            added, duplicates, skipped = (
                import_messages(
                    messages
                )
            )


        conn = get_db()


        total = conn.execute(
            """
            SELECT COUNT(*)
            FROM messages
            """
        ).fetchone()[0]


        conn.close()


        await status.edit_text(
            f"""
✅ بررسی کامل شد.

📥 پیام‌های ورودی:
{len(messages):,}

🆕 خبرهای جدید:
{added:,}

♻️ خبرهای تکراری:
{duplicates:,}

⚠️ رد شده:
{skipped:,}

📦 آرشیو فعلی:
{total:,}/{MAX_ARCHIVE}
"""
        )


    except zipfile.BadZipFile:

        await status.edit_text(
            "❌ فایل ZIP خراب است."
        )


    except json.JSONDecodeError:

        await status.edit_text(
            "❌ فایل JSON خراب است."
        )


    except Exception as error:

        logger.exception(
            "EXPORT ERROR"
        )


        await status.edit_text(
            f"❌ خطا:\n\n{error}"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است."
        )


    if not OPENAI_API_KEY:

        logger.warning(
            "OPENAI_API_KEY تنظیم نشده است."
        )


    get_db().close()


    application = (
        Application
        .builder()
        .token(
            BOT_TOKEN
        )
        .build()
    )


    # -----------------------------
    # Commands
    # -----------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    application.add_handler(
        CommandHandler(
            "stats",
            stats
        )
    )


    application.add_handler(
        CommandHandler(
            "clear",
            clear
        )
    )


    # -----------------------------
    # ZIP / JSON
    # -----------------------------

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            receive_file
        )
    )


    # -----------------------------
    # TEXT NEWS
    # -----------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            receive_text
        )
    )


    logger.info(
        "GAMEFA AI DUPLICATE BOT STARTED"
    )


    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
