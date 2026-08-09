import os
import re
import json
import sqlite3
import hashlib
import logging
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

from openai import OpenAI
from pydantic import BaseModel, Field

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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("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", "").strip()

# اگر 0 باشد همه می‌توانند از ربات استفاده کنند.
# برای محدود کردن، ADMIN_ID را تنظیم کن.
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_FILE = os.getenv(
    "DB_FILE",
    "gamefa_duplicate.db"
)

ARCHIVE_SIZE = int(
    os.getenv(
        "ARCHIVE_SIZE",
        "100"
    )
)

EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "text-embedding-3-small"
)

AI_MODEL = os.getenv(
    "AI_MODEL",
    "gpt-5.5"
)

# تعداد کاندیداهایی که برای AI فرستاده می‌شوند.
MAX_AI_CANDIDATES = int(
    os.getenv(
        "MAX_AI_CANDIDATES",
        "8"
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
    "gamefa-duplicate"
)


# ============================================================
# OPENAI
# ============================================================

openai_client: Optional[OpenAI] = None

if OPENAI_API_KEY:
    openai_client = OpenAI(
        api_key=OPENAI_API_KEY
    )


# ============================================================
# AI OUTPUT
# ============================================================

class AIResult(BaseModel):

    duplicate: bool = Field(
        description="آیا همان خبر است؟"
    )

    confidence: float = Field(
        description="اطمینان بین 0 و 1"
    )

    same_event: bool = Field(
        description="آیا اتفاق اصلی یکی است؟"
    )

    same_claim: bool = Field(
        description="آیا ادعای اصلی یکی است؟"
    )

    explanation: str = Field(
        description="دلیل کوتاه"
    )


# ============================================================
# DATABASE
# ============================================================

def db():

    conn = sqlite3.connect(
        DB_FILE,
        timeout=60
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        """
        PRAGMA journal_mode=WAL
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT,
            text TEXT NOT NULL,
            normalized TEXT NOT NULL,
            title TEXT,
            url TEXT,
            sha256 TEXT UNIQUE,
            embedding TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_sha256
        ON news(sha256)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_url
        ON news(url)
        """
    )

    conn.commit()

    return conn


# ============================================================
# ACCESS
# ============================================================

def is_allowed(update: Update):

    if ADMIN_ID == 0:
        return True

    if not update.effective_user:
        return False

    return (
        update.effective_user.id
        ==
        ADMIN_ID
    )


# ============================================================
# NORMALIZATION
# ============================================================

def normalize(text: str) -> str:

    if not text:
        return ""

    text = unicodedata.normalize(
        "NFKC",
        text
    )

    # فارسی/عربی
    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ة": "ه",
        "ۀ": "ه",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا",
        "ـ": "",
    }

    for a, b in replacements.items():
        text = text.replace(a, b)

    # ZWNJ
    text = text.replace(
        "\u200c",
        " "
    )

    # لینک‌ها را حذف نمی‌کنیم؛
    # در لایه جداگانه بررسی می‌شوند.

    # URLها برای مقایسه متنی
    text = re.sub(
        r"https?://\S+",
        " ",
        text,
        flags=re.I
    )

    # Markdown/HTML ساده
    text = re.sub(
        r"<[^>]+>",
        " ",
        text
    )

    # ایموجی‌ها
    text = re.sub(
        r"[\U00010000-\U0010ffff]",
        " ",
        text
    )

    # علائم
    text = re.sub(
        r"[^\w\s\u0600-\u06ff]",
        " ",
        text,
        flags=re.UNICODE
    )

    # فاصله
    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip().lower()


# ============================================================
# SHA256
# ============================================================

def sha256(text: str):

    return hashlib.sha256(
        normalize(text).encode(
            "utf-8"
        )
    ).hexdigest()


# ============================================================
# URL EXTRACTION
# ============================================================

URL_PATTERN = re.compile(
    r"https?://[^\s<>\]\)\"']+",
    re.I
)


