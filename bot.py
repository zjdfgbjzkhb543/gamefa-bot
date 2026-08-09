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

from pydantic import BaseModel
from openai import OpenAI

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

MAX_ARCHIVE = int(
    os.getenv("MAX_ARCHIVE", "100")
)

DB_PATH = os.getenv(
    "DB_PATH",
    "gamefa_archive.db"
)

# مدلی که برای داوری Duplicate استفاده می‌شود
AI_MODEL = os.getenv(
    "AI_MODEL",
    "gpt-5.5"
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(
    "GAMEFA_AI_DUPLICATE"
)


# =========================================================
# OPENAI
# =========================================================

ai = None

if OPENAI_API_KEY:

    ai = OpenAI(
        api_key=OPENAI_API_KEY
    )


# =========================================================
# AI STRUCTURED OUTPUT
# =========================================================

class DuplicateDecision(BaseModel):

    decision: str

    confidence: float

    reason: str

    same_event: bool

    same_claim: bool


# =========================================================
# DATABASE
# =========================================================

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

    conn.execute("""
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
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_exact_hash
        ON messages(exact_hash)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_url
        ON messages(url)
    """)

    conn.commit()

    return conn


# =========================================================
# PERMISSION
# =========================================================

async def allowed(update):

    if ADMIN_ID == 0:
        return True

    user = update.effective_user

    return bool(
        user and
        user.id == ADMIN_ID
    )


# =========================================================
# TEXT NORMALIZATION
# =========================================================

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

    text = text.replace(
        "\u200c",
        " "
    )

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


# =========================================================
# HASH
# =========================================================

def exact_hash(text):

    return hashlib.sha256(
        normalize_text(
            text
        ).encode("utf-8")
    ).hexdigest()


# =========================================================
# URL
# =========================================================

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

            p = urlparse(url)

            if not p.netloc:
                continue

            scheme = p.scheme.lower()
            domain = p.netloc.lower()
            path = p.path.rstrip("/")

            # fragment را حذف می‌کنیم
            canonical = (
                scheme
                + "://"
                + domain
                + path
            )

            if p.query:

                canonical += (
                    "?"
                    + p.query
                )

            result.append(
                canonical
            )

        except Exception:
            pass

    return list(
        dict.fromkeys(result)
    )


def article_url(text):

    urls = extract_urls(text)

    if not urls:
        return ""

    # اولویت با لینک گیمفا
    for url in urls:

        if "gamefa.com" in url.lower():

            return url

    return urls[0]


# =========================================================
# TELEGRAM EXPORT
# =========================================================

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

        return "".join(result)

    if value is None:
        return ""

    return str(value)


def extract_title(text):

    if not text:
        return ""

    for line in text.splitlines():

        line = compact(line)

        if line:

            return line[:500]

    return ""


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
            extract_title(text),

        "url":
            article_url(text)
    }


# =========================================================
# CLASSIC SIMILARITY
# =========================================================

def token_set(text):

    return set(
        re.findall(
            r"[\w\u0600-\u06ff]+",
            compact(text).lower(),
            re.UNICODE
        )
    )


def jaccard(a, b):

    A = token_set(a)
    B = token_set(b)

    if not A or not B:
        return 0.0

    return len(A & B) / len(A | B)


def sequence_score(a, b):

    return SequenceMatcher(
        None,
        compact(a).lower(),
        compact(b).lower()
    ).ratio()


def classic_similarity(new, old):

    a = new["text"]
    b = old["text"]

    if normalize_text(a) == normalize_text(b):

        return 1.0

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


# =========================================================
# AI JUDGE
# =========================================================

