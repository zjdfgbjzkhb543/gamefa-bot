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
from enum import Enum
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple, Set
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

DEFAULT_ADMIN_IDS = [int(i.strip()) for i in os.getenv("ADMIN_ID", "0").split(",") if i.strip().isdigit()]
OWNER_ID = 8202357756

DB_FILE = os.getenv("DB_FILE", "gamefa_duplicate.db")
ARCHIVE_SIZE = int(os.getenv("ARCHIVE_SIZE", "150"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

# [ایده ۳]: مدل ارزان/سریع برای ارزیابی اولیه + مدل اصلی برای ارزیابی ثانویه
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o")
FAST_AI_MODEL = os.getenv("FAST_AI_MODEL", "gpt-4o-mini")

# [ایده ۱]: بازه زمانی اعتبار اخبار (بر حسب روز)
MAX_NEWS_AGE_DAYS = int(os.getenv("MAX_NEWS_AGE_DAYS", "14"))
MAX_AI_CANDIDATES = int(os.getenv("MAX_AI_CANDIDATES", "8"))

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
# MAIN KEYBOARD LAYOUT
# ============================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔍 بررسی خبر جدید"), KeyboardButton("📊 آمار آرشیو")],
        [KeyboardButton("🧠 وضعیت هوش مصنوعی"), KeyboardButton("📋 راهنما")],
        [KeyboardButton("⚙️ تنظیمات سیستم"), KeyboardButton("👥 لیست مدیران")],
        [KeyboardButton("🗑 پاکسازی کامل آرشیو")]
    ],
    resize_keyboard=True
)

# ============================================================
# LOGGING & OPENAI CLIENT
# ============================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("gamefa-ui-engine")

openai_client: Optional[AsyncOpenAI] = None
if OPENAI_API_KEY:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ============================================================
# SAFE TELEGRAM MESSAGE SENDER
# ============================================================