def extract_urls(text: str):

    if not text:
        return []

    urls = []

    for url in URL_PATTERN.findall(
        text
    ):

        url = url.rstrip(
            ".,،؛!?؟"
        )

        urls.append(
            url
        )

    return list(
        dict.fromkeys(
            urls
        )
    )


def normalize_url(url: str):

    url = url.strip()

    url = url.rstrip(
        "/"
    )

    # لینک‌های Telegram
    url = re.sub(
        r"[?&]utm_[^&]+",
        "",
        url,
        flags=re.I
    )

    return url.lower()


def get_article_url(text: str):

    urls = extract_urls(
        text
    )

    if not urls:
        return ""

    # اولویت Gamefa
    for url in urls:

        if "gamefa.com" in url.lower():
            return normalize_url(url)

    return normalize_url(
        urls[0]
    )


# ============================================================
# TITLE EXTRACTION
# ============================================================

def extract_title(text: str):

    if not text:
        return ""

    lines = []

    for line in text.splitlines():

        line = line.strip()

        if not line:
            continue

        lines.append(
            line
        )

    if not lines:
        return ""

    title = lines[0]

    # حذف ایموجی ابتدای عنوان
    title = re.sub(
        r"^[^\w\u0600-\u06ff]+",
        "",
        title,
        flags=re.UNICODE
    )

    return title[:600]


# ============================================================
# TEXT SIMILARITY
# ============================================================

def sequence_similarity(
    a: str,
    b: str
):

    a = normalize(a)
    b = normalize(b)

    if not a or not b:
        return 0.0

    return SequenceMatcher(
        None,
        a,
        b
    ).ratio()


def word_similarity(
    a: str,
    b: str
):

    A = set(
        normalize(a).split()
    )

    B = set(
        normalize(b).split()
    )

    if not A or not B:
        return 0.0

    return len(A & B) / len(
        A | B
    )


def text_similarity(
    a: str,
    b: str
):

    seq = sequence_similarity(
        a,
        b
    )

    word = word_similarity(
        a,
        b
    )

    return (
        seq * 0.65
        +
        word * 0.35
    )


# ============================================================
# EMBEDDING
# ============================================================

def make_embedding(text: str):

    if not openai_client:
        return None

    text = text[:12000]

    try:

        response = (
            openai_client
            .embeddings
            .create(
                model=EMBEDDING_MODEL,
                input=text
            )
        )

        return (
            response
            .data[0]
            .embedding
        )

    except Exception as e:

        logger.exception(
            "Embedding error: %s",
            e
        )

        return None


def cosine(a, b):

    if not a or not b:
        return 0.0

    if len(a) != len(b):
        return 0.0

    dot = sum(
        x * y
        for x, y in zip(a, b)
    )

    na = sum(
        x * x
        for x in a
    )

    nb = sum(
        x * x
        for x in b
    )

    if na <= 0 or nb <= 0:
        return 0.0

    return dot / (
        na ** 0.5
        *
        nb ** 0.5
    )


# ============================================================
# AI COMPARISON
# ============================================================

