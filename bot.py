import os
import re
import io
import json
import sqlite3
import hashlib
import logging
import asyncio
import unicodedata
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from difflib import SequenceMatcher

from PIL import Image
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_FILE = os.getenv("DB_FILE", "gamefa_duplicate.db")
ARCHIVE_SIZE = int(os.getenv("ARCHIVE_SIZE", "150"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
MAX_AI_CANDIDATES = int(os.getenv("MAX_AI_CANDIDATES", "5"))

# ============================================================
# MAIN REPLY KEYBOARD LAYOUT
# ============================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔍 بررسی خبر جدید"), KeyboardButton("📊 آمار آرشیو")],
        [KeyboardButton("🧠 وضعیت AI"), KeyboardButton("📋 راهنما"), KeyboardButton("📦 وضعیت دیتابیس")],
        [KeyboardButton("⚙️ تنظیمات"), KeyboardButton("💬 پشتیبانی"), KeyboardButton("👥 ادمین‌ها"), KeyboardButton("📜 قوانین")],
        [KeyboardButton("🗑 پاکسازی کامل آرشیو")]
    ],
    resize_keyboard=True
)

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("gamefa-engine")

# ============================================================
# OPENAI ASYNC CLIENT
# ============================================================

openai_client: Optional[AsyncOpenAI] = None
if OPENAI_API_KEY:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ============================================================
# AI SCHEMA
# ============================================================

class AIResult(BaseModel):
    news_a_core: str = Field(description="حقایق کلیدی خبر جدید")
    news_b_core: str = Field(description="حقایق کلیدی خبر آرشیوی")
    key_differences: str = Field(description="تفاوت‌های بنیادی")
    same_event: bool = Field(description="آیا رویداد خبری یا تیتر یکسان است؟")
    same_claim: bool = Field(description="آیا موضوع اصلی یکی است؟")
    duplicate: bool = Field(description="آیا خبر تکراری است؟")
    is_update: bool = Field(description="آیا این خبر یک پوشش تکمیلی یا بروزرسانی خبر قبلی است؟")
    confidence: float = Field(description="اطمینان بین 0.0 تا 1.0")
    explanation: str = Field(description="توضیح کوتاه به فارسی")

# ============================================================
# DATABASE SETUP
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
                image_hash TEXT,
                embedding TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sha256 ON news(sha256)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_url ON news(url)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_image_hash ON news(image_hash)")
        conn.commit()

# ============================================================
# 1 & 2. GAMING PRE-PROCESSING & ENTITY EXTRACTION (NER)
# ============================================================

PERSIAN_ARABIC_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789")

BADGES_PATTERN = re.compile(
    r'\[(شایعه|رسمی|فوری|تحلیل|ویدیو|تکمیلی|اختصاصی|گیمفا|دانلود|تریلر)\]|'
    r'#(شایعه|رسمی|فوری|گیمفا|خبر)', re.I
)
BRANDING_PATTERN = re.compile(
    r'(@\w+|https?://\S+|t\.me/\S+|gamefa\.com\S*|رسانه گیمفا|گیمفا|Gamefa)', re.I
)
DECORATION_PATTERN = re.compile(
    r'[-=_*~•▪️▫️🔹🔸🔻🔺🔴🟢🟡⚡️🔥💎📌📍📝📢📣💡🎮🟣🆔]+'
)

def normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(PERSIAN_ARABIC_DIGITS)
    replacements = {"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "ؤ": "و", "إ": "ا", "أ": "ا", "ٱ": "ا", "ـ": ""}
    for a, b in replacements.items():
        text = text.replace(a, b)
    text = text.replace("\u200c", " ")
    text = re.sub(r"https?://\S+", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\U00010000-\U0010ffff]", " ", text)
    text = re.sub(r"[^\w\s\u0600-\u06ff]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()

def clean_gaming_text(text: str) -> str:
    """پاک‌سازی اختصاصی متون رسانه گیمفا"""
    if not text:
        return ""
    text = BADGES_PATTERN.sub(" ", text)
    text = BRANDING_PATTERN.sub(" ", text)
    text = DECORATION_PATTERN.sub(" ", text)
    return normalize(text)

def extract_entities(text: str) -> set:
    """استخراج موجودیت‌های کلیدی (اسامی انگلیسی و اعداد)"""
    if not text:
        return set()
    eng_entities = set(w.lower() for w in re.findall(r'\b[a-zA-Z0-9]{2,}\b', text))
    numbers = set(re.findall(r'\b\d+\b', text))
    return eng_entities | numbers

def entity_overlap_score(text1: str, text2: str) -> float:
    """محاسبه میزان اشتراک موجودیت‌های کلیدی"""
    e1 = extract_entities(text1)
    e2 = extract_entities(text2)
    if not e1 or not e2:
        return 0.0
    intersection = e1 & e2
    return len(intersection) / min(len(e1), len(e2))

def sha256_hash(text: str) -> str:
    return hashlib.sha256(clean_gaming_text(text).encode("utf-8")).hexdigest()

URL_PATTERN = re.compile(r"https?://[^\s<>\]\)\"']+", re.I)

def extract_urls(text: str) -> List[str]:
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
# 6. IMAGE PERCEPTUAL HASHING (dHash)
# ============================================================

def compute_image_hash(image_bytes: bytes) -> str:
    """محاسبه هش تفاوت تصاویر (dHash)"""
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert('L').resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(image.getdata())
        difference = []
        for row in range(8):
            for col in range(8):
                pixel_left = pixels[row * 9 + col]
                pixel_right = pixels[row * 9 + col + 1]
                difference.append(pixel_left > pixel_right)
        decimal_value = 0
        hex_string = []
        for index, value in enumerate(difference):
            if value:
                decimal_value += 2 ** (index % 4)
            if index % 4 == 3:
                hex_string.append(hex(decimal_value)[2:])
                decimal_value = 0
        return "".join(hex_string)
    except Exception as e:
        logger.error("Image hashing error: %s", e)
        return ""

def hamming_distance(h1: str, h2: str) -> int:
    """محاسبه فاصله همینگ برای هش تصاویر"""
    if not h1 or not h2 or len(h1) != len(h2):
        return 999
    return sum(c1 != c2 for c1, c2 in zip(h1, h2))

# ============================================================
# SIMILARITY & EMBEDDINGS
# ============================================================

def sequence_similarity(a: str, b: str) -> float:
    a, b = clean_gaming_text(a), clean_gaming_text(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()

def word_jaccard(a: str, b: str) -> float:
    set_a = set(clean_gaming_text(a).split())
    set_b = set(clean_gaming_text(b).split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)

def text_similarity(a: str, b: str) -> float:
    seq = sequence_similarity(a, b)
    jaccard = word_jaccard(a, b)
    return (seq * 0.70) + (jaccard * 0.30)

async def make_embedding(text: str) -> Optional[List[float]]:
    if not openai_client:
        return None
    try:
        response = await openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=clean_gaming_text(text)[:10000]
        )
        return response.data[0].embedding
    except Exception as e:
        logger.exception("Embedding API error: %s", e)
        return None

def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a)
    nb = sum(x * x for x in b)
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))

# ============================================================
# 4. FEW-SHOT PROMPTING AI JUDGE
# ============================================================

async def ai_compare(new_text: str, old_text: str) -> Optional[AIResult]:
    if not openai_client:
        return None

    system_prompt = """
تو موتور هوشمند تشخیص اخبار تکراری رسانه گیمفا هستی.
تکلیف: تشخیص بده آیا این دو خبر مربوط به یک موضوع/رویداد یکسان هستند یا خیر.

قوانین تحلیل:
۱. اگر یکی از متن‌ها فقط "تیتر کوتاه" و دیگری "متن کامل" همان خبر باشد -> duplicate: true.
۲. اگر خبر دوم بروزرسانی یا اعلام قیمت/تاریخ بعد از خبر اول باشد -> duplicate: true, is_update: true.
۳. اگر دو خبر درباره یک بازی یکسان اما دو رویداد مجزا باشند -> duplicate: false.

نمونه‌های الگو (Few-Shot Examples):

نمونه ۱:
خبر A: "جان کارمک پس از سال‌ها دوباره Doom را تجربه کرد"
خبر B: "جان کارمک هم‌بنیان‌گذار id Software در جریان رویداد QuakeCon نسخه کلاسیک Doom را بازی کرد..."
نتیجه: duplicate = true, is_update = false.

نمونه ۲:
خبر A: "کنسول پلی‌استیشن ۵ پرو رسماً معرفی شد"
خبر B: "قیمت و تاریخ عرضه پلی‌استیشن ۵ پرو مشخص شد"
نتیجه: duplicate = true, is_update = true.

نمونه ۳:
خبر A: "بازی GTA VI در سال ۲۰۲۵ منتشر می‌شود"
خبر B: "تریلر دوم بازی GTA VI رکورد یوتیوب را شکست"
نتیجه: duplicate = false, is_update = false.
"""

    user_prompt = f"""
خبر جدید:
----------------
{new_text[:8000]}
----------------
خبر آرشیوی:
----------------
{old_text[:8000]}
----------------
"""
    try:
        response = await openai_client.beta.chat.completions.parse(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format=AIResult
        )
        return response.choices[0].message.parsed
    except Exception as e:
        logger.exception("AI Reasoning error: %s", e)
        return None

