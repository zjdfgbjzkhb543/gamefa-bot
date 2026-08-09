import os
import re
import json
import math
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
# CONFIG
# ============================================================

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

EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "text-embedding-3-small"
)

AI_MODEL = os.getenv(
    "AI_MODEL",
    "gpt-5.5"
)

AI_CANDIDATES = int(
    os.getenv("AI_CANDIDATES", "8")
)

SEMANTIC_THRESHOLD = float(
    os.getenv(
        "SEMANTIC_THRESHOLD",
        "0.35"
    )
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(
    "GAMEFA_DUPLICATE_AI"
)


# ============================================================
# OPENAI
# ============================================================

client = None

if OPENAI_API_KEY:

    client = OpenAI(
        api_key=OPENAI_API_KEY
    )


# ============================================================
# AI RESPONSE
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
        timeout=60
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

            embedding TEXT,

            imported_at TEXT
        )
        """
    )

    # سازگاری با دیتابیس قدیمی

    columns = conn.execute(
        "PRAGMA table_info(messages)"
    ).fetchall()

    names = {
        row["name"]
        for row in columns
    }

    if "embedding" not in names:

        conn.execute(
            """
            ALTER TABLE messages
            ADD COLUMN embedding TEXT
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
# ACCESS
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

    for line in text.splitlines():

        line = compact(
            line
        )

        if line:

            return line[:500]

    return ""


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
# TEXT SIMILARITY
# ============================================================

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

    return len(
        A & B
    ) / len(
        A | B
    )


def sequence_score(a, b):

    return SequenceMatcher(
        None,
        compact(a).lower(),
        compact(b).lower()
    ).ratio()


def lexical_similarity(a, b):

    return (
        sequence_score(a, b) * 0.65
        +
        jaccard(a, b) * 0.35
    )


# ============================================================
# EMBEDDING INPUT
# ============================================================

def embedding_text(message):

    title = compact(
        message.get(
            "title",
            ""
        )
    )

    text = compact(
        message.get(
            "text",
            ""
        )
    )

    if title and text.startswith(title):

        return text[:12000]

    return (
        title
        + "\n\n"
        + text
    )[:12000]


# ============================================================
# CREATE EMBEDDING
# ============================================================

def create_embedding(message):

    if client is None:

        return None

    content = embedding_text(
        message
    )

    if not content:

        return None

    try:

        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=content
        )

        return response.data[0].embedding

    except Exception as error:

        logger.exception(
            "Embedding error: %s",
            error
        )

        return None


# ============================================================
# COSINE
# ============================================================

def cosine_similarity(a, b):

    if not a or not b:

        return 0.0

    if len(a) != len(b):

        return 0.0

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0

    for x, y in zip(a, b):

        dot += x * y

        norm_a += x * x
        norm_b += y * y

    if norm_a == 0 or norm_b == 0:

        return 0.0

    return (
        dot
        /
        (
            math.sqrt(norm_a)
            *
            math.sqrt(norm_b)
        )
    )


# ============================================================
# GET OLD EMBEDDING
# ============================================================

def get_old_embedding(
    conn,
    row
):

    if row["embedding"]:

        try:

            return json.loads(
                row["embedding"]
            )

        except Exception:

            pass

    old = dict(row)

    embedding = create_embedding(
        old
    )

    if embedding:

        conn.execute(
            """
            UPDATE messages
            SET embedding = ?
            WHERE id = ?
            """,
            (
                json.dumps(
                    embedding
                ),
                row["id"]
            )
        )

        conn.commit()

    return embedding


# ============================================================
# AI JUDGE
# ============================================================