def ai_compare(
    new_text: str,
    old_text: str
):

    if not openai_client:

        return None

    prompt = f"""
تو سیستم تخصصی تشخیص اخبار تکراری گیمفا هستی.

هدف این نیست که بفهمی دو خبر «موضوع مشابه» دارند.

هدف این است که بفهمی آیا آنها
دقیقاً یک اتفاق خبری / ادعای خبری را گزارش می‌کنند.

خبر جدید:
----------------
{new_text[:12000]}
----------------

خبر آرشیوی:
----------------
{old_text[:12000]}
----------------

قوانین بسیار مهم:

1. اگر فقط یک کلمه حذف شده باشد،
همان خبر است.

2. اگر چند کلمه تغییر کرده باشد،
ولی اتفاق اصلی و ادعای اصلی یکی باشد،
همان خبر است.

3. اگر جمله‌ها بازنویسی شده باشند،
ولی همان اتفاق را گزارش کنند،
همان خبر است.

4. اگر تیتر تغییر کرده ولی اتفاق همان است،
همان خبر است.

5. اگر دو خبر فقط درباره یک بازی،
بازیگر، شرکت یا شخصیت مشترک باشند،
اما دو اتفاق متفاوت را گزارش کنند،
تکراری نیستند.

6. اگر یکی خبر اولیه و دیگری همان خبر
با جزئیات بیشتر باشد،
تکراری است.

7. اگر خبر دوم یک آپدیت کاملاً جدید
از یک اتفاق قدیمی باشد،
به‌صورت خودکار تکراری حساب نکن.

8. شباهت کلمات به‌تنهایی کافی نیست.

9. موضوع مشترک به‌تنهایی کافی نیست.

10. اگر مطمئن نیستی،
duplicate=false بده.

فقط نتیجه ساختاریافته بده.
"""

    try:

        response = (
            openai_client
            .responses
            .parse(
                model=AI_MODEL,
                input=[
                    {
                        "role": "system",
                        "content":
                            "You are an extremely strict news duplicate detector."
                    },
                    {
                        "role": "user",
                        "content":
                            prompt
                    }
                ],
                text_format=AIResult
            )
        )

        for output in response.output:

            if output.type != "message":
                continue

            for content in output.content:

                if (
                    content.type
                    ==
                    "output_text"
                ):

                    if content.parsed:

                        result = (
                            content.parsed
                        )

                        return result

    except Exception as e:

        logger.exception(
            "AI comparison error: %s",
            e
        )

    return None


# ============================================================
# BUILD CANDIDATES
# ============================================================

def get_candidates(
    conn,
    new_text,
    new_embedding
):

    rows = conn.execute(
        """
        SELECT *
        FROM news
        ORDER BY id DESC
        LIMIT ?
        """,
        (
            ARCHIVE_SIZE,
        )
    ).fetchall()

    candidates = []

    for row in rows:

        old_text = row["text"]

        lexical = text_similarity(
            new_text,
            old_text
        )

        title_score = sequence_similarity(
            extract_title(new_text),
            row["title"] or ""
        )

        semantic = 0.0

        if new_embedding:

            old_embedding = None

            if row["embedding"]:

                try:

                    old_embedding = json.loads(
                        row["embedding"]
                    )

                except Exception:
                    old_embedding = None

            if old_embedding:

                semantic = cosine(
                    new_embedding,
                    old_embedding
                )

        # امتیاز ترکیبی فقط برای
        # پیدا کردن کاندید مناسب است.
        ranking = (
            semantic * 0.60
            +
            lexical * 0.30
            +
            title_score * 0.10
        )

        candidates.append(
            (
                ranking,
                semantic,
                lexical,
                title_score,
                row
            )
        )

    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )

    return candidates[
        :MAX_AI_CANDIDATES
    ]


# ============================================================
# MAIN DUPLICATE ENGINE
# ============================================================