# ============================================================
# 3. TEMPORAL DECAY & CANDIDATES SELECTION
# ============================================================

def get_candidates_sync(new_text: str, new_embedding: Optional[List[float]]):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (ARCHIVE_SIZE,)).fetchall()

    candidates = []
    new_title = extract_title(new_text)
    clean_new = clean_gaming_text(new_text)
    clean_title = clean_gaming_text(new_title)

    now = datetime.now(timezone.utc)

    for row in rows:
        old_text = row["text"]
        clean_old = row["normalized"]
        clean_old_title = clean_gaming_text(row["title"] or "")

        lexical = text_similarity(new_text, old_text)
        title_score = sequence_similarity(new_title, row["title"] or "")
        ner_score = entity_overlap_score(new_text, old_text)

        cross_title = 0.0
        if clean_title and (clean_title in clean_old or clean_old_title in clean_new):
            cross_title = 0.90

        effective_title = max(title_score, cross_title)

        semantic = 0.0
        if new_embedding and row["embedding"]:
            try:
                old_embedding = json.loads(row["embedding"])
                semantic = cosine_similarity(new_embedding, old_embedding)
            except Exception:
                semantic = 0.0

        time_multiplier = 1.0
        if row["created_at"]:
            try:
                created_dt = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                hours_diff = (now - created_dt).total_seconds() / 3600.0
                if hours_diff <= 48:
                    time_multiplier = 1.25
            except Exception:
                pass

        ranking = ((effective_title * 0.40) + (semantic * 0.30) + (ner_score * 0.20) + (lexical * 0.10)) * time_multiplier
        candidates.append((ranking, semantic, lexical, ner_score, row))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[:MAX_AI_CANDIDATES]

# ============================================================
# PIPELINE MAIN CHECK
# ============================================================

