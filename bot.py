import os
import re
import json
import sqlite3
import hashlib
import logging
import asyncio
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

from openai import AsyncOpenAI
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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# اگر 0 باشد همه می‌توانند از ربات استفاده کنند.
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_FILE = os.getenv("DB_FILE", "gamefa_duplicate.db")
ARCHIVE_SIZE = int(os.getenv("ARCHIVE_SIZE", "100"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")

# تعداد کاندیداهایی که برای AI فرستاده می‌شوند.
MAX_AI_CANDIDATES = int(os.getenv("MAX_AI_CANDIDATES", "8"))


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("gamefa-duplicate")


# ============================================================
# OPENAI CLIENT (ASYNC)
# ============================================================

openai_client: Optional[AsyncOpenAI] = None

if OPENAI_API_KEY:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# ============================================================
# AI OUTPUT SCHEMA
# ============================================================

class AIResult(BaseModel):
    duplicate: bool = Field(description="آیا همان خبر است؟")
    confidence: float = Field(description="اطمینان بین 0 و 1")
    same_event: bool = Field(description="آیا اتفاق اصلی یکی است؟")
    same_claim: bool = Field(description="آیا ادعای اصلی یکی است؟")
    explanation: str = Field(description="دلیل کوتاه")


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_db() as conn:
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sha256 ON news(sha256)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_url ON news(url)")
        conn.commit()


# ============================================================
# ACCESS CONTROL
# ============================================================

def is_allowed(update: Update) -> bool:
    if ADMIN_ID == 0:
        return True
    if not update.effective_user:
        return False
    return update.effective_user.id == ADMIN_ID


# ============================================================
# NORMALIZATION & EXTRACTION
# ============================================================

def normalize(text: str) -> str:
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)

    replacements = {
        "ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه",
        "ۀ": "ه", "ؤ": "و", "إ": "ا", "أ": "ا",
        "ٱ": "ا", "ـ": "",
    }

    for a, b in replacements.items():
        text = text.replace(a, b)

    text = text.replace("\u200c", " ")
    text = re.sub(r"https?://\S+", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\U00010000-\U0010ffff]", " ", text)
    text = re.sub(r"[^\w\s\u0600-\u06ff]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)

    return text.strip().lower()


def sha256(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


URL_PATTERN = re.compile(r"https?://[^\s<>\]\)\"']+", re.I)


def extract_urls(text: str):
    if not text:
        return []
    urls = [url.rstrip(".,،؛!?؟") for url in URL_PATTERN.findall(text)]
    return list(dict.fromkeys(urls))


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    url = re.sub(r"[?&]utm_[^&]+", "", url, flags=re.I)
    return url.lower()


def get_article_url(text: str) -> str:
    urls = extract_urls(text)
    if not urls:
        return ""
    for url in urls:
        if "gamefa.com" in url.lower():
            return normalize_url(url)
    return normalize_url(urls[0])


def extract_title(text: str) -> str:
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    title = re.sub(r"^[^\w\u0600-\u06ff]+", "", lines[0], flags=re.UNICODE)
    return title[:600]


# ============================================================
# TEXT SIMILARITY
# ============================================================

def sequence_similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def word_similarity(a: str, b: str) -> float:
    A = set(normalize(a).split())
    B = set(normalize(b).split())
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def text_similarity(a: str, b: str) -> float:
    seq = sequence_similarity(a, b)
    word = word_similarity(a, b)
    return (seq * 0.65) + (word * 0.35)


# ============================================================
# EMBEDDING & VECTOR SIMILARITY
# ============================================================

async def make_embedding(text: str):
    if not openai_client:
        return None

    text = text[:12000]
    try:
        response = await openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text
        )
        return response.data[0].embedding
    except Exception as e:
        logger.exception("Embedding error: %s", e)
        return None


def cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a)
    nb = sum(x * x for x in b)
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


# ============================================================
# AI COMPARISON
# ============================================================

async def ai_compare(new_text: str, old_text: str) -> Optional[AIResult]:
    if not openai_client:
        return None

    prompt = f"""
تو سیستم تخصصی تشخیص اخبار تکراری گیمفا هستی.

هدف این نیست که بفهمی دو خبر «موضوع مشابه» دارند.
هدف این است که بفهمی آیا آن‌ها دقیقاً یک اتفاق خبری / ادعای خبری را گزارش می‌کنند.

خبر جدید:
----------------
{new_text[:12000]}
----------------

خبر آرشیوی:
----------------
{old_text[:12000]}
----------------

قوانین بسیار مهم:
1. اگر فقط یک کلمه حذف شده باشد، همان خبر است.
2. اگر چند کلمه تغییر کرده باشد، ولی اتفاق اصلی و ادعای اصلی یکی باشد، همان خبر است.
3. اگر جمله‌ها بازنویسی شده باشند، ولی همان اتفاق را گزارش کنند، همان خبر است.
4. اگر تیتر تغییر کرده ولی اتفاق همان است، همان خبر است.
5. اگر دو خبر فقط درباره یک بازی، بازیگر، شرکت یا شخصیت مشترک باشند، اما دو اتفاق متفاوت را گزارش کنند، تکراری نیستند.
6. اگر یکی خبر اولیه و دیگری همان خبر با جزئیات بیشتر باشد، تکراری است.
7. اگر خبر دوم یک آپدیت کاملاً جدید از یک اتفاق قدیمی باشد، به‌صورت خودکار تکراری حساب نکن.
8. شباهت کلمات به‌تنهایی کافی نیست.
9. موضوع مشترک به‌تنهایی کافی نیست.
10. اگر مطمئن نیستی، duplicate=false بده.
"""

    try:
        response = await openai_client.beta.chat.completions.parse(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "You are an extremely strict news duplicate detector."},
                {"role": "user", "content": prompt}
            ],
            response_format=AIResult
        )
        return response.choices[0].message.parsed
    except Exception as e:
        logger.exception("AI comparison error: %s", e)
        return None