async def safe_reply_text(message, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        return await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "parse entities" in str(e).lower() or "entity" in str(e).lower():
            clean_text = re.sub(r'<[^>]+>', '', text)
            return await message.reply_text(clean_text, reply_markup=reply_markup, parse_mode=None)
        raise e

async def safe_edit_text(message, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        return await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "parse entities" in str(e).lower() or "entity" in str(e).lower():
            clean_text = re.sub(r'<[^>]+>', '', text)
            return await message.edit_text(clean_text, reply_markup=reply_markup, parse_mode=None)
        raise e

# ============================================================
# AI RESPONSE SCHEMA
# ============================================================

class MatchType(str, Enum):
    EXACT_DUPLICATE = "exact_duplicate"
    UPDATE_COVERAGE = "update_coverage"
    DIFFERENT_NEWS = "different_news"

class AIResult(BaseModel):
    subject_entity: str = Field(description="نام بازی، شخص، رسانه یا کمپانی اصلی خبر")
    core_event_summary: str = Field(description="خلاصه رویداد، مصاحبه یا ادعای اصلی")
    is_same_subject_and_event: bool = Field(description="آیا هر دو خبر درباره یک شخص/رویداد/مصاحبه/بازی یکسان صحبت می‌کنند؟")
    has_numerical_update: bool = Field(description="آیا آمار، قیمت یا تاریخ کلیدی خبر دوم نسبت به خبر اول تغییر کرده است؟")
    match_type: MatchType = Field(description="دسته‌بندی دقیق ارتباط دو خبر")
    duplicate: bool = Field(description="آیا خبر دوم باید به عنوان تکراری/موازی مسدود شود؟")
    confidence: float = Field(description="میزان اطمینان بین 0.0 تا 1.0")
    explanation: str = Field(description="تحلیل و دلیل نهایی به زبان فارسی")

# ============================================================
# DATABASE SETUP & FEEDBACK TABLE [ایده ۴]
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
                summary TEXT,
                sha256 TEXT UNIQUE,
                image_hash TEXT,
                embedding TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # [ایده ۴]: جدول ذخیره بازخورد و تصمیمات دستی ادمین برای آموزش Few-Shot
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                new_text TEXT NOT NULL,
                old_text TEXT NOT NULL,
                admin_action TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sha256 ON news(sha256)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_url ON news(url)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_image_hash ON news(image_hash)")

        initial_admins = set(DEFAULT_ADMIN_IDS + [OWNER_ID])
        for aid in initial_admins:
            if aid != 0:
                conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (aid,))
        conn.commit()

def get_db_admin_ids() -> List[int]:
    with get_db() as conn:
        rows = conn.execute("SELECT user_id FROM admins").fetchall()
        admins = [r["user_id"] for r in rows]
        if OWNER_ID not in admins:
            admins.append(OWNER_ID)
        return admins

def add_admin_db(user_id: int) -> bool:
    with get_db() as conn:
        try:
            conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error adding admin: {e}")
            return False

def remove_admin_db(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return False
    with get_db() as conn:
        conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        return True

def is_allowed(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    return user.id in get_db_admin_ids()

# [ایده ۴]: ذخیره بازخورد ادمین
def record_feedback(new_text: str, old_text: str, action: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO feedback (new_text, old_text, admin_action) VALUES (?, ?, ?)",
            (new_text[:1000], old_text[:1000], action)
        )
        conn.commit()

# [ایده ۴]: دریافت آخرین نمونه‌های بازخورد ادمین برای تزریق به پروامپت
def get_few_shot_examples() -> str:
    with get_db() as conn:
        rows = conn.execute("SELECT new_text, old_text, admin_action FROM feedback ORDER BY id DESC LIMIT 3").fetchall()
    if not rows:
        return ""
    examples = "\n\n[نمونه تصمیمات قبلی ادمین برای یادگیری الگوی تصمیم‌گیری]:\n"
    for r in rows:
        action_desc = "تکراری نیست و باید ذخیره شود (خبر جدید یا تغییر آمار)" if r["admin_action"] == "force_saved" else "تکراری است و رد شد"
        examples += f"- خبر جدید: «{r['new_text'][:120]}...»\n  خبر آرشیو: «{r['old_text'][:120]}...»\n  تصمیم ادمین: {action_desc}\n\n"
    return examples

# ============================================================
# PERSIAN GAMING PRE-PROCESSING & EXTRACTION ENGINE
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

def extract_numbers(text: str) -> Set[str]:
    if not text:
        return set()
    text_norm = text.translate(PERSIAN_ARABIC_DIGITS)
    nums = re.findall(r'\b\d+(?:\.\d+)?\b', text_norm)
    return set(nums)

def extract_entities(text: str) -> Set[str]:
    if not text:
        return set()
    cleaned = clean_gaming_text(text)
    words = cleaned.split()
    persian_entities = set(w for w in words if w not in PERSIAN_STOPWORDS and len(w) >= 3)
    eng_entities = set(w.lower() for w in re.findall(r'\b[a-zA-Z0-9]{2,}\b', text))
    return persian_entities | eng_entities

def extract_metadata(text: str) -> Dict[str, Any]:
    cleaned = clean_gaming_text(text)
    found_events = [event for event in EVENT_TYPES if event in cleaned]
    entities = extract_entities(text)
    numbers = extract_numbers(text)
    return {
        "events": found_events,
        "entities": entities,
        "numbers": numbers
    }

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
# IMAGE HASHING & VISION OCR [ایده ۲]
# ============================================================

def compute_image_hash(image_bytes: bytes) -> str:
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert('L')
        w, h = img.size
        img = img.crop((int(w * 0.15), int(h * 0.15), int(w * 0.85), int(h * 0.85)))
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

# [ایده ۲]: استخراج تمام متن‌ها و مفاهیم بصری پوستر
async def analyze_image_content(image_bytes: bytes) -> str:
    if not openai_client:
        return ""
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        response = await openai_client.chat.completions.create(
            model=FAST_AI_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "تمام متن‌ها، تیترها، کاراکترها و موضوعات گیمینگ موجود در این پوستر/تصویر را به دقت استخراج کن:"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                        }
                    ]
                }
            ],
            temperature=0.0,
            max_tokens=300
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.error("Vision AI error: %s", e)
        return ""

# ============================================================
# HyDE & VECTOR SEARCH ENGINE [ایده ۳ - HyDE]
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

