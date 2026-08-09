import os
import re
import io
import html
import json
import math
import base64
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
from telegram.error import BadRequest
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

# شناسه ادمین‌ها (می‌توانید چندین آیدی را با کاما جدا کنید مثلاً "123,456")
ADMIN_IDS = [int(i.strip()) for i in os.getenv("ADMIN_ID", "0").split(",") if i.strip().isdigit()]

# شناسه انحصاری مالک اصلی (فقط این آیدی حق پاکسازی آرشیو را دارد)
OWNER_ID = 8202357756

DB_FILE = os.getenv("DB_FILE", "gamefa_duplicate.db")
ARCHIVE_SIZE = int(os.getenv("ARCHIVE_SIZE", "150"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
MAX_AI_CANDIDATES = int(os.getenv("MAX_AI_CANDIDATES", "5"))

# ============================================================
# GAMING ALIASES MAPPING
# ============================================================

GAME_ALIASES = {
    "ps5": "playstation5",
    "پلی استیشن 5": "playstation5",
    "پلی استیشن ۵": "playstation5",
    "ps4": "playstation4",
    "پلی استیشن 4": "playstation4",
    "پلی استیشن ۴": "playstation4",
    "xbox series x": "xboxseriesx",
    "ایکس باکس سری ایکس": "xboxseriesx",
    "xbox series s": "xboxseriess",
    "ایکس باکس سری اس": "xboxseriess",
    "xbox": "xbox",
    "ایکس باکس": "xbox",
    "pc": "کامپیوتر",
    "پی سی": "کامپیوتر",
    "راک استار": "rockstar",
    "راکستار": "rockstar",
    "جی تی ای": "gta",
    "جی تی ای 6": "gta6",
    "جی تی ای vi": "gta6",
    "ویچر": "witcher",
    "دث استرندینگ": "deathstranding",
    "کالاف دیوتی": "callofduty",
    "کال آف دیوتی": "callofduty",
    "سونی": "sony",
    "مایکروسافت": "microsoft",
    "نینتندو": "nintendo",
}

EVENT_TYPES = [
    "سیستم پیشنهادی", "سیستم مورد نیاز", "تاریخ انتشار", "تریلر",
    "ویدیو", "تاخیر", "شایعه", "کد تخفیف", "آپدیت", "پچ", "قیمت", "فروش", "مدت زمان"
]

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
# SAFE TELEGRAM MESSAGE SENDER (PREVENT ENTITY PARSE ERRORS)
# ============================================================

async def safe_reply_text(message, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        return await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "parse entities" in str(e).lower() or "entity" in str(e).lower():
            logger.warning("HTML parsing failed, falling back to plain text.")
            clean_text = re.sub(r'<[^>]+>', '', text)
            return await message.reply_text(clean_text, reply_markup=reply_markup, parse_mode=None)
        raise e

async def safe_edit_text(message, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        return await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "parse entities" in str(e).lower() or "entity" in str(e).lower():
            logger.warning("HTML parsing failed during edit, falling back to plain text.")
            clean_text = re.sub(r'<[^>]+>', '', text)
            return await message.edit_text(clean_text, reply_markup=reply_markup, parse_mode=None)
        raise e

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
# PERSIAN GAMING PRE-PROCESSING & STOPWORDS
# ============================================================

PERSIAN_ARABIC_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

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

PERSIAN_STOPWORDS = {
    "در", "یک", "از", "به", "با", "که", "را", "روی", "بر", "برای", "شد", "کرد",
    "است", "بود", "این", "آن", "هم", "نیز", "تا", "چون", "باید", "ستاره",
    "جدید", "اعلام", "انتشار", "تاریخ", "تریلر", "پوستر", "ویدیو", "تصویر",
    "عکس", "دانلود", "تماشا", "کنید", "رسما", "رسمی", "شایعه", "تایید",
    "پوشش", "اخبار", "خبر", "وجود", "داد"
}

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
    text = re.sub(r"\s+", " ", text).strip().lower()

    for alias, standard in GAME_ALIASES.items():
        text = re.sub(r'\b' + re.escape(alias) + r'\b', standard, text)

    return text

def clean_gaming_text(text: str) -> str:
    if not text:
        return ""
    text = BADGES_PATTERN.sub(" ", text)
    text = BRANDING_PATTERN.sub(" ", text)
    text = DECORATION_PATTERN.sub(" ", text)
    return normalize(text)

# ============================================================
# METADATA EXTRACTION & ENTITY MATCHING
# ============================================================

def extract_metadata(text: str) -> Dict[str, Any]:
    cleaned = clean_gaming_text(text)
    found_events = [event for event in EVENT_TYPES if event in cleaned]
    entities = extract_entities(text)
    return {
        "events": found_events,
        "entities": entities
    }

def extract_entities(text: str) -> set:
    if not text:
        return set()
    cleaned = clean_gaming_text(text)
    words = cleaned.split()
    
    persian_entities = set(w for w in words if w not in PERSIAN_STOPWORDS and len(w) >= 2)
    eng_entities = set(w.lower() for w in re.findall(r'\b[a-zA-Z0-9]{1,}\b', text))
    
    return persian_entities | eng_entities

def entity_overlap_score(text1: str, text2: str) -> float:
    e1 = extract_entities(text1)
    e2 = extract_entities(text2)
    if not e1 or not e2:
        return 0.0
    intersection = e1 & e2
    return len(intersection) / min(len(e1), len(e2))

def token_overlap_ratio(text1: str, text2: str) -> float:
    words1 = extract_entities(text1)
    words2 = extract_entities(text2)
    if not words1 or not words2:
        return 0.0
    intersection = words1 & words2
    return len(intersection) / max(len(words1), len(words2))

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
# ROBUST IMAGE PERCEPTUAL HASHING
# ============================================================

def compute_image_hash(image_bytes: bytes) -> str:
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert('L')
        w, h = img.size
        img = img.crop((int(w * 0.1), int(h * 0.1), int(w * 0.9), int(h * 0.9)))
        img = img.resize((9, 8), Image.Resampling.LANCZOS)
        
        pixels = list(img.getdata())
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
    if not h1 or not h2 or len(h1) != len(h2):
        return 999
    return sum(c1 != c2 for c1, c2 in zip(h1, h2))

# ============================================================
# VECTOR SEARCH & PURE PYTHON MATH
# ============================================================

def sequence_similarity(a: str, b: str) -> float:
    a, b = clean_gaming_text(a), clean_gaming_text(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()

def word_jaccard(a: str, b: str) -> float:
    set_a = extract_entities(a)
    set_b = extract_entities(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)

def text_similarity(a: str, b: str) -> float:
    seq = sequence_similarity(a, b)
    jaccard = word_jaccard(a, b)
    return (seq * 0.50) + (jaccard * 0.50)

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

def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_v1 = math.sqrt(sum(a * a for a in v1))
    norm_v2 = math.sqrt(sum(b * b for b in v2))
    if norm_v1 == 0 or norm_v2 == 0:
        return 0.0
    return dot_product / (norm_v1 * norm_v2)

def batch_cosine_similarity(query_vector: List[float], vectors: List[List[float]]) -> List[float]:
    if not vectors or not query_vector:
        return []
    return [cosine_similarity(query_vector, v) for v in vectors]

# ============================================================
# VISION AI FOR SHORT TEXT
# ============================================================

async def analyze_image_content(image_bytes: bytes) -> str:
    if not openai_client:
        return ""
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        response = await openai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "متن و موضوع اصلی موجود در این تصویر خبری گیمینگ را به صورت خلاصه توضیح بده:"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                        }
                    ]
                }
            ],
            max_tokens=300
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.error("Vision AI error: %s", e)
        return ""

# ============================================================
# FEW-SHOT PROMPTING AI JUDGE
# ============================================================

async def ai_compare(new_text: str, old_text: str) -> Optional[AIResult]:
    if not openai_client:
        return None

    system_prompt = """
تو موتور هوشمند تشخیص اخبار تکراری رسانه گیمفا هستی.
تکلیف: تشخیص بده آیا این دو خبر مربوط به یک موضوع، رویداد، مصاحبه یا واکنش یکسان هستند یا خیر.

قوانین تشخیص:
۱. بازنویسی تیترها (Paraphrasing): اگر دو خبر در مورد یک مصاحبه، یک شخص (مثلا خالق بازی) یا یک رویداد یکسان صحبت می‌کنند (حتی اگر یکی کلی‌تر و دیگری جزیی‌تر باشد)، این اخبار duplicate = true هستند.
۲. نظر و واکنش: اگر هر دو خبر درباره نظر یک شخص درباره یک بازی/بتا هستند، حتماً تکراری محسوب می‌شوند.
۳. پوشش موازی: اگر خبر دوم تکمیل‌کننده یا تیتر دیگری از همان مصاحبه/خبر باشد، is_update = true و duplicate = true بگذار.
۴. اسامی و اعداد: اگر نام شرکت/بازی و عدد کلیدی خبر یکسان باشد، حتماً تکراری است.
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
# CANDIDATES SELECTION
# ============================================================

def get_candidates_sync(new_text: str, new_embedding: Optional[List[float]]):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (ARCHIVE_SIZE,)).fetchall()

    if not rows:
        return []

    candidates = []
    new_title = extract_title(new_text)
    clean_new = clean_gaming_text(new_text)
    clean_title = clean_gaming_text(new_title)
    new_meta = extract_metadata(new_text)

    valid_vectors = []
    vector_row_indices = []

    for idx, row in enumerate(rows):
        if row["embedding"]:
            try:
                valid_vectors.append(json.loads(row["embedding"]))
                vector_row_indices.append(idx)
            except Exception:
                pass

    semantics = [0.0] * len(rows)
    if new_embedding and valid_vectors:
        sim_scores = batch_cosine_similarity(new_embedding, valid_vectors)
        for idx, score in zip(vector_row_indices, sim_scores):
            semantics[idx] = score

    for idx, row in enumerate(rows):
        old_text = row["text"]
        clean_old = row["normalized"]
        clean_old_title = clean_gaming_text(row["title"] or "")

        lexical = text_similarity(new_text, old_text)
        seq_title_score = sequence_similarity(new_title, row["title"] or "")
        token_title_score = token_overlap_ratio(new_title, row["title"] or "")
        title_score = max(seq_title_score, token_title_score)

        ner_score = entity_overlap_score(new_text, old_text)

        cross_title = 0.0
        if clean_title and (clean_title in clean_old or clean_old_title in clean_new):
            cross_title = 0.90

        effective_title = max(title_score, cross_title)
        semantic = semantics[idx]

        old_meta = extract_metadata(old_text)
        meta_boost = 0.0
        if new_meta["events"] and old_meta["events"]:
            if set(new_meta["events"]) & set(old_meta["events"]):
                meta_boost = 0.15

        ranking = ((effective_title * 0.35) + (semantic * 0.35) + (ner_score * 0.20) + (lexical * 0.10)) + meta_boost
        
        if ner_score >= 0.40:
            ranking += 0.20

        candidates.append((ranking, semantic, lexical, ner_score, row))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[:MAX_AI_CANDIDATES]

# ============================================================
# PIPELINE MAIN CHECK
# ============================================================

async def check_duplicate(text: str, image_hash: Optional[str] = None, image_bytes: Optional[bytes] = None) -> Dict[str, Any]:
    if image_bytes and len(text.split()) < 5:
        vision_text = await analyze_image_content(image_bytes)
        if vision_text:
            text = f"{text}\n[توضیحات تصویر]: {vision_text}"

    cleaned = clean_gaming_text(text)
    fingerprint = sha256_hash(text)
    url = get_article_url(text)

    def fast_checks():
        with get_db() as conn:
            row = conn.execute("SELECT * FROM news WHERE sha256 = ? LIMIT 1", (fingerprint,)).fetchone()
            if row:
                return {"duplicate": True, "reason": "exact_hash", "confidence": 1.0, "row": row}

            if url:
                row = conn.execute("SELECT * FROM news WHERE url = ? LIMIT 1", (url,)).fetchone()
                if row:
                    return {"duplicate": True, "reason": "exact_url", "confidence": 1.0, "row": row}

            if image_hash:
                img_rows = conn.execute("SELECT * FROM news WHERE image_hash IS NOT NULL AND image_hash != '' ORDER BY id DESC LIMIT 50").fetchall()
                for r in img_rows:
                    dist = hamming_distance(image_hash, r["image_hash"])
                    if dist <= 6:
                        return {"duplicate": True, "reason": "image_match", "confidence": 0.95, "row": r}

            recent_rows = conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT 50").fetchall()
            for r in recent_rows:
                overlap = entity_overlap_score(text, r["text"])
                jaccard = word_jaccard(text, r["text"])
                if overlap >= 0.75 and jaccard >= 0.50:
                    return {"duplicate": True, "reason": "near_exact_text", "confidence": 0.95, "row": r}

        return None

    quick_res = await asyncio.to_thread(fast_checks)
    if quick_res:
        return quick_res

    embedding = await make_embedding(cleaned)
    candidates = await asyncio.to_thread(get_candidates_sync, text, embedding)

    tasks = []
    candidate_meta = []

    for ranking, semantic, lexical, ner_score, row in candidates:
        if ranking < 0.05:
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
                
            if result.duplicate and (result.same_event or result.same_claim) and conf >= 0.70:
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

def format_old_news_preview(row) -> str:
    if not row:
        return ""
    old_text = row["text"].strip()
    preview = old_text[:200] + "..." if len(old_text) > 200 else old_text
    escaped_preview = html.escape(preview)
    return f"\n\n📌 <b>خبر قبلی موجود در آرشیو:</b>\n«{escaped_preview}»"

# ============================================================
# TELEGRAM HANDLERS (WITH PERMISSION & FALLBACK)
# ============================================================

def is_allowed(update: Update) -> bool:
    if not ADMIN_IDS or 0 in ADMIN_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    await safe_reply_text(
        update.message,
        "🤖 <b>ربات پیشرفته تشخیص خبر تکراری گیمفا</b>\n\n"
        "متن یا تصویر خبر را بفرستید تا آنالیز ۶ لایه‌ای انجام شود:",
        reply_markup=MAIN_KEYBOARD
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    text = (update.message.text or update.message.caption or "").strip()
    photo = update.message.photo

    if text in ["📊 آمار آرشیو", "📦 وضعیت دیتابیس"]:
        with get_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        await safe_reply_text(update.message, f"📊 <b>وضعیت دیتابیس و آرشیو:</b>\n\nتعداد اخبار ذخیره‌شده: <code>{total}/{ARCHIVE_SIZE}</code>")
        return

    elif text == "🗑 پاکسازی کامل آرشیو":
        if user_id != OWNER_ID:
            await safe_reply_text(
                update.message,
                "⛔ <b>دسترسی غیرمجاز!</b>\nعملیات پاکسازی کامل آرشیو فقط برای مالک اصلی ربات مجاز است."
            )
            return

        with get_db() as conn:
            conn.execute("DELETE FROM news")
            conn.commit()
        await safe_reply_text(update.message, "🗑 <b>آرشیو دیتابیس با موفقیت پاکسازی شد.</b>", reply_markup=MAIN_KEYBOARD)
        return

    elif text in ["📋 راهنما", "🔍 بررسی خبر جدید"]:
        await safe_reply_text(
            update.message,
            "ℹ️ <b>راهنمای استفاده از ربات:</b>\n\n"
            "• کافیست متن یا تصویر پست جدید تلگرام را ارسال کنید.\n"
            "• ربات آن را در ۶ لایه آنالیز کرده و اسامی گیمینگ، پلتفرم‌ها و کاور را تطبیق می‌دهد.\n"
            "• در صورت تکراری بودن، متن خبر قبلی آرشیو شده به همراه دلیل نمایش داده خواهد شد."
        )
        return

    elif text == "🧠 وضعیت AI":
        status_ai = "🟢 فعال" if openai_client else "🔴 غیرفعال (کلید API تنظیم نشده)"
        await safe_reply_text(
            update.message,
            f"🧠 <b>وضعیت موتور هوش مصنوعی:</b>\n\n"
            f"• وضعیت اتصال: {status_ai}\n"
            f"• مدل پردازش متنی و Vision: <code>{AI_MODEL}</code>\n"
            f"• مدل Embedding: <code>{EMBEDDING_MODEL}</code>"
        )
        return

    elif text == "⚙️ تنظیمات":
        await safe_reply_text(
            update.message,
            f"⚙️ <b>تنظیمات فعلی ربات:</b>\n\n"
            f"• حداکثر ظرفیت آرشیو: <code>{ARCHIVE_SIZE}</code> خبر\n"
            f"• شناسه مالک اصلی: <code>{OWNER_ID}</code>\n"
            f"• مدل هوش مصنوعی: <code>{AI_MODEL}</code>"
        )
        return

    elif text == "💬 پشتیبانی":
        await safe_reply_text(update.message, "💬 <b>پشتیبانی و ارتباط:</b>\n\nجهت گزارش مشکلات با توسعه‌دهنده در ارتباط باشید.")
        return

    elif text == "👥 ادمین‌ها":
        admins_str = ", ".join(map(str, ADMIN_IDS))
        await safe_reply_text(update.message, f"👥 <b>مدیریت دسترسی:</b>\n\n• مالک اصلی: <code>{OWNER_ID}</code>\n• شناسه ادمین‌ها: <code>{admins_str}</code>")
        return

    elif text == "📜 قوانین":
        await safe_reply_text(
            update.message,
            "📜 <b>قوانین و ضوابط بررسی اخبار:</b>\n\n"
            "۱. اخبار تکراری حتی با تغییر کلمات، بازنویسی یا نام‌های مترادف شناسایی خواهند شد.\n"
            "۲. اخبار تکمیلی و بروزرسانی‌ها (Update) به عنوان خبر تکراری شناسایی می‌شوند."
        )
        return

    image_hash = None
    image_bytes = None

    if photo:
        file = await context.bot.get_file(photo[-1].file_id)
        img_bytearray = await file.download_as_bytearray()
        image_bytes = bytes(img_bytearray)
        image_hash = compute_image_hash(image_bytes)

    if not text and not image_hash:
        return

    status = await safe_reply_text(update.message, "🔎 در حال آنالیز ۶ لایه‌ای (کاور، اسامی گیمینگ، متادیتا و AI)...")

    try:
        result = await check_duplicate(text, image_hash, image_bytes)

        if result["duplicate"]:
            conf = result["confidence"] * 100
            reason = result["reason"]
            old_preview = format_old_news_preview(result.get("row"))

            if reason in ["exact_hash", "exact_url", "near_exact_text", "title_exact_match"]:
                await safe_edit_text(status, f"♻️ <b>خبر کاملاً تکراری است</b> (اطمینان بالا){old_preview}\n\n⛔ خبر ذخیره نشد.")
                return

            elif reason == "image_match":
                await safe_edit_text(status, f"🖼 <b>کاور/تصویر خبر تکراری است</b>{old_preview}\n\n⛔ خبر ذخیره نشد.")
                return

            elif reason == "ai_update":
                explanation = html.escape(result.get("explanation", ""))
                await safe_edit_text(
                    status,
                    f"ℹ️ <b>این پست بروزرسانی / پوشش تکمیلی خبر قبلی است</b>\n\n"
                    f"🎯 درصد شباهت: {conf:.1f}%\n"
                    f"💡 تحلیل AI: {explanation}"
                    f"{old_preview}\n\n"
                    f"⛔ به عنوان خبر مستقل ذخیره نشد."
                )
                return

            elif reason == "ai_high_confidence":
                explanation = html.escape(result.get("explanation", ""))
                await safe_edit_text(
                    status,
                    f"♻️ <b>خبر تکراری است</b>\n\n"
                    f"🎯 درصد اطمینان: {conf:.1f}%\n"
                    f"💡 دلیل AI: {explanation}"
                    f"{old_preview}\n\n"
                    f"⛔ خبر ذخیره نشد."
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
                explanation = html.escape(result.get("explanation", ""))
                await safe_edit_text(
                    status,
                    f"⚠️ <b>نیازمند بررسی ادمین</b>\n\n"
                    f"🎯 درصد شباهت: {conf:.1f}%\n"
                    f"💡 تحلیل AI: {explanation}"
                    f"{old_preview}\n\n"
                    f"آیا خبر ذخیره شود؟",
                    reply_markup=keyboard
                )
                return

        total = await save_news(text, image_hash)
        await safe_edit_text(
            status,
            f"🆕 <b>خبر جدید است</b>\n\n"
            f"✅ ذخیره شد.\n"
            f"📦 آرشیو: {total}/{ARCHIVE_SIZE}"
        )

    except Exception as e:
        logger.exception("Processing Error")
        escaped_err = html.escape(str(e))
        await safe_edit_text(status, f"❌ خطا در پردازش:\n<code>{escaped_err}</code>")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if query.data == "force_save":
        text = context.user_data.get("pending_news")
        image_hash = context.user_data.get("pending_image_hash")

        if text:
            total = await save_news(text, image_hash)
            await safe_edit_text(
                query.message,
                f"✅ <b>خبر با موفقیت ذخیره شد.</b>\n\n"
                f"📦 ظرفیت آرشیو: {total}/{ARCHIVE_SIZE}"
            )
            context.user_data.pop("pending_news", None)
            context.user_data.pop("pending_image_hash", None)
        else:
            await safe_edit_text(query.message, "❌ <b>اطلاعات خبر یافت نشد یا منقضی شده است.</b>")

    elif query.data == "force_discard":
        context.user_data.pop("pending_news", None)
        context.user_data.pop("pending_image_hash", None)
        await safe_edit_text(query.message, "🗑 <b>خبر تکراری تشخیص داده شد و ذخیره نگردید.</b>")

# ============================================================
# MAIN ENTRY POINT
# ============================================================

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN یافت نشد! لطفاً متغیر محیطی BOT_TOKEN را تنظیم کنید.")
        return

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
            handle_message
        )
    )

    logger.info("ربات تشخیص خبر تکراری گیمفا با موفقیت روشن شد...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
