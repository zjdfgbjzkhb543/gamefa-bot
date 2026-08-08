import asyncio
import logging
import sqlite3
import hashlib
import re
import os
import json
import shutil
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import textdistance
from persiantools import digits

# ====== بخش تنظیمات ======
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
DATABASE_FILE = "news_cache.db"
BACKUP_DIR = "backups"
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.75"))
CACHE_DAYS = int(os.getenv("CACHE_DAYS", "30"))

# تنظیمات بهینه
MAX_PREVIOUS_NEWS = 20
BACKUP_INTERVAL_HOURS = 6
CLEANUP_INTERVAL_HOURS = 24

# ====== بخش دیتابیس ======
class Database:
    def __init__(self, db_file: str):
        self.db_file = db_file
        self._init_db()
        self._cache = {}
        self._cache_time = {}
        self._cache_ttl = 300
    
    def _init_db(self):
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text_hash TEXT NOT NULL,
                    text_content TEXT NOT NULL,
                    caption TEXT,
                    media_hash TEXT,
                    media_type TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    admin_id INTEGER,
                    is_sent BOOLEAN DEFAULT 0,
                    tags TEXT,
                    similarity_score REAL DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    views INTEGER DEFAULT 0,
                    category TEXT DEFAULT 'general'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    admin_id INTEGER,
                    news_id INTEGER,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON news(text_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON news(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON news(status)")
            
            conn.execute("""
                INSERT OR IGNORE INTO settings (key, value) VALUES 
                ('threshold', ?),
                ('cache_days', ?),
                ('auto_backup', 'true'),
                ('last_backup', ?),
                ('total_news', '0'),
                ('duplicates_detected', '0')
            """, (str(SIMILARITY_THRESHOLD), str(CACHE_DAYS), datetime.now().isoformat()))
            
            for admin_id in ADMIN_IDS:
                conn.execute("INSERT OR IGNORE INTO admins (id) VALUES (?)", (admin_id,))
    
    def _get_text_hash(self, text: str) -> str:
        return hashlib.md5(text.strip().lower().encode()).hexdigest()
    
    def _get_media_hash(self, media_id: str) -> str:
        return hashlib.md5(media_id.encode()).hexdigest() if media_id else None
    
    def add_news(self, text: str, caption: str = "", media_id: str = None, 
                 media_type: str = None, admin_id: int = None, 
                 tags: str = "", similarity_score: float = 0,
                 category: str = "general") -> int:
        text_hash = self._get_text_hash(text)
        media_hash = self._get_media_hash(media_id) if media_id else None
        
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.execute("""
                INSERT INTO news (text_hash, text_content, caption, media_hash, 
                                 media_type, admin_id, tags, similarity_score, category)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (text_hash, text, caption, media_hash, media_type, admin_id, tags, similarity_score, category))
            news_id = cursor.lastrowid
            
            conn.execute("""
                UPDATE settings SET value = CAST(value AS INTEGER) + 1 
                WHERE key = 'total_news'
            """)
            
            conn.execute("""
                INSERT INTO logs (action, admin_id, news_id, details)
                VALUES (?, ?, ?, ?)
            """, ('add', admin_id, news_id, f'خبر جدید اضافه شد: {text[:50]}...'))
            
            self._cache.clear()
            return news_id
    
    def check_duplicate(self, text: str, media_id: str = None) -> Tuple[bool, Optional[Dict]]:
        text_hash = self._get_text_hash(text)
        media_hash = self._get_media_hash(media_id) if media_id else None
        
        cache_key = f"{text_hash}_{media_hash}"
        if cache_key in self._cache:
            if datetime.now().timestamp() - self._cache_time.get(cache_key, 0) < self._cache_ttl:
                return self._cache[cache_key]
        
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            
            cursor = conn.execute("""
                SELECT * FROM news 
                WHERE text_hash = ? AND status != 'rejected'
                ORDER BY created_at DESC 
                LIMIT 1
            """, (text_hash,))
            exact_match = cursor.fetchone()
            
            if exact_match:
                result = (True, dict(exact_match))
                self._cache[cache_key] = result
                self._cache_time[cache_key] = datetime.now().timestamp()
                return result
            
            if media_hash:
                cursor = conn.execute("""
                    SELECT * FROM news 
                    WHERE media_hash = ? AND status != 'rejected'
                    ORDER BY created_at DESC 
                    LIMIT 1
                """, (media_hash,))
                media_match = cursor.fetchone()
                if media_match:
                    result = (True, dict(media_match))
                    self._cache[cache_key] = result
                    self._cache_time[cache_key] = datetime.now().timestamp()
                    return result
        
        return False, None
    
    def get_previous_news(self, text: str, limit: int = 20) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM news 
                WHERE status IN ('approved', 'sent')
                ORDER BY created_at DESC 
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    def update_status(self, news_id: int, status: str, admin_id: int = None):
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("UPDATE news SET status = ? WHERE id = ?", (status, news_id))
            
            if status == 'duplicate':
                conn.execute("""
                    UPDATE settings SET value = CAST(value AS INTEGER) + 1 
                    WHERE key = 'duplicates_detected'
                """)
            
            conn.execute("""
                INSERT INTO logs (action, admin_id, news_id, details)
                VALUES (?, ?, ?, ?)
            """, ('status_change', admin_id, news_id, f'وضعیت به {status} تغییر کرد'))
            
            self._cache.clear()
    
    def delete_news(self, news_id: int, admin_id: int = None):
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("DELETE FROM news WHERE id = ?", (news_id,))
            conn.execute("""
                INSERT INTO logs (action, admin_id, news_id, details)
                VALUES (?, ?, ?, ?)
            """, ('delete', admin_id, news_id, 'خبر حذف شد'))
            self._cache.clear()
    
    def get_stats(self) -> Dict:
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.execute("SELECT COUNT(*) as total FROM news")
            total = cursor.fetchone()[0]
            
            cursor = conn.execute("""
                SELECT status, COUNT(*) as count FROM news GROUP BY status
            """)
            status_counts = {row[0]: row[1] for row in cursor.fetchall()}
            
            cursor = conn.execute("""
                SELECT category, COUNT(*) as count FROM news 
                WHERE status IN ('approved', 'sent')
                GROUP BY category
            """)
            category_counts = {row[0]: row[1] for row in cursor.fetchall()}
            
            cursor = conn.execute("""
                SELECT COUNT(*) as today FROM news 
                WHERE DATE(created_at) = DATE('now')
            """)
            today = cursor.fetchone()[0]
            
            cursor = conn.execute("""
                SELECT COUNT(*) as week FROM news 
                WHERE created_at >= DATE('now', '-7 days')
            """)
            week = cursor.fetchone()[0]
            
            cursor = conn.execute("""
                SELECT value FROM settings WHERE key = 'duplicates_detected'
            """)
            duplicates = int(cursor.fetchone()[0] or 0)
            
            return {
                'total': total,
                'pending': status_counts.get('pending', 0),
                'approved': status_counts.get('approved', 0),
                'rejected': status_counts.get('rejected', 0),
                'duplicates': duplicates,
                'sent': status_counts.get('sent', 0),
                'today': today,
                'week': week,
                'categories': category_counts
            }
    
    def get_setting(self, key: str) -> Optional[str]:
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            result = cursor.fetchone()
            return result[0] if result else None
    
    def set_setting(self, key: str, value: str):
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
            """, (key, value, datetime.now().isoformat()))
    
    def search_news(self, query: str, limit: int = 20) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM news 
                WHERE text_content LIKE ? OR caption LIKE ? OR tags LIKE ?
                ORDER BY created_at DESC 
                LIMIT ?
            """, (f'%{query}%', f'%{query}%', f'%{query}%', limit))
            return [dict(row) for row in cursor.fetchall()]
    
    def create_backup(self) -> str:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        backup_path = os.path.join(BACKUP_DIR, backup_name)
        shutil.copy2(self.db_file, backup_path)
        self.set_setting('last_backup', datetime.now().isoformat())
        
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')])
        if len(backups) > 7:
            for old_backup in backups[:-7]:
                os.remove(os.path.join(BACKUP_DIR, old_backup))
        
        return backup_path
    
    def clean_old_records(self, days: int = None):
        if days is None:
            days = int(self.get_setting('cache_days') or CACHE_DAYS)
        
        cutoff = datetime.now() - timedelta(days=days)
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("DELETE FROM news WHERE created_at < ? AND status = 'rejected'", (cutoff,))
            conn.execute("VACUUM")
    
    def add_admin(self, admin_id: int, name: str = ""):
        with sqlite3.connect(self.db_file) as conn:
            conn.execute("INSERT OR REPLACE INTO admins (id, name) VALUES (?, ?)", (admin_id, name))
    
    def get_admins(self) -> List[int]:
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.execute("SELECT id FROM admins")
            return [row[0] for row in cursor.fetchall()]

# ====== بخش تشخیص شباهت ======
class SimilarityChecker:
    def __init__(self):
        self.threshold = float(db.get_setting('threshold') or SIMILARITY_THRESHOLD)
        self._cache = {}
    
    def update_threshold(self, new_threshold: float):
        self.threshold = new_threshold
        db.set_setting('threshold', str(new_threshold))
        self._cache.clear()
    
    def _clean_text(self, text: str) -> str:
        text = re.sub(r'[^\w\s]', ' ', text)
        text = digits.en_to_fa(text)
        text = ' '.join(text.split())
        return text.lower()
    
    def _get_keywords(self, text: str, min_length: int = 3) -> List[str]:
        cleaned = self._clean_text(text)
        words = cleaned.split()
        stopwords = {'و', 'به', 'از', 'با', 'برای', 'در', 'را', 'که', 'این', 'آن', 'نیز', 'هم', 'یا', 'بر', 'است', 'تا'}
        keywords = [w for w in words if len(w) >= min_length and w not in stopwords]
        return keywords
    
    def combined_similarity(self, text1: str, text2: str) -> float:
        cache_key = f"{hash(text1)}_{hash(text2)}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        kw1 = set(self._get_keywords(text1))
        kw2 = set(self._get_keywords(text2))
        
        if not kw1 or not kw2:
            jaccard = 0.0
        else:
            intersection = len(kw1 & kw2)
            union = len(kw1 | kw2)
            jaccard = intersection / union if union > 0 else 0.0
        
        clean1 = self._clean_text(text1)
        clean2 = self._clean_text(text2)
        
        if clean1 and clean2:
            lev_dist = textdistance.levenshtein.distance(clean1, clean2)
            max_len = max(len(clean1), len(clean2))
            levenshtein = 1 - (lev_dist / max_len) if max_len > 0 else 0.0
        else:
            levenshtein = 0.0
        
        combined = (jaccard * 0.6) + (levenshtein * 0.4)
        result = min(1.0, max(0.0, combined))
        
        self._cache[cache_key] = result
        return result
    
    def is_duplicate(self, new_text: str, old_text: str) -> Tuple[bool, float]:
        similarity = self.combined_similarity(new_text, old_text)
        return similarity >= self.threshold, similarity

# ====== بخش اصلی ربات ======
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

db = Database(DATABASE_FILE)
checker = SimilarityChecker()

# ====== کلاس ربات ======
class GamfaNewsBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.admin_ids = db.get_admins() or ADMIN_IDS
        self.last_backup = datetime.now()
        self.last_cleanup = datetime.now()
        
        # ثبت هندلرها
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("stats", self.show_stats))
        self.application.add_handler(CommandHandler("search", self.search_news))
        self.application.add_handler(CommandHandler("backup", self.create_backup))
        self.application.add_handler(CommandHandler("threshold", self.change_threshold))
        self.application.add_handler(CommandHandler("delete", self.delete_news))
        self.application.add_handler(CommandHandler("help", self.show_help))
        self.application.add_handler(CommandHandler("settings", self.show_settings))
        self.application.add_handler(CommandHandler("addadmin", self.add_admin))
        self.application.add_handler(CommandHandler("listadmins", self.list_admins))
        self.application.add_handler(CommandHandler("categories", self.show_categories))
        self.application.add_handler(CommandHandler("top", self.show_top_news))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """دستور استارت"""
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید!")
            return
        
        await update.message.reply_text(
            "🤖 **ربات گیمفا فعال شد!**\n\n"
            "📌 برای راهنما `/help` رو بفرست.\n"
            f"👥 تعداد ادمین‌ها: {len(self.admin_ids)}\n"
            f"🎯 آستانه شباهت: {checker.threshold*100:.0f}%"
        )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """مدیریت پیام‌های کاربران"""
        user_id = update.effective_user.id
        
        # بررسی ادمین
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی ارسال خبر ندارید.")
            logger.warning(f"⚠️ تلاش غیرمجاز از {user_id}")
            return
        
        # پردازش خبر
        await self.process_news(update, context)
    
    async def process_news(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """پردازش خبر جدید"""
        text = update.message.text or update.message.caption or ""
        
        if not text:
            await update.message.reply_text("❌ لطفاً متن خبر رو وارد کنید.")
            return
        
        # استخراج مدیا
        media_id = None
        media_type = None
        if update.message.photo:
            media_id = f"photo_{update.message.photo[-1].file_id}"
            media_type = 'photo'
        elif update.message.document:
            media_id = f"document_{update.message.document.file_id}"
            media_type = 'document'
        
        # تشخیص دسته‌بندی
        category = self.detect_category(text)
        
        # چک تکراری
        is_dup, previous = db.check_duplicate(text, media_id)
        if is_dup:
            news_id = db.add_news(
                text, update.message.caption or "", media_id, media_type,
                user_id, similarity_score=1.0, category=category
            )
            db.update_status(news_id, 'duplicate')
            
            await update.message.reply_text(
                f"⚠️ **خبر تکراری!**\n\n"
                f"📅 تاریخ: {previous['created_at']}\n"
                f"📝 متن: {previous['text_content'][:100]}...\n\n"
                f"🔴 این خبر قبلاً ارسال شده."
            )
            return
        
        # چک شباهت
        previous_news = db.get_previous_news(text, limit=MAX_PREVIOUS_NEWS)
        best_match = None
        best_score = 0
        
        for old in previous_news:
            is_dup, similarity = checker.is_duplicate(text, old['text_content'])
            if is_dup and similarity > best_score:
                best_score = similarity
                best_match = old
                if similarity > 0.9:
                    break
        
        if best_match:
            news_id = db.add_news(
                text, update.message.caption or "", media_id, media_type,
                user_id, similarity_score=best_score, category=category
            )
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ تایید", callback_data=f"approve_{news_id}"),
                    InlineKeyboardButton("❌ رد", callback_data=f"reject_{news_id}")
                ],
                [
                    InlineKeyboardButton("📝 مشاهده قبلی", callback_data=f"view_{best_match['id']}")
                ]
            ]
            
            await update.message.reply_text(
                f"⚠️ **خبر مشابه!**\n\n"
                f"📊 شباهت: {best_score*100:.1f}%\n"
                f"📂 دسته: {category}\n\n"
                f"📝 جدید:\n{text[:150]}...\n\n"
                f"📝 قبلی:\n{best_match['text_content'][:150]}...\n\n"
                f"⬇️ تصمیم بگیر:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        # خبر جدید
        news_id = db.add_news(
            text, update.message.caption or "", media_id, media_type,
            user_id, category=category
        )
        db.update_status(news_id, 'approved')
        
        tags = self.extract_tags(text)
        
        keyboard = [
            [InlineKeyboardButton("📤 ارسال به کانال", callback_data=f"send_{news_id}")],
            [
                InlineKeyboardButton("🏷️ تگ", callback_data=f"tag_{news_id}"),
                InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_{news_id}")
            ]
        ]
        
        await update.message.reply_text(
            f"✅ **خبر جدید!**\n\n"
            f"🆔 شناسه: {news_id}\n"
            f"📂 دسته: {category}\n"
            f"🏷️ تگ‌ها: {tags if tags else 'ندارد'}\n"
            f"📝 متن: {text[:150]}...\n\n"
            f"⬇️ ارسال یا مدیریت:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    def detect_category(self, text: str) -> str:
        """تشخیص دسته‌بندی خودکار"""
        text_lower = text.lower()
        categories = {
            'بازی': ['بازی', 'game', 'گیم', 'playstation', 'xbox', 'nintendo', 'pc', 'کنسول'],
            'سینما': ['فیلم', 'سریال', 'سینما', 'movie', 'film', 'اکران', 'کارگردان'],
            'تکنولوژی': ['تکنولوژی', 'فناوری', 'technology', 'گوشی', 'موبایل', 'لپ تاپ'],
            'علمی': ['علمی', 'پژوهش', 'تحقیق', 'ناسا', 'فضا'],
            'اقتصادی': ['اقتصاد', 'بازار', 'سهام', 'ارز', 'طلا']
        }
        
        for category, keywords in categories.items():
            for keyword in keywords:
                if keyword in text_lower:
                    return category
        
        return 'عمومی'
    
    def extract_tags(self, text: str) -> str:
        """استخراج تگ‌های خودکار"""
        words = text.split()
        tags = []
        for word in words[:10]:
            if len(word) > 4 and word not in ['و', 'به', 'از', 'با', 'برای']:
                tags.append(word)
        return ' '.join([f"#{tag}" for tag in tags[:5]])
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """مدیریت کلیک روی دکمه‌ها"""
        query = update.callback_query
        data = query.data
        user_id = update.effective_user.id
        
        if user_id not in self.admin_ids:
            await query.answer("⛔ شما دسترسی ندارید!", show_alert=True)
            return
        
        await query.answer("⏳ در حال پردازش...")
        
        if data.startswith('approve_'):
            news_id = int(data.split('_')[1])
            db.update_status(news_id, 'approved')
            await query.edit_message_text(
                f"✅ خبر {news_id} تایید شد!\n"
                f"📤 برای ارسال روی دکمه زیر کلیک کن:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📤 ارسال", callback_data=f"send_{news_id}")]
                ])
            )
            
        elif data.startswith('reject_'):
            news_id = int(data.split('_')[1])
            db.update_status(news_id, 'rejected')
            await query.edit_message_text(f"❌ خبر {news_id} رد شد!")
            
        elif data.startswith('view_'):
            news_id = int(data.split('_')[1])
            news = db.search_news(f"id:{news_id}", limit=1)
            if news:
                await query.answer(
                    f"📝 {news[0]['text_content'][:200]}...",
                    show_alert=True
                )
            else:
                await query.answer("❌ خبر پیدا نشد!", show_alert=True)
                
        elif data.startswith('send_'):
            news_id = int(data.split('_')[1])
            await self.send_to_channel(update, news_id)
            
        elif data.startswith('delete_'):
            news_id = int(data.split('_')[1])
            db.delete_news(news_id, user_id)
            await query.edit_message_text(f"🗑️ خبر {news_id} حذف شد!")
            
        elif data.startswith('tag_'):
            news_id = int(data.split('_')[1])
            await query.edit_message_text(
                f"🏷️ برای اضافه کردن تگ به خبر {news_id}،\n"
                f"پیام جدیدی با فرمت زیر بفرست:\n"
                f"`/tag {news_id} #بازی #جدید`"
            )
    
    async def send_to_channel(self, update: Update, news_id: int):
        """ارسال خبر به کانال"""
        try:
            news = db.search_news(f"id:{news_id}", limit=1)
            if not news:
                await update.callback_query.answer("❌ خبر پیدا نشد!", show_alert=True)
                return
            
            news = news[0]
            
            # ساخت پیام زیبا
            message = (
                f"📰 **خبر جدید**\n\n"
                f"{news['text_content']}\n\n"
                f"🏷️ {news['tags'] if news['tags'] else '#اخبار'}\n"
                f"📂 {news['category']}"
            )
            
            await context.bot.send_message(chat_id=CHANNEL_ID, text=message)
            db.update_status(news_id, 'sent')
            
            await update.callback_query.edit_message_text(
                f"✅ خبر {news_id} به کانال ارسال شد!"
            )
            logger.info(f"📤 خبر {news_id} به کانال ارسال شد")
            
        except Exception as e:
            logger.error(f"❌ خطا در ارسال: {e}")
            await update.callback_query.answer("❌ خطا در ارسال!", show_alert=True)
    
    # ====== دستورات مدیریتی ======
    
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی ندارید!")
            return
        
        stats = db.get_stats()
        
        bar = "█" * min(int(stats['total'] / 10), 20)
        
        message = (
            f"📊 **آمار ربات گیمفا**\n\n"
            f"📌 کل اخبار: {stats['total']}\n"
            f"⏳ در انتظار: {stats['pending']}\n"
            f"✅ تایید شده: {stats['approved']}\n"
            f"📤 ارسال شده: {stats['sent']}\n"
            f"❌ رد شده: {stats['rejected']}\n"
            f"🔄 تکراری: {stats['duplicates']}\n\n"
            f"📅 امروز: {stats['today']} خبر\n"
            f"📆 این هفته: {stats['week']} خبر\n\n"
            f"📂 **دسته‌بندی:**\n"
        )
        
        for cat, count in stats['categories'].items():
            cat_bar = "█" * min(int(count / 5), 15)
            message += f"  {cat}: {count} {cat_bar}\n"
        
        message += f"\n⚙️ **تنظیمات:**\n"
        message += f"🎯 آستانه: {checker.threshold*100:.0f}%\n"
        message += f"📆 نگهداری: {db.get_setting('cache_days')} روز\n"
        message += f"👥 ادمین‌ها: {len(self.admin_ids)}"
        
        await update.message.reply_text(message)
    
    async def search_news(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی ندارید!")
            return
        
        if not context.args:
            await update.message.reply_text(
                "🔍 **جستجو**\n\n"
                "`/search متن مورد نظر`"
            )
            return
        
        query = ' '.join(context.args)
        results = db.search_news(query, limit=10)
        
        if not results:
            await update.message.reply_text(f"🔍 چیزی با '{query}' پیدا نشد.")
            return
        
        message = f"🔍 **نتایج:** '{query}'\n\n"
        for i, news in enumerate(results[:10], 1):
            status_emoji = {
                'pending': '⏳',
                'approved': '✅',
                'rejected': '❌',
                'duplicate': '🔄',
                'sent': '📤'
            }.get(news['status'], '📝')
            
            message += (
                f"{i}. {status_emoji} 🆔{news['id']} - "
                f"{news['text_content'][:50]}...\n"
                f"   📂 {news['category']} | {news['created_at'][:10]}\n\n"
            )
        
        await update.message.reply_text(message)
    
    async def create_backup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی ندارید!")
            return
        
        backup_path = db.create_backup()
        await update.message.reply_text(
            f"💾 **بکاپ گرفته شد!**\n\n"
            f"📁: {backup_path}\n"
            f"📅: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    
    async def change_threshold(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی ندارید!")
            return
        
        if not context.args:
            await update.message.reply_text(
                f"🎯 **آستانه فعلی:** {checker.threshold*100:.0f}%\n\n"
                f"تغییر: `/threshold 75`"
            )
            return
        
        try:
            new_threshold = float(context.args[0]) / 100
            if not 0.5 <= new_threshold <= 0.95:
                await update.message.reply_text("❌ بین 50 تا 95 باشه.")
                return
            
            checker.update_threshold(new_threshold)
            await update.message.reply_text(
                f"✅ آستانه به {new_threshold*100:.0f}% تغییر کرد!"
            )
            
        except ValueError:
            await update.message.reply_text("❌ عدد معتبر وارد کن.")
    
    async def delete_news(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی ندارید!")
            return
        
        if not context.args:
            await update.message.reply_text(
                "🗑️ **حذف خبر**\n\n"
                "`/delete شناسه`"
            )
            return
        
        try:
            news_id = int(context.args[0])
            db.delete_news(news_id, user_id)
            await update.message.reply_text(f"✅ خبر {news_id} حذف شد!")
            
        except ValueError:
            await update.message.reply_text("❌ شناسه باید عدد باشه.")
    
    async def add_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی ندارید!")
            return
        
        if not context.args:
            await update.message.reply_text("`/addadmin 123456789`")
            return
        
        try:
            new_admin_id = int(context.args[0])
            if new_admin_id in self.admin_ids:
                await update.message.reply_text("⚠️ قبلاً ادمین هست!")
                return
            
            db.add_admin(new_admin_id)
            self.admin_ids.append(new_admin_id)
            await update.message.reply_text(f"✅ ادمین جدید اضافه شد: `{new_admin_id}`")
            
        except ValueError:
            await update.message.reply_text("❌ آیدی عددی وارد کن.")
    
    async def list_admins(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی ندارید!")
            return
        
        admins = db.get_admins()
        message = "👥 **لیست ادمین‌ها:**\n\n"
        for i, admin_id in enumerate(admins, 1):
            message += f"{i}. `{admin_id}`\n"
        await update.message.reply_text(message)
    
    async def show_categories(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی ندارید!")
            return
        
        stats = db.get_stats()
        message = "📂 **دسته‌بندی اخبار:**\n\n"
        for cat, count in stats['categories'].items():
            bar = "█" * min(int(count / 5), 15)
            message += f"  {cat}: {count} {bar}\n"
        await update.message.reply_text(message)
    
    async def show_top_news(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی ندارید!")
            return
        
        with sqlite3.connect(DATABASE_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM news 
                WHERE status = 'sent'
                ORDER BY views DESC, created_at DESC 
                LIMIT 5
            """)
            top_news = [dict(row) for row in cursor.fetchall()]
        
        if not top_news:
            await update.message.reply_text("📊 هنوز خبری ارسال نشده.")
            return
        
        message = "🏆 **۵ خبر پربازدید:**\n\n"
        for i, news in enumerate(top_news, 1):
            message += (
                f"{i}. 🆔{news['id']} - {news['text_content'][:60]}...\n"
                f"   👁️ {news['views']} بازدید | {news['created_at'][:10]}\n\n"
            )
        await update.message.reply_text(message)
    
    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید!")
            return
        
        help_text = (
            "🤖 **راهنمای کامل ربات گیمفا**\n\n"
            "📌 **دستورات مدیریتی:**\n"
            "`/stats` - آمار کامل\n"
            "`/search متن` - جستجو\n"
            "`/backup` - بکاپ فوری\n"
            "`/threshold عدد` - تنظیم آستانه (50-95)\n"
            "`/delete شناسه` - حذف خبر\n"
            "`/addadmin آیدی` - افزودن ادمین\n"
            "`/listadmins` - لیست ادمین‌ها\n"
            "`/categories` - آمار دسته‌بندی\n"
            "`/top` - ۵ خبر برتر\n"
            "`/help` - این راهنما\n\n"
            "📤 **ارسال خبر:**\n"
            "متن خبر رو مستقیم بفرست.\n\n"
            "✅ **تایید خودکار:**\n"
            "- خبر جدید → تایید خودکار\n"
            "- خبر مشابه → نیاز به تایید\n"
            "- خبر تکراری → رد خودکار\n\n"
            "🎯 **آستانه فعلی:** {:.0f}%\n"
            "📆 **نگهداری:** {} روز\n"
            "👥 **تعداد ادمین‌ها:** {}\n\n"
            "⚡ ربات روی Railway (رایگان) اجرا میشه."
        ).format(
            checker.threshold * 100,
            db.get_setting('cache_days'),
            len(self.admin_ids)
        )
        
        await update.message.reply_text(help_text)
    
    async def show_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.admin_ids:
            await update.message.reply_text("⛔ شما دسترسی ندارید!")
            return
        
        settings = {
            'threshold': db.get_setting('threshold'),
            'cache_days': db.get_setting('cache_days'),
            'auto_backup': db.get_setting('auto_backup'),
            'last_backup': db.get_setting('last_backup'),
            'total_news': db.get_setting('total_news'),
            'duplicates_detected': db.get_setting('duplicates_detected')
        }
        
        message = (
            f"⚙️ **تنظیمات ربات**\n\n"
            f"🎯 آستانه شباهت: {float(settings['threshold'])*100:.0f}%\n"
            f"📆 روزهای نگهداری: {settings['cache_days']} روز\n"
            f"💾 بکاپ خودکار: {'فعال' if settings['auto_backup'] == 'true' else 'غیرفعال'}\n"
            f"📅 آخرین بکاپ: {settings['last_backup'][:16] if settings['last_backup'] else 'ندارد'}\n"
            f"📊 کل اخبار: {settings['total_news']}\n"
            f"🔄 تکراری‌ها: {settings['duplicates_detected']}\n\n"
            f"👥 تعداد ادمین‌ها: {len(self.admin_ids)}"
        )
        await update.message.reply_text(message)
    
    async def run(self):
        """اجرای ربات"""
        logger.info("🚀 ربات گیمفا راه‌اندازی شد!")
        
        if not self.admin_ids:
            logger.error("❌ هیچ ادمینی تنظیم نشده!")
            return
        
        logger.info(f"👥 تعداد ادمین‌ها: {len(self.admin_ids)}")
        
        db.clean_old_records()
        logger.info("🗑️ پاکسازی اخبار قدیمی انجام شد")
        
        backup_path = db.create_backup()
        logger.info(f"💾 بکاپ اولیه: {backup_path}")
        
        # شروع تسک‌های پس‌زمینه
        asyncio.create_task(self.background_tasks())
        
        logger.info("👂 ربات آماده دریافت پیام است...")
        await self.application.run_polling()
    
    async def background_tasks(self):
        """تسک‌های پس‌زمینه"""
        while True:
            await asyncio.sleep(3600)
            
            if (datetime.now() - self.last_backup).seconds >= BACKUP_INTERVAL_HOURS * 3600:
                backup_path = db.create_backup()
                logger.info(f"💾 بکاپ خودکار: {backup_path}")
                self.last_backup = datetime.now()
            
            if (datetime.now() - self.last_cleanup).seconds >= CLEANUP_INTERVAL_HOURS * 3600:
                db.clean_old_records()
                logger.info("🗑️ پاکسازی خودکار انجام شد")
                self.last_cleanup = datetime.now()

# ====== اجرا ======
async def main():
    bot = GamfaNewsBot()
    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())