def ai_compare(
    new_message,
    old_message
):

    if ai is None:

        logger.warning(
            "OPENAI_API_KEY is not configured."
        )

        return {
            "decision": "UNCERTAIN",
            "confidence": 0,
            "reason": "AI unavailable",
            "same_event": False,
            "same_claim": False
        }


    new_text = new_message[
        "text"
    ]

    old_text = old_message[
        "text"
    ]


    prompt = f"""
دو خبر زیر را به عنوان یک ویراستار حرفه‌ای اخبار گیمینگ مقایسه کن.

هدف فقط این است که بفهمی آیا این دو خبر
درباره «همان رویداد خبری / همان ادعای اصلی» هستند یا نه.

قوانین بسیار مهم:

1. صرفاً یکسان بودن موضوع کافی نیست.
2. صرفاً یکسان بودن نام بازی، فیلم، شرکت یا شخص کافی نیست.
3. اگر هر دو خبر درباره یک بازی باشند ولی دو اتفاق متفاوت را گزارش کنند،
   NEW محسوب می‌شوند.
4. اگر یکی خبر اعلام یک اتفاق و دیگری همان اتفاق با بیان متفاوت باشد،
   DUPLICATE محسوب می‌شوند.
5. تغییر جزئی در ترجمه، ساختار جمله، طول متن، تیتر یا لحن نباید مانع
   تشخیص Duplicate شود.
6. اگر درباره Duplicate بودن مطمئن نیستی، UNCERTAIN بده.
7. برای کاهش False Positive محافظه‌کار باش.
8. خبرهای مربوط به دو شایعه، دو مصاحبه یا دو رویداد متفاوت را یکی نکن.
9. تاریخ متفاوت به تنهایی باعث NEW شدن نمی‌شود؛ ممکن است یک خبر
   همان رویداد را بعداً با اطلاعات تکمیلی گزارش کند.
10. اما اگر ادعای اصلی تغییر کرده باشد، NEW است.

خبر اول:

عنوان:
{new_message["title"]}

متن:
{new_text}


خبر دوم:

عنوان:
{old_message["title"]}

متن:
{old_text}


فقط بر اساس معنای خبری تصمیم بگیر.
"""


    try:

        response = ai.responses.parse(
            model=AI_MODEL,

            input=[
                {
                    "role": "system",
                    "content": (
                        "You are an extremely conservative "
                        "news duplicate detection system."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],

            text_format=DuplicateDecision
        )


        # پیدا کردن structured output
        for output in response.output:

            if output.type != "message":
                continue

            for item in output.content:

                if (
                    item.type
                    !=
                    "output_text"
                ):
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

                    return {

                        "decision":
                            decision,

                        "confidence":
                            max(
                                0,
                                min(
                                    1,
                                    float(
                                        result.confidence
                                    )
                                )
                            ),

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
            "OpenAI comparison failed: %s",
            error
        )


    return {

        "decision":
            "UNCERTAIN",

        "confidence":
            0,

        "reason":
            "AI comparison failed",

        "same_event":
            False,

        "same_claim":
            False
    }


# =========================================================
# FIND DUPLICATE
# =========================================================

def find_duplicate(
    conn,
    message
):

    # -----------------------------------------------------
    # 1. EXACT TEXT
    # -----------------------------------------------------

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

        return True, "EXACT_TEXT"


    # -----------------------------------------------------
    # 2. SAME ARTICLE URL
    # -----------------------------------------------------

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

            return True, "SAME_URL"


    # -----------------------------------------------------
    # 3. FIND CANDIDATES
    # -----------------------------------------------------

    rows = conn.execute(
        """
        SELECT *
        FROM messages
        """
    ).fetchall()


    candidates = []


    for row in rows:

        old = dict(row)

        score = classic_similarity(
            message,
            old
        )

        candidates.append(
            (
                score,
                old
            )
        )


    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )


    # -----------------------------------------------------
    # 4. CLEARLY DIFFERENT
    # -----------------------------------------------------

    # فقط اگر شباهت واقعاً پایین باشد
    # AI را بی‌دلیل صدا نمی‌زنیم.

    if (
        not candidates
        or
        candidates[0][0] < 0.35
    ):

        return False, "NEW"


    # -----------------------------------------------------
    # 5. AI ANALYSIS
    # -----------------------------------------------------

    # حداکثر چند کاندیدای نزدیک
    # تا مصرف API کنترل شود.

    top_candidates = candidates[:5]


    for score, old in top_candidates:

        # متن‌های خیلی کوتاه را
        # به AI نمی‌فرستیم مگر اینکه
        # عنوان بسیار مشابه باشد.

        title_score = sequence_score(
            message["title"],
            old["title"]
        )


        should_ai = (

            score >= 0.45

            or

            title_score >= 0.75

            or

            message["url"]
            and
            old["url"]
            and
            message["url"]
            == old["url"]
        )


        if not should_ai:

            continue


        result = ai_compare(
            message,
            old
        )


        logger.info(
            "AI: %s | confidence=%s | reason=%s",
            result["decision"],
            result["confidence"],
            result["reason"]
        )


        # -------------------------------------------------
        # AI DUPLICATE
        # -------------------------------------------------

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

            return True, "AI_DUPLICATE"


        # -------------------------------------------------
        # AI UNCERTAIN
        # -------------------------------------------------

        # عمداً چیزی حذف نمی‌کنیم.

        if (
            result["decision"]
            ==
            "UNCERTAIN"
        ):

            continue


    # -----------------------------------------------------
    # NO CONFIDENT DUPLICATE
    # -----------------------------------------------------

    return False, "NEW"


# =========================================================
# IMPORT
# =========================================================

def import_messages(
    messages
):

    conn = get_db()

    added = 0
    duplicates = 0
    skipped = 0
    ai_checks = 0


    for raw in messages:

        try:

            message = parse_message(
                raw
            )


            if not message[
                "text"
            ]:

                skipped += 1

                continue


            duplicate, reason = (
                find_duplicate(
                    conn,
                    message
                )
            )


            if duplicate:

                logger.info(
                    "DUPLICATE: %s",
                    reason
                )

                duplicates += 1

                continue


            fingerprint = exact_hash(
                message[
                    "text"
                ]
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

                    message[
                        "source_id"
                    ],

                    message[
                        "date"
                    ],

                    normalize_text(
                        message[
                            "text"
                        ]
                    ),

                    message[
                        "text"
                    ],

                    message[
                        "title"
                    ],

                    message[
                        "url"
                    ],

                    fingerprint
                )
            )


            added += 1


        except sqlite3.IntegrityError:

            duplicates += 1


        except Exception as error:

            logger.exception(
                "Import error: %s",
                error
            )

            skipped += 1


    # =====================================================
    # KEEP ONLY LAST 100
    # =====================================================

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


    if len(rows) > MAX_ARCHIVE:

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


    conn.commit()

    conn.close()


    return (
        added,
        duplicates,
        skipped
    )