# ============================================================
# CANDIDATE SELECTION
# ============================================================

def get_candidates_sync(new_text: str, new_embedding: Optional[list]):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM news ORDER BY id DESC LIMIT ?",
            (ARCHIVE_SIZE,)
        ).fetchall()

    candidates = []
    new_title = extract_title(new_text)

    for row in rows:
        old_text = row["text"]
        lexical = text_similarity(new_text, old_text)
        title_score = sequence_similarity(new_title, row["title"] or "")

        semantic = 0.0
        if new_embedding and row["embedding"]:
            try:
                old_embedding = json.loads(row["embedding"])
                semantic = cosine(new_embedding, old_embedding)
            except Exception:
                semantic = 0.0

        ranking = (semantic * 0.60) + (lexical * 0.30) + (title_score * 0.10)
        candidates.append((ranking, semantic, lexical, title_score, row))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[:MAX_AI_CANDIDATES]


# ============================================================
# MAIN DUPLICATE ENGINE
# ============================================================

async def check_duplicate(text: str) -> dict:
    normalized = normalize(text)
    fingerprint = sha256(text)
    url = get_article_url(text)

    # 1. Exact Hash & 2. Same URL & 3. Text Similarity (Offloaded to thread)
    def db_quick_checks():
        with get_db() as conn:
            # 1. Exact Hash
            row = conn.execute("SELECT * FROM news WHERE sha256 = ? LIMIT 1", (fingerprint,)).fetchone()
            if row:
                return {"duplicate": True, "reason": "exact", "confidence": 1.0, "row": row}

            # 2. Same Article URL
            if url:
                row = conn.execute("SELECT * FROM news WHERE url = ? LIMIT 1", (url,)).fetchone()
                if row:
                    return {"duplicate": True, "reason": "same_url", "confidence": 1.0, "row": row}

            # 3. Near Exact Text
            rows = conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (ARCHIVE_SIZE,)).fetchall()
            best_lexical = None
            for r in rows:
                score = text_similarity(text, r["text"])
                if best_lexical is None or score > best_lexical[0]:
                    best_lexical = (score, r)

            if best_lexical and best_lexical[0] >= 0.985:
                return {
                    "duplicate": True,
                    "reason": "near_exact_text",
                    "confidence": best_lexical[0],
                    "row": best_lexical[1]
                }

        return None

    quick_result = await asyncio.to_thread(db_quick_checks)
    if quick_result:
        return quick_result

    # 4. Semantic Search
    embedding = await make_embedding(normalized)
    candidates = await asyncio.to_thread(get_candidates_sync, text, embedding)

    # 5. AI Judge (Run AI comparisons in Parallel using asyncio.gather)
    tasks = []
    candidate_meta = []

    for ranking, semantic, lexical, title_score, row in candidates:
        if semantic < 0.45 and lexical < 0.30 and title_score < 0.50:
            continue
        tasks.append(ai_compare(text, row["text"]))
        candidate_meta.append((row, semantic, lexical, title_score))

    if tasks:
        results = await asyncio.gather(*tasks)
        best_ai = None

        for result, (row, semantic, lexical, title_score) in zip(results, candidate_meta):
            if not result:
                continue

            confidence = max(0.0, min(1.0, float(result.confidence)))
            current = (confidence, result, row, semantic, lexical, title_score)

            if best_ai is None or confidence > best_ai[0]:
                best_ai = current

        if best_ai:
            confidence, result, row, semantic, lexical, title_score = best_ai
            if result.duplicate and result.same_event and result.same_claim and confidence >= 0.90:
                return {
                    "duplicate": True,
                    "reason": "ai",
                    "confidence": confidence,
                    "row": row,
                    "explanation": result.explanation
                }

    return {"duplicate": False, "reason": "new", "confidence": 0.0, "row": None}