def ai_compare(
    new_message,
    old_message
):

    if client is None:

        return {

            "decision":
                "UNCERTAIN",

            "confidence":
                0.0,

            "reason":
                "OpenAI unavailable",

            "same_event":
                False,

            "same_claim":
                False
        }


    prompt = f"""
تو موتور تشخیص اخبار تکراری گیمفا هستی.

دو خبر زیر را مقایسه کن.

هدف اصلی:
تشخیص اینکه آیا این دو خبر واقعاً یک اتفاق خبری
و یک ادعای اصلی را گزارش می‌کنند یا خیر.

DUPLICATE یعنی:
دو خبر درباره همان اتفاق و همان ادعای اصلی باشند،
حتی اگر:

- یک کلمه حذف شده باشد
- چند کلمه اضافه شده باشد
- جمله‌ای بازنویسی شده باشد
- ترتیب جمله‌ها تغییر کرده باشد
- تیتر متفاوت باشد
- متن کوتاه‌تر یا طولانی‌تر باشد
- از مترادف استفاده شده باشد
- ترجمه متفاوت باشد

NEW یعنی:
اتفاق خبری یا ادعای اصلی متفاوت باشد.

قوانین:

1. فقط شباهت موضوع کافی نیست.

2. یکسان بودن نام بازی کافی نیست.

3. یکسان بودن نام شخصیت کافی نیست.

4. یکسان بودن شرکت کافی نیست.

5. دو خبر متفاوت درباره یک بازی را Duplicate نکن.

6. اگر خبر دوم همان اتفاق قبلی را با چند جزئیات اضافه گزارش کند،
Duplicate است.

7. اگر فقط جمله‌بندی تغییر کرده ولی معنی اصلی همان است،
Duplicate است.

8. اگر یک کلمه حذف یا اضافه شده ولی اتفاق همان است،
Duplicate است.

9. اگر دو خبر درباره دو اتفاق مختلف باشند،
حتی با شباهت زیاد، NEW است.

10. اگر مطمئن نیستی، UNCERTAIN بده.

خبر جدید:

عنوان:
{new_message["title"]}

متن:
{new_message["text"]}


خبر موجود:

عنوان:
{old_message["title"]}

متن:
{old_message["text"]}


نتیجه:

decision:
DUPLICATE / NEW / UNCERTAIN

confidence:
عدد بین 0 تا 1

reason:
دلیل کوتاه

same_event:
true / false

same_claim:
true / false
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
                            "You are an expert "
                            "Persian gaming news "
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

                if not item.parsed:

                    continue

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


                confidence = max(
                    0.0,
                    min(
                        1.0,
                        float(
                            result.confidence
                        )
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
            "AI judge error: %s",
            error
        )


    return {

        "decision":
            "UNCERTAIN",

        "confidence":
            0.0,

        "reason":
            "AI error",

        "same_event":
            False,

        "same_claim":
            False
    }


# ============================================================
# FIND CANDIDATES
# ============================================================

def find_candidates(
    conn,
    message,
    new_embedding
):

    rows = conn.execute(
        """
        SELECT *
        FROM messages
        ORDER BY id DESC
        """
    ).fetchall()


    candidates = []


    for row in rows:

        old = dict(row)

        old_embedding = get_old_embedding(
            conn,
            row
        )


        semantic = cosine_similarity(
            new_embedding,
            old_embedding
        )


        lexical = lexical_similarity(
            message["text"],
            old["text"]
        )


        title_score = sequence_score(
            message["title"],
            old["title"]
        )


        combined = (
            semantic * 0.70
            +
            lexical * 0.20
            +
            title_score * 0.10
        )


        # مهم:
        # برای حذف یک کلمه یا بازنویسی،
        # فقط Semantic را ملاک قرار نمی‌دهیم.

        relevant = (

            semantic >= SEMANTIC_THRESHOLD

            or

            lexical >= 0.45

            or

            title_score >= 0.70
        )


        if relevant:

            candidates.append(
                {
                    "row": old,
                    "semantic": semantic,
                    "lexical": lexical,
                    "title": title_score,
                    "combined": combined
                }
            )


    candidates.sort(
        key=lambda x: x["combined"],
        reverse=True
    )


    return candidates


# ============================================================
# DUPLICATE DETECTOR
# ============================================================

def find_duplicate(
    conn,
    message
):

    # --------------------------------------------------------
    # EXACT TEXT
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
            "EXACT_TEXT",
            1.0
        )


    # --------------------------------------------------------
    # SAME URL
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
                "SAME_URL",
                1.0
            )


    # --------------------------------------------------------
    # EMBEDDING
    # --------------------------------------------------------

    new_embedding = create_embedding(
        message
    )


    if not new_embedding:

        return (
            False,
            "AI_UNAVAILABLE",
            0.0
        )


    # --------------------------------------------------------
    # CANDIDATES
    # --------------------------------------------------------

    candidates = find_candidates(
        conn,
        message,
        new_embedding
    )


    if not candidates:

        return (
            False,
            "NEW",
            0.0
        )


    # --------------------------------------------------------
    # AI
    # --------------------------------------------------------

    for candidate in candidates[
        :AI_CANDIDATES
    ]:

        old = candidate["row"]


        logger.info(
            "Candidate semantic=%.4f lexical=%.4f title=%.4f",
            candidate["semantic"],
            candidate["lexical"],
            candidate["title"]
        )


        result = ai_compare(
            message,
            old
        )


        logger.info(
            "AI decision=%s confidence=%.3f event=%s claim=%s",
            result["decision"],
            result["confidence"],
            result["same_event"],
            result["same_claim"]
        )


        if (

            result["decision"]
            ==
            "DUPLICATE"

            and

            result["confidence"]
            >=
            0.85

            and

            result["same_event"]

            and

            result["same_claim"]
        ):

            return (
                True,
                "AI_SEMANTIC_DUPLICATE",
                result["confidence"]
            )


    return (
        False,
        "NEW",
        0.0
    )


# ============================================================
# SAVE
# ============================================================

def save_message(
    conn,
    message,
    embedding
):

    fingerprint = exact_hash(
        message["text"]
    )


    embedding_json = None

    if embedding:

        embedding_json = json.dumps(
            embedding
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
            embedding,
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
            fingerprint,
            embedding_json
        )
    )


# ============================================================
# LIMIT ARCHIVE
# ============================================================

def limit_archive(conn):

    rows = conn.execute(
        """
        SELECT id
        FROM messages
        ORDER BY id DESC
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
# PROCESS
# ============================================================

