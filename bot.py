import os
import re
import json
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.78"))
KEEP_HOURS = 24
DB_FILE = "news.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("gamefa")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")


def connect():
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text TEXT NOT NULL,
            normalized TEXT NOT NULL,
            created_at TEXT NOT NULL,
            score REAL DEFAULT 0,
            reason TEXT DEFAULT '',
            result TEXT NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_news_created ON news(created_at)")


def now():
    return datetime.now(timezone.utc)


def normalize(text):
    text = text.lower()
    for a, b in {"ي":"ی", "ى":"ی", "ك":"ک", "ۀ":"ه", "ة":"ه", "ؤ":"و", "إ":"ا", "أ":"ا", "ئ":"ی"}.items():
        text = text.replace(a, b)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"@\w+", " ", text)
    text = re.sub(r"[^\w\s\u0600-\u06ff]", " ", text)
    return " ".join(text.split())


def cleanup():
    cutoff = (now() - timedelta(hours=KEEP_HOURS)).isoformat()
    with connect() as c:
        c.execute("DELETE FROM news WHERE created_at < ?", (cutoff,))


def recent(limit=100):
    cutoff = (now() - timedelta(hours=KEEP_HOURS)).isoformat()
    with connect() as c:
        return c.execute("SELECT * FROM news WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?", (cutoff, limit)).fetchall()


def save(user_id, text, score=0, reason="", result="new"):
    with connect() as c:
        cur = c.execute("INSERT INTO news(user_id,text,normalized,created_at,score,reason,result) VALUES(?,?,?,?,?,?,?)",
                        (user_id, text, normalize(text), now().isoformat(), score, reason, result))
        return cur.lastrowid


def local_similarity(a, b):
    A, B = set(normalize(a).split()), set(normalize(b).split())
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


async def ai_compare(new_text, old_text):
    if not OPENAI_API_KEY:
        return None
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        prompt = f'''دو خبر زیر را مقایسه کن. فقط مشخص کن آیا درباره همان رویداد هستند یا نه. تفاوت در تیتر، ترجمه، ترتیب جمله‌ها یا کلمات نباید مانع تشخیص شود.
خبر جدید:
{new_text[:6000]}

خبر قبلی:
{old_text[:6000]}

فقط JSON معتبر برگردان:
{{"duplicate": true/false, "score": 0-100, "reason": "کوتاه و فارسی"}}'''
        r = await client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "تو سیستم تشخیص اخبار تکراری گیمفا هستی."},
                {"role": "user", "content": prompt}
            ]
        )
        data = json.loads(r.choices[0].message.content)
        return bool(data.get("duplicate")), max(0, min(100, float(data.get("score", 0)))) / 100, str(data.get("reason", ""))[:500]
    except Exception:
        log.exception("AI comparison failed")
        return None


def allowed(uid):
    return not ADMIN_IDS or uid in ADMIN_IDS


def keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📰 بررسی خبر", callback_data="how"), InlineKeyboardButton("📊 آمار", callback_data="stats")],
        [InlineKeyboardButton("🗑 پاکسازی ۲۴ ساعته", callback_data="cleanup"), InlineKeyboardButton("⚙️ وضعیت", callback_data="status")],
        [InlineKeyboardButton("❓ راهنما", callback_data="help")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id):
        await update.message.reply_text("⛔ شما دسترسی ندارید.")
        return
    cleanup()
    await update.message.reply_text(
        "🤖 ربات تشخیص اخبار تکراری گیمفا فعال است.\n\n"
        "خبر را همین‌جا بفرست. ربات فقط مشخص می‌کند خبر جدید است یا تکراری/مشابه.\n\n"
        f"🧠 هوش مصنوعی: {'فعال' if OPENAI_API_KEY else 'غیرفعال'}\n"
        "⏱ آرشیو مقایسه: فقط ۲۴ ساعت اخیر\n"
        "📤 ارسال خودکار به کانال: ندارد",
        reply_markup=keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id): return
    await update.message.reply_text(
        "❓ راهنما\n\nخبر را برای ربات ارسال کن. ابتدا شباهت سریع بررسی می‌شود و اگر کاندیدای مشابه وجود داشته باشد، هوش مصنوعی آن را دقیق‌تر بررسی می‌کند. فقط نتیجه اعلام می‌شود و هیچ خبری به کانال ارسال نمی‌شود.\n\n"
        "دستورات:\n/start — شروع\n/stats — آمار\n/cleanup — پاکسازی دستی\n/help — راهنما", reply_markup=keyboard())


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id): return
    cleanup(); rows = recent(10000)
    dup = sum(r["result"] == "duplicate" for r in rows)
    await update.message.reply_text(f"📊 آمار ۲۴ ساعت اخیر\n\n📰 کل بررسی‌ها: {len(rows)}\n🆕 جدید: {len(rows)-dup}\n🔴 تکراری: {dup}\n🧠 AI: {'فعال' if OPENAI_API_KEY else 'غیرفعال'}", reply_markup=keyboard())