async def check_duplicate(text: str, image_hash: Optional[str] = None) -> Dict[str, Any]:
    cleaned = clean_gaming_text(text)
    fingerprint = sha256_hash(text)
    url = get_article_url(text)
    title = extract_title(text)

    def fast_checks():
        with get_db() as conn:
            # ۱. بررسی هش دقیق متن
            row = conn.execute("SELECT * FROM news WHERE sha256 = ? LIMIT 1", (fingerprint,)).fetchone()
            if row:
                return {"duplicate": True, "reason": "exact_hash", "confidence": 1.0, "row": row}

            # ۲. بررسی لینک یکسان خبر
            if url:
                row = conn.execute("SELECT * FROM news WHERE url = ? LIMIT 1", (url,)).fetchone()
                if row:
                    return {"duplicate": True, "reason": "exact_url", "confidence": 1.0, "row": row}

            # ۳. بررسی کاور یکسان تصویر (Image Hash)
            if image_hash:
                img_rows = conn.execute("SELECT * FROM news WHERE image_hash IS NOT NULL AND image_hash != '' ORDER BY id DESC LIMIT 50").fetchall()
                for r in img_rows:
                    dist = hamming_distance(image_hash, r["image_hash"])
                    if dist <= 6:
                        return {"duplicate": True, "reason": "image_match", "confidence": 0.95, "row": r}

            # ۴. بررسی شباهت آستانه‌ای تیترها (Fuzzy Title Matching)
            # این لایه جلوی کپی‌های همراه با تغییرات جزیی کلمات را می‌گیرد
            rows = conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (ARCHIVE_SIZE,)).fetchall()
            clean_t = clean_gaming_text(title)

            if clean_t:
                for r in rows:
                    old_title = clean_gaming_text(r["title"] or "")
                    if old_title:
                        title_sim = SequenceMatcher(None, clean_t, old_title).ratio()
                        if title_sim >= 0.80:
                            return {
                                "duplicate": True,
                                "reason": "fuzzy_title_match",
                                "confidence": title_sim,
                                "row": r
                            }

        return None

    quick_res = await asyncio.to_thread(fast_checks)
    if quick_res:
        return quick_res

    embedding = await make_embedding(cleaned)
    candidates = await asyncio.to_thread(get_candidates_sync, text, embedding)

    tasks = []
    candidate_meta = []

    for ranking, semantic, lexical, ner_score, row in candidates:
        if ranking < 0.25 and ner_score < 0.40:
            continue
        tasks.append(ai_compare(text, row["text"]))
        candidate_meta.append((row, semantic, lexical, ranking))

    if tasks:
        results = await asyncio.gather(*tasks)
        best_decision = None

        for result, (row, semantic, lexical, ranking) in zip(results, candidate_meta):
            if not result:
                continue

            conf = max(0.0, min(1.0, float(result.confidence)))
            current = (conf, result, row)

            if best_decision is None or conf > best_decision[0]:
                best_decision = current

        if best_decision:
            conf, result, row = best_decision
            
            if result.duplicate and result.is_update and conf >= 0.70:
                return {"duplicate": True, "reason": "ai_update", "confidence": conf, "row": row, "explanation": result.explanation}
                
            if result.duplicate and (result.same_event or result.same_claim) and conf >= 0.75:
                return {"duplicate": True, "reason": "ai_high_confidence", "confidence": conf, "row": row, "explanation": result.explanation}
                
            if result.duplicate and conf >= 0.60:
                return {"duplicate": True, "reason": "ai_ambiguous", "confidence": conf, "row": row, "explanation": result.explanation}

    return {"duplicate": False, "reason": "new_news", "confidence": 0.0, "row": None}

async def save_news(text: str, image_hash: Optional[str] = None) -> int:
    cleaned = clean_gaming_text(text)
    title = extract_title(text)
    url = get_article_url(text)
    fingerprint = sha256_hash(text)

    embedding = await make_embedding(cleaned)
    embedding_json = json.dumps(embedding, ensure_ascii=False) if embedding else None

    def db_save():
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT INTO news (telegram_id, text, normalized, title, url, sha256, image_hash, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("", text, cleaned, title, url, fingerprint, image_hash or "", embedding_json)
                )
            except sqlite3.IntegrityError:
                pass

            conn.execute("DELETE FROM news WHERE id NOT IN (SELECT id FROM news ORDER BY id DESC LIMIT ?)", (ARCHIVE_SIZE,))
            conn.commit()
            return conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]

    return await asyncio.to_thread(db_save)

# ============================================================
# TELEGRAM HANDLERS
# ============================================================