def check_duplicate(
    text: str
):

    conn = db()

    normalized = normalize(
        text
    )

    fingerprint = sha256(
        text
    )

    url = get_article_url(
        text
    )

    # --------------------------------------------------------
    # 1. EXACT HASH
    # --------------------------------------------------------

    row = conn.execute(
        """
        SELECT *
        FROM news
        WHERE sha256 = ?
        LIMIT 1
        """,
        (
            fingerprint,
        )
    ).fetchone()

    if row:

        conn.close()

        return {
            "duplicate": True,
            "reason": "exact",
            "confidence": 1.0,
            "row": row
        }


    # --------------------------------------------------------
    # 2. SAME ARTICLE URL
    # --------------------------------------------------------

    if url:

        row = conn.execute(
            """
            SELECT *
            FROM news
            WHERE url = ?
            LIMIT 1
            """,
            (
                url,
            )
        ).fetchone()

        if row:

            conn.close()

            return {
                "duplicate": True,
                "reason": "same_url",
                "confidence": 1.0,
                "row": row
            }


    # --------------------------------------------------------
    # 3. VERY HIGH TEXT SIMILARITY
    # --------------------------------------------------------

    rows = conn.execute(
        """
        SELECT *
        FROM news
        ORDER BY id DESC
        LIMIT ?
        """,
        (
            ARCHIVE_SIZE,
        )
    ).fetchall()

    best_lexical = None

    for row in rows:

        score = text_similarity(
            text,
            row["text"]
        )

        if (
            best_lexical is None
            or
            score > best_lexical[0]
        ):

            best_lexical = (
                score,
                row
            )


    # اگر متن تقریباً یکی باشد،
    # بدون نیاز به AI هم Duplicate است.
    if (
        best_lexical
        and
        best_lexical[0] >= 0.985
    ):

        conn.close()

        return {
            "duplicate": True,
            "reason": "near_exact_text",
            "confidence":
                best_lexical[0],
            "row":
                best_lexical[1]
        }


    # --------------------------------------------------------
    # 4. SEMANTIC SEARCH
    # --------------------------------------------------------

    embedding = make_embedding(
        normalized
    )

    candidates = get_candidates(
        conn,
        text,
        embedding
    )


    # --------------------------------------------------------
    # 5. AI JUDGE
    # --------------------------------------------------------

    best_ai = None

    for (
        ranking,
        semantic,
        lexical,
        title_score,
        row
    ) in candidates:

        # اگر حتی شباهت بسیار پایین باشد،
        # AI را بی‌دلیل صدا نمی‌زنیم.
        if (
            semantic < 0.45
            and
            lexical < 0.30
            and
            title_score < 0.50
        ):
            continue

        result = ai_compare(
            text,
            row["text"]
        )

        if result is None:
            continue

        confidence = max(
            0.0,
            min(
                1.0,
                float(
                    result.confidence
                )
            )
        )

        current = (
            confidence,
            result,
            row,
            semantic,
            lexical,
            title_score
        )

        if (
            best_ai is None
            or
            confidence > best_ai[0]
        ):

            best_ai = current


    conn.close()


    # --------------------------------------------------------
    # 6. FINAL DECISION
    # --------------------------------------------------------

    if best_ai:

        (
            confidence,
            result,
            row,
            semantic,
            lexical,
            title_score
        ) = best_ai


        # فقط زمانی Duplicate می‌کنیم
        # که AI واقعاً مطمئن باشد
        # و هر دو شرط اصلی را تأیید کند.

        if (
            result.duplicate
            and
            result.same_event
            and
            result.same_claim
            and
            confidence >= 0.90
        ):

            return {
                "duplicate": True,
                "reason": "ai",
                "confidence":
                    confidence,
                "row":
                    row,
                "explanation":
                    result.explanation
            }


    return {
        "duplicate": False,
        "reason": "new",
        "confidence": 0.0,
        "row": None
    }


# ============================================================
# SAVE
# ============================================================

def save_news(
    text: str
):

    conn = db()

    normalized = normalize(
        text
    )

    title = extract_title(
        text
    )

    url = get_article_url(
        text
    )

    fingerprint = sha256(
        text
    )

    embedding = make_embedding(
        normalized
    )

    embedding_json = None

    if embedding:

        embedding_json = json.dumps(
            embedding,
            ensure_ascii=False
        )


    try:

        conn.execute(
            """
            INSERT INTO news
            (
                telegram_id,
                text,
                normalized,
                title,
                url,
                sha256,
                embedding
            )
            VALUES
            (
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?
            )
            """,
            (
                "",
                text,
                normalized,
                title,
                url,
                fingerprint,
                embedding_json
            )
        )

        conn.commit()

    except sqlite3.IntegrityError:

        pass


    # --------------------------------------------------------
    # فقط 100 خبر آخر
    # --------------------------------------------------------

    conn.execute(
        """
        DELETE FROM news
        WHERE id NOT IN
        (
            SELECT id
            FROM news
            ORDER BY id DESC
            LIMIT ?
        )
        """,
        (
            ARCHIVE_SIZE,
        )
    )

    conn.commit()

    total = conn.execute(
        """
        SELECT COUNT(*)
        FROM news
        """
    ).fetchone()[0]

    conn.close()

    return total


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_allowed(update):
        return

    await update.message.reply_text(
        """
🤖 موتور تشخیص خبر تکراری گیمفا فعال است.

خبر را بفرست.

سیستم برای هر خبر:

• متن را نرمال می‌کند
• SHA-256 را بررسی می‌کند
• لینک را بررسی می‌کند
• شباهت متنی را بررسی می‌کند
• شباهت معنایی را بررسی می‌کند
• کاندیداهای مشابه را پیدا می‌کند
• با AI اتفاق اصلی را مقایسه می‌کند
• ادعای اصلی را مقایسه می‌کند

اگر مطمئن نباشد، خبر را حذف نمی‌کند.
"""
    )