# [ایده ۳]: تکنیک HyDE (ساخت چکیده موضوعی قبل از ایمبدینگ)
async def generate_hyde_summary(text: str) -> str:
    if not openai_client:
        return text[:300]
    try:
        response = await openai_client.chat.completions.create(
            model=FAST_AI_MODEL,
            messages=[
                {"role": "system", "content": "یک چکیده یک‌جمله‌ای بسیار فشرده شامل (سوژه اصلی + ادعا/رویداد اصلی خبر) بنویس."},
                {"role": "user", "content": text[:3000]}
            ],
            max_tokens=100,
            temperature=0.0
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return text[:300]

async def make_embedding(text: str) -> Optional[List[float]]:
    if not openai_client:
        return None
    try:
        # استفاده از چکیده HyDE برای بالا بردن دقت شباهت برداری
        hyde_text = await generate_hyde_summary(text)
        response = await openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=clean_gaming_text(hyde_text)[:10000]
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
# TWO-STAGE AI COMPARATOR ENGINE [ایده ۳ و ایده ۴]
# ============================================================

async def ai_compare(new_text: str, old_text: str) -> Optional[AIResult]:
    if not openai_client:
        return None

    few_shot_context = get_few_shot_examples()

    system_prompt = f"""
شما موتوری فوق‌العاده حساس و دقیق برای تشخیص اخبار تکراری در رسانه گیمفا هستید.

دستورالعمل‌های اصلی:
۱. تطابق موضوعی (Subject Matching): اگر هر دو خبر درباره یک شخص/بازی/مصاحبه یکسان صحبت می‌کنند، duplicate = true است.
۲. تغییر تیتر و بازنویسی (Paraphrasing): تغییر کلمات یا تیترها نباید باعث عبور خبر شود.
۳. بررسی اعداد (Numerical Guard): اگر اعداد کلیدی (فروش، قیمت، تاریخ) تغییر کرده باشند، has_numerical_update را true بگذارید و match_type = "update_coverage" قرار دهید.
۴. دسته‌بندی ارتباط:
   - exact_duplicate: رویداد دقیقاً یکسان.
   - update_coverage: آپدیت آمار، خبر یا مصاحبه قبلی.
   - different_news: کاملاً دو خبر متفاوت.
{few_shot_context}
"""

    user_prompt = f"[خبر جدید]:\n{new_text[:4000]}\n\n==================\n\n[خبر آرشیوی]:\n{old_text[:4000]}"

    try:
        # مرحله ۱: تحلیل سریع و ارزان با gpt-4o-mini
        fast_response = await openai_client.beta.chat.completions.parse(
            model=FAST_AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.0,
            response_format=AIResult
        )
        fast_result = fast_response.choices[0].message.parsed

        # مرحله ۲: اگر پاسخ مرزی/مشکوک بود یا آپدیت عددی داشت، ارسال به gpt-4o برای تصمیم‌گیری نهایی
        if 0.45 <= fast_result.confidence <= 0.82 or fast_result.has_numerical_update:
            main_response = await openai_client.beta.chat.completions.parse(
                model=AI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.0,
                response_format=AIResult
            )
            return main_response.choices[0].message.parsed

        return fast_result
    except Exception as e:
        logger.exception("AI Engine Compare error: %s", e)
        return None

# ============================================================
# CANDIDATE SELECTION WITH TEMPORAL & NER GUARDS [ایده ۱]
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
    now = datetime.now(timezone.utc)

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

        # [ایده ۱]: جریمه زمانی اخبار قدیمی
        created_at_str = row["created_at"]
        time_penalty = 1.0
        if created_at_str:
            try:
                created_dt = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age_days = (now - created_dt).days
                if age_days > MAX_NEWS_AGE_DAYS:
                    time_penalty = max(0.2, 1.0 - ((age_days - MAX_NEWS_AGE_DAYS) * 0.05))
            except Exception:
                pass

        lexical = text_similarity(new_text, old_text)
        seq_title_score = sequence_similarity(new_title, row["title"] or "")
        token_title_score = token_overlap_ratio(new_title, row["title"] or "")
        title_score = max(seq_title_score, token_title_score)

        ner_score = entity_overlap_score(new_text, old_text)

        # [ایده ۱]: گارد سخت‌گیرانه عدم شباهت اسامی خاص
        if ner_score < 0.15 and len(new_meta["entities"]) >= 2:
            ner_score *= 0.1

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

        ranking = (((effective_title * 0.35) + (semantic * 0.35) + (ner_score * 0.20) + (lexical * 0.10)) + meta_boost) * time_penalty

        if ner_score >= 0.30:
            ranking += 0.25

        candidates.append((ranking, semantic, lexical, ner_score, row))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[:MAX_AI_CANDIDATES]

# ============================================================
# CHECK DUPLICATE PIPELINE
# ============================================================

async def check_duplicate(text: str, image_hash: Optional[str] = None, image_bytes: Optional[bytes] = None) -> Dict[str, Any]:
    # [ایده ۲]: استخراج متن از تصویر در صورت کوتاه بودن پیام
    if image_bytes and len(text.split()) < 5:
        vision_text = await analyze_image_content(image_bytes)
        if vision_text:
            text = f"{text}\n[توضیحات تصویر]: {vision_text}"

    cleaned = clean_gaming_text(text)
    word_count = len(cleaned.split())
    fingerprint = sha256_hash(text)
    url = get_article_url(text)
    new_meta = extract_metadata(text)

    required_conf = 0.60 if word_count >= 30 else 0.75

    def fast_checks():
        with get_db() as conn:
            row = conn.execute("SELECT * FROM news WHERE sha256 = ? LIMIT 1", (fingerprint,)).fetchone()
            if row:
                return {"duplicate": True, "reason": "exact_hash", "confidence": 1.0, "row": row}

            # [ایده ۱]: بررسی منبع خبر و لینک یکسان
            if url:
                row = conn.execute("SELECT * FROM news WHERE url = ? LIMIT 1", (url,)).fetchone()
                if row:
                    return {"duplicate": True, "reason": "exact_url", "confidence": 1.0, "row": row}

            if image_hash:
                img_rows = conn.execute("SELECT * FROM news WHERE image_hash IS NOT NULL AND image_hash != '' ORDER BY id DESC LIMIT 50").fetchall()
                for r in img_rows:
                    dist = hamming_distance(image_hash, r["image_hash"])
                    if dist <= 5:
                        return {"duplicate": True, "reason": "image_match", "confidence": 0.95, "row": r}

            recent_rows = conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT 50").fetchall()
            for r in recent_rows:
                old_meta = extract_metadata(r["text"])
                has_num_conflict = False
                if new_meta["numbers"] and old_meta["numbers"]:
                    if new_meta["numbers"] != old_meta["numbers"]:
                        has_num_conflict = True

                overlap = entity_overlap_score(text, r["text"])
                jaccard = word_jaccard(text, r["text"])

                if not has_num_conflict and overlap >= 0.75 and jaccard >= 0.50:
                    return {"duplicate": True, "reason": "near_exact_text", "confidence": 0.95, "row": r}

        return None

    quick_res = await asyncio.to_thread(fast_checks)
    if quick_res:
        return quick_res

    embedding = await make_embedding(cleaned)
    candidates = await asyncio.to_thread(get_candidates_sync, text, embedding)

    stage1_candidates = [c for c in candidates if c[0] >= 0.20][:3]

    tasks = []
    candidate_meta = []

    for ranking, semantic, lexical, ner_score, row in stage1_candidates:
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
            
            if result.duplicate and (result.match_type == MatchType.UPDATE_COVERAGE or result.has_numerical_update):
                return {
                    "duplicate": True,
                    "reason": "ai_update",
                    "confidence": conf,
                    "row": row,
                    "explanation": result.explanation
                }

            if result.duplicate and conf >= required_conf:
                return {
                    "duplicate": True,
                    "reason": "ai_high_confidence",
                    "confidence": conf,
                    "row": row,
                    "explanation": result.explanation
                }
            
            if result.duplicate and conf >= (required_conf - 0.10):
                return {
                    "duplicate": True,
                    "reason": "ai_ambiguous",
                    "confidence": conf,
                    "row": row,
                    "explanation": result.explanation
                }

    return {"duplicate": False, "reason": "new_news", "confidence": 0.0, "row": None}

async def save_news(text: str, image_hash: Optional[str] = None) -> int:
    cleaned = clean_gaming_text(text)
    title = extract_title(text)
    url = get_article_url(text)
    fingerprint = sha256_hash(text)

    summary = await generate_hyde_summary(text)
    embedding = await make_embedding(cleaned)
    embedding_json = json.dumps(embedding, ensure_ascii=False) if embedding else None

    def db_save():
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT INTO news (telegram_id, text, normalized, title, url, summary, sha256, image_hash, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("", text, cleaned, title, url, summary, fingerprint, image_hash or "", embedding_json)
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
    preview = old_text[:220] + "..." if len(old_text) > 220 else old_text
    escaped_preview = html.escape(preview)
    return (
        f"\n\n▫️▪️ <b>خبر مشابه در آرشیو:</b>\n"
        f"<blockquote>«{escaped_preview}»</blockquote>"
    )

# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    welcome_text = (
        "✨ <b>سامانه هوشمند پایش و تشخیص اخبار تکراری گیمفا v3.0</b>\n"
        "─── • 💎 • ───\n\n"
        "مجهز به ۴ زیرسیستم ارتقایافته:\n"
        "🔹 <b>معماری دو مرحله‌ای AI:</b> سرعت بالا با gpt-4o-mini و دقت بالا با gpt-4o\n"
        "🔹 <b>تکنیک HyDE:</b> ایمبدینگ برداری بر اساس چکیده کلیدی خبر\n"
        "🔹 <b>سیستم یادگیری از ادمین:</b> یادگیری الگوهای تحریریه بر اساس تصمیمات شما\n"
        "🔹 <b>بینایی ماشین OCR:</b> تحلیل کامل متن پوسترها"
    )

    await safe_reply_text(update.message, welcome_text, reply_markup=MAIN_KEYBOARD)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    text = (update.message.text or update.message.caption or "").strip()
    photo = update.message.photo

    if context.user_data.get("action") == "await_admin_id":
        if text in ["🔍 بررسی خبر جدید", "📊 آمار آرشیو", "🧠 وضعیت هوش مصنوعی", "📋 راهنما", "⚙️ تنظیمات سیستم", "👥 لیست مدیران", "🗑 پاکسازی کامل آرشیو"]:
            context.user_data.pop("action", None)
        else:
            clean_input = text.strip()
            if not clean_input.isdigit():
                await safe_reply_text(update.message, "❌ لطفاً فقط آیدی عددی کاربر را وارد کنید.")
                return
            
            new_admin_id = int(clean_input)
            success = add_admin_db(new_admin_id)
            context.user_data.pop("action", None)
            
            if success:
                await safe_reply_text(update.message, f"✅ کاربر <code>{new_admin_id}</code> با موفقیت ادمین شد.", reply_markup=MAIN_KEYBOARD)
            else:
                await safe_reply_text(update.message, "❌ خطا در ثبت ادمین.")
            return

    if text in ["📊 آمار آرشیو", "📦 وضعیت دیتابیس"]:
        with get_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
            feedback_count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        
        stat_text = (
            "📊 <b>اطلاعات و وضعیت آرشیو</b>\n"
            "─── • 💎 • ───\n\n"
            f"📦 اخبار آرشیو: <code>{total}</code> از <code>{ARCHIVE_SIZE}</code>\n"
            f"🧠 نمونه‌های یادگیری ادمین: <code>{feedback_count}</code> مورد"
        )
        await safe_reply_text(update.message, stat_text)
        return

    elif text == "🗑 پاکسازی کامل آرشیو":
        if user_id != OWNER_ID:
            await safe_reply_text(update.message, "🚫 این دستور فقط مخصوص مالک سیستم است.")
            return

        with get_db() as conn:
            conn.execute("DELETE FROM news")
            conn.commit()

        await safe_reply_text(update.message, "🗑 آرشیو اخبار کاملاً پاکسازی شد.", reply_markup=MAIN_KEYBOARD)
        return

    elif text in ["📋 راهنما", "🔍 بررسی خبر جدید"]:
        guide_text = (
            "📋 <b>راهنمای عملکرد سامانه:</b>\n"
            "─── • 💎 • ───\n\n"
            "متن خبر یا تصویر پوستر را ارسال کنید. سیستم با تحلیل چندلایه‌ای و گارد عددی/اسامی خاص، تکراری بودن خبر را مشخص می‌کند."
        )
        await safe_reply_text(update.message, guide_text)
        return

    elif text == "🧠 وضعیت هوش مصنوعی":
        status_ai = "🟢 فعال" if openai_client else "🔴 غیرفعال"
        ai_text = (
            "🧠 <b>مشخصات موتور هوش مصنوعی</b>\n"
            "─── • 💎 • ───\n\n"
            f"🔹 <b>وضعیت:</b> {status_ai}\n"
            f"🔹 <b>مدل سریع:</b> <code>{FAST_AI_MODEL}</code>\n"
            f"🔹 <b>مدل دقیق:</b> <code>{AI_MODEL}</code>\n"
            f"🔹 <b>تکنیک جستجو:</b> <code>HyDE + Cosine Similarity</code>"
        )
        await safe_reply_text(update.message, ai_text)
        return

    elif text == "⚙️ تنظیمات سیستم":
        settings_text = (
            "⚙️ <b>تنظیمات زیرساخت</b>\n"
            "─── • 💎 • ───\n\n"
            f"🔸 <b>سقف آرشیو:</b> <code>{ARCHIVE_SIZE}</code>\n"
            f"🔸 <b>بازه اعتبار:</b> <code>{MAX_NEWS_AGE_DAYS} روز</code>\n"
            f"🔸 <b>مالک سیستم:</b> <code>{OWNER_ID}</code>"
        )
        await safe_reply_text(update.message, settings_text)
        return

    elif text == "👥 لیست مدیران":
        admins = get_db_admin_ids()
        admins_str = "\n".join([f"• <code>{aid}</code>" for aid in admins if aid != OWNER_ID])
        
        admin_text = (
            "👥 <b>لیست مدیران سیستم</b>\n"
            "─── • 💎 • ───\n\n"
            f"👑 <b>مالک:</b>\n• <code>{OWNER_ID}</code>\n\n"
            f"🛡 <b>ادمین‌ها:</b>\n{admins_str if admins_str else '• مديري تعریف نشده است.'}"
        )

        buttons = []
        if user_id == OWNER_ID:
            buttons.append([
                InlineKeyboardButton("➕ افزودن مدیر", callback_data="admin_add_prompt"),
                InlineKeyboardButton("❌ حذف مدیر", callback_data="admin_remove_menu")
            ])

        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        await safe_reply_text(update.message, admin_text, reply_markup=keyboard)
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

    status = await safe_reply_text(update.message, "⏳ <b>در حال آنالیز دو مرحله‌ای و استدلال هوشمند...</b>")

    try:
        result = await check_duplicate(text, image_hash, image_bytes)

        if result["duplicate"]:
            conf = result["confidence"] * 100
            reason = result["reason"]
            row = result.get("row")
            old_preview = format_old_news_preview(row)

            context.user_data["pending_news"] = text
            context.user_data["pending_image_hash"] = image_hash
            if row:
                context.user_data["matched_old_text"] = row["text"]

            force_save_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 با این حال در آرشیو ذخیره شود", callback_data="force_save")]
            ])

            if reason in ["exact_hash", "exact_url", "near_exact_text"]:
                await safe_edit_text(
                    status,
                    f"⛔ <b>خبر کاملاً تکراری است</b>\n─── • 💎 • ───{old_preview}",
                    reply_markup=force_save_keyboard
                )
                return

            elif reason == "image_match":
                await safe_edit_text(
                    status,
                    f"🖼 <b>پوستر خبر تکراری است</b>\n─── • 💎 • ───{old_preview}",
                    reply_markup=force_save_keyboard
                )
                return

            elif reason == "ai_update":
                explanation = html.escape(result.get("explanation", ""))
                await safe_edit_text(
                    status,
                    f"🔄 <b>پوشش موازی / آپدیت آمار و خبر قبلی</b>\n"
                    f"─── • 💎 • ───\n\n"
                    f"🎯 <b>اطمینان سیستم:</b> <code>{conf:.1f}%</code>\n"
                    f"💡 <b>استدلال:</b> {explanation}{old_preview}",
                    reply_markup=force_save_keyboard
                )
                return

            elif reason == "ai_high_confidence":
                explanation = html.escape(result.get("explanation", ""))
                await safe_edit_text(
                    status,
                    f"⛔ <b>خبر تکراری تشخیص داده شد</b>\n"
                    f"─── • 💎 • ───\n\n"
                    f"🎯 <b>اطمینان سیستم:</b> <code>{conf:.1f}%</code>\n"
                    f"💡 <b>استدلال:</b> {explanation}{old_preview}",
                    reply_markup=force_save_keyboard
                )
                return

            elif reason == "ai_ambiguous":
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ ثبت (خبر جدید)", callback_data="force_save"),
                        InlineKeyboardButton("❌ رد (خبر تکراری)", callback_data="force_discard")
                    ]
                ])
                explanation = html.escape(result.get("explanation", ""))
                await safe_edit_text(
                    status,
                    f"⚠️ <b>نیازمند بررسی ادمین</b>\n"
                    f"─── • 💎 • ───\n\n"
                    f"🎯 <b>درصد تشابه:</b> <code>{conf:.1f}%</code>\n"
                    f"💡 <b>استدلال:</b> {explanation}{old_preview}\n\n"
                    f"تصمیم نهایی را انتخاب کنید:",
                    reply_markup=keyboard
                )
                return

        total = await save_news(text, image_hash)
        await safe_edit_text(
            status,
            f"✅ <b>خبر جدید است</b>\n"
            f"─── • 💎 • ───\n\n"
            f"📌 با موفقیت آنالیز و در دیتابیس ثبت گردید.\n"
            f"📦 <b>وضعیت آرشیو:</b> <code>{total}/{ARCHIVE_SIZE}</code>"
        )

    except Exception as e:
        logger.exception("Processing Error")
        escaped_err = html.escape(str(e))
        await safe_edit_text(status, f"❌ <b>خطا در سامانه:</b>\n<code>{escaped_err}</code>")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user_id = update.effective_user.id if update.effective_user else 0

    if query.data == "force_save":
        text = context.user_data.get("pending_news")
        image_hash = context.user_data.get("pending_image_hash")
        old_text = context.user_data.get("matched_old_text", "")

        if text:
            # [ایده ۴]: ذخیره بازخورد ادمین جهت یادگیری Few-Shot
            if old_text:
                record_feedback(text, old_text, "force_saved")

            total = await save_news(text, image_hash)
            await safe_edit_text(
                query.message,
                f"📥 <b>خبر ثبت شد و الگوی تصمیم شما ذخیره گردید.</b>\n"
                f"─── • 💎 • ───\n\n"
                f"📦 <b>وضعیت آرشیو:</b> <code>{total}/{ARCHIVE_SIZE}</code>"
            )
            context.user_data.pop("pending_news", None)
            context.user_data.pop("pending_image_hash", None)
            context.user_data.pop("matched_old_text", None)
        else:
            await safe_edit_text(query.message, "❌ اطلاعات منقضی شده است.")

    elif query.data == "force_discard":
        text = context.user_data.get("pending_news", "")
        old_text = context.user_data.get("matched_old_text", "")
        
        # [ایده ۴]: ذخیره بازخورد رد خبر جهت یادگیری
        if text and old_text:
            record_feedback(text, old_text, "force_discarded")

        context.user_data.pop("pending_news", None)
        context.user_data.pop("pending_image_hash", None)
        context.user_data.pop("matched_old_text", None)
        await safe_edit_text(query.message, "🗑 <b>خبر تکراری تشخیص داده شد و الگوی رد ثبت گردید.</b>")

    elif query.data == "admin_add_prompt":
        if user_id != OWNER_ID:
            await query.answer("🚫 دسترسی محدود.", show_alert=True)
            return
        
        context.user_data["action"] = "await_admin_id"
        await safe_edit_text(query.message, "➕ لطفاً آیدی عددی کاربر جدید را ارسال کنید:")

    elif query.data == "admin_remove_menu":
        if user_id != OWNER_ID:
            await query.answer("🚫 دسترسی محدود.", show_alert=True)
            return

        admins = get_db_admin_ids()
        remove_buttons = [[InlineKeyboardButton(f"❌ حذف {aid}", callback_data=f"admin_del_{aid}")] for aid in admins if aid != OWNER_ID]

        if not remove_buttons:
            await safe_edit_text(query.message, "ℹ️ مديري برای حذف وجود ندارد.")
            return

        await safe_edit_text(query.message, "❌ مدیر مورد نظر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(remove_buttons))

    elif query.data.startswith("admin_del_"):
        if user_id != OWNER_ID:
            await query.answer("🚫 دسترسی محدود.", show_alert=True)
            return

        target_id = int(query.data.split("_")[2])
        if remove_admin_db(target_id):
            await safe_edit_text(query.message, f"✅ دسترسی کاربر <code>{target_id}</code> لغو شد.")
        else:
            await safe_edit_text(query.message, "❌ خطا در حذف مدیر.")

# ============================================================
# MAIN ENTRY POINT
# ============================================================

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN یافت نشد!")
        return

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
            handle_message
        )
    )

    logger.info("ربات ارتقایافته گیمفا نسخه 3.0 فعال شد...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