async def cleanup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id): return
    cleanup(); await update.message.reply_text("🗑 اخبار قدیمی‌تر از ۲۴ ساعت پاک شدند.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not allowed(update.effective_user.id): return
    text = (update.message.text or update.message.caption or "").strip()
    if not text:
        await update.message.reply_text("❌ متن خبر پیدا نشد."); return

    cleanup()
    rows = recent(100)
    candidates = sorted(((local_similarity(text, r["text"]), r) for r in rows), key=lambda x: x[0], reverse=True)[:8]
    candidates = [x for x in candidates if x[0] >= 0.20]

    best = None; best_score = 0; reason = ""
    if OPENAI_API_KEY and candidates:
        for _, row in candidates:
            result = await ai_compare(text, row["text"])
            if result:
                duplicate, score, why = result
                if score > best_score:
                    best, best_score, reason = row, score, why
                if duplicate and score >= THRESHOLD:
                    break
    else:
        if candidates:
            best_score, best = candidates[0]

    duplicate = best is not None and best_score >= THRESHOLD
    if duplicate:
        nid = save(update.effective_user.id, text, best_score, reason, "duplicate")
        await update.message.reply_text(
            "🔴 خبر تکراری/مشابه است.\n\n"
            f"📊 شباهت: {best_score*100:.1f}%\n"
            f"🆔 شناسه بررسی: {nid}\n"
            f"🤖 دلیل: {reason or 'شباهت محتوایی بالا'}\n\n"
            f"📝 خبر قبلی:\n{best['text'][:1500]}",
            reply_markup=keyboard())
    else:
        nid = save(update.effective_user.id, text, best_score, "", "new")
        await update.message.reply_text(
            "🟢 خبر جدید است.\n\n"
            f"🆔 شناسه: {nid}\n"
            "⏱ این خبر فقط تا ۲۴ ساعت در آرشیو مقایسه می‌ماند.\n"
            "📤 هیچ ارسالی به کانال انجام نمی‌شود.", reply_markup=keyboard())


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update.effective_user.id): return
    if q.data == "how":
        await q.message.reply_text("📰 خبر را همین‌جا ارسال کن؛ متن یا کپشن قابل قبول است.")
    elif q.data == "stats":
        cleanup(); rows = recent(10000); dup = sum(r["result"] == "duplicate" for r in rows)
        await q.message.reply_text(f"📊 ۲۴ ساعت اخیر\nکل: {len(rows)}\nجدید: {len(rows)-dup}\nتکراری: {dup}")
    elif q.data == "cleanup":
        cleanup(); await q.message.reply_text("🗑 پاکسازی انجام شد.")
    elif q.data == "status":
        await q.message.reply_text(f"⚙️ وضعیت\nAI: {'فعال' if OPENAI_API_KEY else 'غیرفعال'}\nمدل: {OPENAI_MODEL if OPENAI_API_KEY else '-'}\nآرشیو: ۲۴ ساعت\nآستانه: {THRESHOLD*100:.0f}%")
    elif q.data == "help":
        await q.message.reply_text("❓ خبر را بفرست؛ ربات فقط تکراری یا جدید بودن آن را بررسی می‌کند.")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("cleanup", cleanup_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))
    log.info("Gamefa bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