# ============================================================
# STATS
# ============================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_allowed(update):
        return

    conn = db()

    total = conn.execute(
        """
        SELECT COUNT(*)
        FROM news
        """
    ).fetchone()[0]

    embeddings = conn.execute(
        """
        SELECT COUNT(*)
        FROM news
        WHERE embedding IS NOT NULL
        """
    ).fetchone()[0]

    conn.close()

    await update.message.reply_text(
        f"""
📊 وضعیت موتور

📦 آرشیو:
{total}/{ARCHIVE_SIZE}

🧠 Embedding:
{embeddings}/{total}

🤖 AI:
{"فعال ✅" if openai_client else "غیرفعال ❌"}
"""
    )


# ============================================================
# CLEAR
# ============================================================

async def clear(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_allowed(update):
        return

    conn = db()

    conn.execute(
        "DELETE FROM news"
    )

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "🗑 آرشیو 100 خبر پاک شد."
    )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_allowed(update):
        return

    if not update.message:
        return

    text = (
        update.message.text
        or
        update.message.caption
        or
        ""
    ).strip()

    if not text:
        return

    if text.startswith("/"):
        return


    status = await update.message.reply_text(
        "🧠 در حال بررسی..."
    )


    try:

        result = check_duplicate(
            text
        )


        if result["duplicate"]:

            confidence = (
                result["confidence"]
                * 100
            )

            reason = result[
                "reason"
            ]

            if reason == "exact":
                reason_text = (
                    "متن دقیقاً تکراری است."
                )

            elif reason == "same_url":
                reason_text = (
                    "لینک مقاله قبلاً ثبت شده است."
                )

            elif reason == "near_exact_text":
                reason_text = (
                    "متن تقریباً یکسان است."
                )

            else:
                reason_text = (
                    result.get(
                        "explanation",
                        "AI همان اتفاق خبری را تشخیص داد."
                    )
                )


            await status.edit_text(
                f"""
♻️ خبر تکراری است.

🎯 اطمینان:
{confidence:.1f}%

🔎 دلیل:
{reason_text}

⛔ ذخیره نشد.
"""
            )

            return


        total = save_news(
            text
        )


        await status.edit_text(
            f"""
🆕 خبر جدید است.

✅ ذخیره شد.

📦 آرشیو:
{total}/{ARCHIVE_SIZE}
"""
        )


    except Exception as e:

        logger.exception(
            "MESSAGE ERROR"
        )

        await status.edit_text(
            f"""
❌ خطا هنگام بررسی خبر:

{str(e)}
"""
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
            "OPENAI_API_KEY تنظیم نشده؛ "
            "تشخیص AI کار نخواهد کرد."
        )


    db().close()


    app = (
        Application
        .builder()
        .token(
            BOT_TOKEN
        )
        .build()
    )


    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            stats
        )
    )

    app.add_handler(
        CommandHandler(
            "clear",
            clear
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            &
            ~filters.COMMAND,
            handle_message
        )
    )


    logger.info(
        "GAMEFA DUPLICATE ENGINE STARTED"
    )


    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