def process_news(
    message
):

    conn = get_db()

    try:

        duplicate, reason, confidence = (
            find_duplicate(
                conn,
                message
            )
        )


        if duplicate:

            return (
                True,
                reason,
                confidence,
                None
            )


        embedding = create_embedding(
            message
        )


        try:

            save_message(
                conn,
                message,
                embedding
            )

        except sqlite3.IntegrityError:

            return (
                True,
                "EXACT_TEXT",
                1.0,
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
            0.0,
            total
        )


    finally:

        conn.close()


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
🤖 موتور AI تشخیص اخبار تکراری گیمفا

سیستم بررسی:

✓ SHA-256
✓ لینک مقاله
✓ شباهت متنی
✓ شباهت تیتر
✓ Semantic Embedding
✓ تحلیل هوش مصنوعی
✓ تشخیص اتفاق اصلی
✓ تشخیص ادعای اصلی

حتی اگر:
• یک کلمه حذف شود
• چند کلمه تغییر کند
• جمله بازنویسی شود
• تیتر تغییر کند

خبر برای بررسی AI ارسال می‌شود.

اگر واقعاً همان خبر باشد:
♻️ DUPLICATE

اگر اتفاق متفاوت باشد:
🆕

/stats
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


    embeddings = conn.execute(
        """
        SELECT COUNT(*)
        FROM messages
        WHERE embedding IS NOT NULL
        """
    ).fetchone()[0]


    conn.close()


    await update.message.reply_text(
        f"""
📊 وضعیت موتور

📦 آرشیو:
{total}/{MAX_ARCHIVE}

🧠 Embedding:
{embeddings}/{total}

🤖 AI:
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
        "🗑 آرشیو پاک شد."
    )


# ============================================================
# TEXT MESSAGE
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


        duplicate, reason, confidence, total = (
            process_news(
                message
            )
        )


        if duplicate:

            confidence_text = ""

            if confidence:

                confidence_text = (
                    f"\n🎯 اطمینان: "
                    f"{confidence * 100:.1f}%"
                )


            await status.edit_text(
                "♻️ خبر تکراری تشخیص داده شد.\n\n"
                f"🔎 {reason}"
                f"{confidence_text}\n\n"
                "⛔ ذخیره نشد."
            )

            return


        await status.edit_text(
            "🆕 خبر جدید تشخیص داده شد.\n\n"
            "✅ ذخیره شد.\n\n"
            f"📦 آرشیو: {total}/{MAX_ARCHIVE}"
        )


    except Exception as error:

        logger.exception(
            "TEXT ERROR"
        )


        await status.edit_text(
            f"❌ خطا:\n\n{error}"
        )


# ============================================================
# TELEGRAM EXPORT
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


# ============================================================
# IMPORT
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


            duplicate, reason, confidence, total = (
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
# FILE HANDLER
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
                "در حال بررسی با AI..."
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
✅ تمام شد.

📥 ورودی:
{len(messages):,}

🆕 جدید:
{added:,}

♻️ تکراری:
{duplicates:,}

⚠️ رد شده:
{skipped:,}

📦 آرشیو:
{total}/{MAX_ARCHIVE}
"""
        )


    except Exception as error:

        logger.exception(
            "FILE ERROR"
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
            "OPENAI_API_KEY تنظیم نشده."
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


if __name__ == "__main__":

    main()