# ============================================================
# SAVE NEWS
# ============================================================

async def save_news(text: str) -> int:
    normalized = normalize(text)
    title = extract_title(text)
    url = get_article_url(text)
    fingerprint = sha256(text)

    embedding = await make_embedding(normalized)
    embedding_json = json.dumps(embedding, ensure_ascii=False) if embedding else None

    def db_save():
        with get_db() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO news (telegram_id, text, normalized, title, url, sha256, embedding)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("", text, normalized, title, url, fingerprint, embedding_json)
                )
            except sqlite3.IntegrityError:
                pass

            conn.execute(
                """
                DELETE FROM news
                WHERE id NOT IN (
                    SELECT id FROM news ORDER BY id DESC LIMIT ?
                )
                """,
                (ARCHIVE_SIZE,)
            )
            conn.commit()
            return conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]

    return await asyncio.to_thread(db_save)


# ============================================================
# HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    await update.message.reply_text(
        "🤖 موتور تشخیص خبر تکراری گیمفا فعال است.\n\n"
        "خبر را بفرستید تا بررسی انجام شود."
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    def get_stats():
        with get_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
            embeddings = conn.execute("SELECT COUNT(*) FROM news WHERE embedding IS NOT NULL").fetchone()[0]
            return total, embeddings

    total, embeddings = await asyncio.to_thread(get_stats)

    await update.message.reply_text(
        f"📊 وضعیت موتور\n\n"
        f"📦 آرشیو: {total}/{ARCHIVE_SIZE}\n"
        f"🧠 Embedding: {embeddings}/{total}\n"
        f"🤖 AI: {'فعال ✅' if openai_client else 'غیرفعال ❌'}"
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    def db_clear():
        with get_db() as conn:
            conn.execute("DELETE FROM news")
            conn.commit()

    await asyncio.to_thread(db_clear)
    await update.message.reply_text("🗑 آرشیو پاک شد.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    text = (update.message.text or update.message.caption or "").strip()
    if not text or text.startswith("/"):
        return

    status = await update.message.reply_text("🧠 در حال بررسی...")

    try:
        result = await check_duplicate(text)

        if result["duplicate"]:
            confidence = result["confidence"] * 100
            reason = result["reason"]

            if reason == "exact":
                reason_text = "متن دقیقاً تکراری است."
            elif reason == "same_url":
                reason_text = "لینک مقاله قبلاً ثبت شده است."
            elif reason == "near_exact_text":
                reason_text = "متن تقریباً یکسان است."
            else:
                reason_text = result.get("explanation", "AI همان اتفاق خبری را تشخیص داد.")

            await status.edit_text(
                f"♻️ خبر تکراری است.\n\n"
                f"🎯 اطمینان: {confidence:.1f}%\n"
                f"🔎 دلیل: {reason_text}\n\n"
                f"⛔ ذخیره نشد."
            )
            return

        total = await save_news(text)
        await status.edit_text(
            f"🆕 خبر جدید است.\n\n"
            f"✅ ذخیره شد.\n\n"
            f"📦 آرشیو: {total}/{ARCHIVE_SIZE}"
        )

    except Exception as e:
        logger.exception("MESSAGE ERROR")
        await status.edit_text(f"❌ خطا هنگام بررسی خبر:\n\n{str(e)}")


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY تنظیم نشده؛ تشخیص AI کار نخواهد کرد.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("GAMEFA DUPLICATE ENGINE STARTED")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