# =========================================================
# LOAD EXPORT
# =========================================================

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


def load_zip(path):

    with zipfile.ZipFile(
        path,
        "r"
    ) as archive:

        names = archive.namelist()


        candidates = [
            x
            for x in names
            if x.lower().endswith(
                "result.json"
            )
        ]


        if not candidates:

            candidates = [
                x
                for x in names
                if x.lower().endswith(
                    ".json"
                )
            ]


        if not candidates:

            raise ValueError(
                "فایل JSON داخل ZIP پیدا نشد."
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
        "فقط ZIP یا JSON."
    )


# =========================================================
# START
# =========================================================

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
🤖 موتور AI تشخیص اخبار تکراری گیمفا

تمرکز اصلی ربات:
تشخیص اینکه دو خبر واقعاً درباره
یک اتفاق خبری هستند یا نه.

روش:

✓ متن دقیق
✓ SHA-256
✓ لینک مقاله
✓ شباهت متنی
✓ شباهت تیتر
✓ تحلیل معنایی با هوش مصنوعی
✓ تشخیص رویداد اصلی
✓ تشخیص ادعای اصلی

اگر AI مطمئن نباشد،
خبر تکراری حذف نمی‌شود.
"""
    )


# =========================================================
# STATS
# =========================================================

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
📊 آمار آرشیو

📦 پیام‌ها:
{total}

🎯 سقف:
{MAX_ARCHIVE}

🤖 AI:
{"فعال" if ai else "غیرفعال"}
"""
    )


# =========================================================
# CLEAR
# =========================================================

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
        "🗑 آرشیو پاک شد."
    )


# =========================================================
# RECEIVE EXPORT
# =========================================================

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
                "🔍 در حال خواندن Export..."
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
                f"🧠 {len(messages):,} پیام پیدا شد.\n"
                "در حال تحلیل Duplicate..."
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
✅ تحلیل تمام شد.

📥 پیام‌های ورودی:
{len(messages):,}

🆕 خبر جدید:
{added:,}

♻️ خبر تکراری:
{duplicates:,}

⚠️ رد شده:
{skipped:,}

📦 آرشیو فعلی:
{total:,} / {MAX_ARCHIVE}

🤖 تحلیل معنایی:
{"فعال" if ai else "غیرفعال"}
"""
        )


    except zipfile.BadZipFile:

        await status.edit_text(
            "❌ ZIP خراب است."
        )


    except json.JSONDecodeError:

        await status.edit_text(
            "❌ JSON نامعتبر است."
        )


    except Exception as error:

        logger.exception(
            "File processing error"
        )

        await status.edit_text(
            f"❌ خطا:\n{error}"
        )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است."
        )


    if not OPENAI_API_KEY:

        logger.warning(
            "OPENAI_API_KEY تنظیم نشده؛ "
            "AI Duplicate فعال نخواهد بود."
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


    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            receive_file
        )
    )


    logger.info(
        "GAMEFA AI DUPLICATE ENGINE STARTED"
    )


    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":

    main()