def is_allowed(update: Update) -> bool:
    if ADMIN_ID == 0:
        return True
    user = update.effective_user
    return bool(user and user.id == ADMIN_ID)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    await update.message.reply_text(
        "🤖 *ربات پیشرفته تشخیص خبر تکراری گیمفا*\n\n"
        "متن یا تصویر خبر را بفرستید تا آنالیز ۶ لایه‌ای انجام شود:",
        reply_markup=MAIN_KEYBOARD,
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    text = (update.message.text or update.message.caption or "").strip()
    photo = update.message.photo

    if text in ["📊 آمار آرشیو", "📦 وضعیت دیتابیس"]:
        with get_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        await update.message.reply_text(f"📊 *اخبار آرشیو:* {total}/{ARCHIVE_SIZE}", parse_mode="Markdown")
        return
    elif text == "🗑 پاکسازی کامل آرشیو":
        with get_db() as conn:
            conn.execute("DELETE FROM news")
            conn.commit()
        await update.message.reply_text("🗑 آرشیو کاملاً پاکسازی شد.", reply_markup=MAIN_KEYBOARD)
        return
    elif text in ["📋 راهنما", "🔍 بررسی خبر جدید"]:
        await update.message.reply_text("ℹ️ متن یا عکس پست تلگرام را بفرستید.")
        return

    image_hash = None
    if photo:
        file = await context.bot.get_file(photo[-1].file_id)
        img_bytes = await file.download_as_bytearray()
        image_hash = compute_image_hash(bytes(img_bytes))

    if not text and not image_hash:
        return

    if text and (len(text.split()) < 2 and len(text) < 10):
        await update.message.reply_text("⚠️ متن ارسال‌شده بسیار کوتاه است.")
        return

    status = await update.message.reply_text("🔎 در حال آنالیز ۶ لایه‌ای (کاور، کلمات کلیدی، زمان و AI)...")

    try:
        result = await check_duplicate(text, image_hash)

        if result["duplicate"]:
            conf = result["confidence"] * 100
            reason = result["reason"]

            if reason in ["exact_hash", "exact_url", "near_exact_text", "title_exact_match"]:
                await status.edit_text("♻️ *خبر کاملاً تکراری است* (اطمینان ۱۰۰٪)\n\n⛔ خبر ذخیره نشد.", parse_mode="Markdown")
                return

            elif reason == "fuzzy_title_match":
                await status.edit_text(f"♻️ *خبر تکراری است* (شباهت تیتر: {conf:.1f}%)\n\n⛔ خبر ذخیره نشد.", parse_mode="Markdown")
                return

            elif reason == "image_match":
                await status.edit_text("🖼 *کاور/تصویر خبر تکراری است*\n\n⛔ خبر ذخیره نشد.", parse_mode="Markdown")
                return

            elif reason == "ai_update":
                explanation = result.get("explanation", "")
                await status.edit_text(
                    f"ℹ️ *این پست بروزرسانی / پوشش تکمیلی خبر قبلی است*\n\n"
                    f"🎯 درصد شباهت: {conf:.1f}%\n"
                    f"💡 تحلیل AI: {explanation}\n\n"
                    f"⛔ به عنوان خبر مستقل ذخیره نشد.",
                    parse_mode="Markdown"
                )
                return

            elif reason == "ai_high_confidence":
                explanation = result.get("explanation", "")
                await status.edit_text(
                    f"♻️ *خبر تکراری است*\n\n"
                    f"🎯 درصد اطمینان: {conf:.1f}%\n"
                    f"💡 دلیل AI: {explanation}\n\n"
                    f"⛔ خبر ذخیره نشد.",
                    parse_mode="Markdown"
                )
                return

            elif reason == "ai_ambiguous":
                context.user_data["pending_news"] = text
                context.user_data["pending_image_hash"] = image_hash
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ ذخیره شود (خبر جدید)", callback_data="force_save"),
                        InlineKeyboardButton("❌ رد شود (تکراری)", callback_data="force_discard")
                    ]
                ])
                explanation = result.get("explanation", "")
                await status.edit_text(
                    f"⚠️ *نیازمند بررسی ادمین*\n\n"
                    f"🎯 درصد شباهت: {conf:.1f}%\n"
                    f"💡 تحلیل AI: {explanation}\n\n"
                    f"آیا خبر ذخیره شود؟",
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
                return

        total = await save_news(text, image_hash)
        await status.edit_text(
            f"🆕 *خبر جدید است*\n\n"
            f"✅ ذخیره شد.\n"
            f"📦 آرشیو: {total}/{ARCHIVE_SIZE}",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.exception("Processing Error")
        await status.edit_text(f"❌ خطا در پردازش:\n{str(e)}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    pending_text = context.user_data.get("pending_news")
    pending_img = context.user_data.get("pending_image_hash")

    if query.data == "force_save" and pending_text:
        total = await save_news(pending_text, pending_img)
        await query.edit_message_text(f"✅ خبر ذخیره شد.\n📦 آرشیو: {total}/{ARCHIVE_SIZE}", parse_mode="Markdown")
    elif query.data == "force_discard":
        await query.edit_message_text("❌ خبر تکراری تشخیص داده شد و رد گردید.", parse_mode="Markdown")

    context.user_data.pop("pending_news", None)
    context.user_data.pop("pending_image_hash", None)

# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("GAMEFA ADVANCED DUPLICATE ENGINE STARTED")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
