import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from difflib import SequenceMatcher

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from openai import AsyncOpenAI

# ============================================================
# GAMEFA AI BOT - TELEGRAM DESKTOP EXPORT VERSION
# بدون API_ID و API_HASH
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
MAX_NEWS = 1000
AUTO_ARCHIVE_LIMIT = 100

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

DATA_DIR.mkdir(parents=True, exist_ok=True)
NEWS_FILE = DATA_DIR / "news.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("gamefa")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
ai = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

PENDING = {}
AWAITING = {}
IMPORTING = set()


def load_json(path, default):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed reading %s", path)
        return default


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_news():
    data = load_json(NEWS_FILE, [])
    return data if isinstance(data, list) else []


def save_news(news):
    save_json(NEWS_FILE, news[-MAX_NEWS:])


def load_settings():
    default = {"admins": [], "primary_admin": 0, "similarity_threshold": 0.72}
    data = load_json(SETTINGS_FILE, default)
    if not isinstance(data, dict):
        data = default
    data.setdefault("admins", [])
    data.setdefault("primary_admin", 0)
    data.setdefault("similarity_threshold", 0.72)
    return data


SETTINGS = load_settings()


def admins():
    return {int(x) for x in SETTINGS.get("admins", []) if str(x).isdigit()}


def is_admin(uid):
    return uid in admins() or uid == int(SETTINGS.get("primary_admin", 0) or 0)


def is_primary(uid):
    return uid == int(SETTINGS.get("primary_admin", 0) or 0)


def normalize(text):
    text = str(text or "").lower()
    for a, b in {"ي": "ی", "ى": "ی", "ك": "ک", "ۀ": "ه", "\u200c": " ", "\u200f": " "}.items():
        text = text.replace(a, b)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def similarity(a, b):
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    aw, bw = set(a.split()), set(b.split())
    jac = len(aw & bw) / max(1, len(aw | bw))
    seq = SequenceMatcher(None, a, b).ratio()
    return 0.55 * jac + 0.45 * seq


def next_id(news):
    return max([int(x.get("id", 0)) for x in news if str(x.get("id", "")).isdigit()] or [0]) + 1


def extract_telegram_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def parse_telegram_desktop(data):
    """Parse Telegram Desktop export result.json / messages.json."""
    if isinstance(data, dict):
        messages = data.get("messages", [])
    elif isinstance(data, list):
        messages = data
    else:
        return []

    result = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("type") not in (None, "message"):
            continue
        text = extract_telegram_text(m.get("text", "")).strip()
        if not text:
            text = extract_telegram_text(m.get("caption", "")).strip()
        if not text:
            continue
        # Telegram Desktop exports are normally chronological.
        result.append({
            "text": text,
            "date": m.get("date", ""),
            "id": m.get("id"),
            "from": m.get("from", ""),
            "post_author": m.get("post_author", ""),
            "views": m.get("views", 0),
        })
    return result


def parse_archive(raw, filename):
    ext = Path(filename).suffix.lower()
    if ext not in (".json", ".txt"):
        raise ValueError("فایل باید JSON یا TXT باشد.")
    text = raw.decode("utf-8-sig", errors="ignore")
    if ext == ".json":
        return parse_telegram_desktop(json.loads(text))
    blocks = [x.strip() for x in re.split(r"\n\s*\n+", text) if x.strip()]
    return [{"text": x, "date": ""} for x in blocks]


async def ai_json(system, user):
    if not ai:
        return None
    try:
        r = await ai.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user[:16000]}],
        )
        return json.loads(r.choices[0].message.content)
    except Exception:
        logger.exception("OpenAI error")
        return None


async def analyze(text):
    return await ai_json(
        "تو دستیار تحریریه گیمفا هستی. فقط JSON معتبر بده با title, category, summary, source_type, importance, reason. اطلاعاتی که در متن نیست جعل نکن.",
        text,
    )


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📰 بررسی خبر", callback_data="check")],
        [InlineKeyboardButton(text="📚 آرشیو", callback_data="archive_menu"), InlineKeyboardButton(text="📊 آمار", callback_data="stats")],
        [InlineKeyboardButton(text="👥 ادمین‌ها", callback_data="admins")],
    ])


def archive_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 ورود خودکار ۱۰۰ خبر", callback_data="import_100")],
        [InlineKeyboardButton(text="📥 ورود کل آرشیو", callback_data="import_all")],
        [InlineKeyboardButton(text="📊 آمار آرشیو", callback_data="stats")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="main")],
    ])


@router.message(CommandStart())
async def start(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ شما دسترسی ندارید.")
        return
    await message.answer("🤖 پنل مدیریت گیمفا آماده است.", reply_markup=main_keyboard())


@router.message(Command("help"))
async def help_cmd(message: Message):
    if message.from_user and is_admin(message.from_user.id):
        await message.answer("برای آرشیو تلگرام: پنل ← 📚 آرشیو ← یکی از گزینه‌های ورود. فایل خروجی Telegram Desktop (result.json) را بفرست. API_ID و API_HASH لازم نیست.")


@router.callback_query(F.data == "main")
async def main_menu(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    await c.message.edit_text("🤖 پنل مدیریت گیمفا", reply_markup=main_keyboard())
    await c.answer()


@router.callback_query(F.data == "archive_menu")
async def archive_menu(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    await c.message.edit_text("📚 مدیریت آرشیو\n\nبرای وارد کردن ۱۰۰ خبر آخر، فایل خروجی Telegram Desktop را ارسال کن.", reply_markup=archive_keyboard())
    await c.answer()


@router.callback_query(F.data.in_({"import_100", "import_all"}))
async def start_import(c: CallbackQuery):
    if not is_primary(c.from_user.id):
        await c.answer("⛔ فقط ادمین اصلی.", show_alert=True); return
    limit = 100 if c.data == "import_100" else MAX_NEWS
    AWAITING[c.from_user.id] = ("archive", limit)
    await c.message.answer(f"📥 فایل خروجی Telegram Desktop را بفرست.\n\nربات {'۱۰۰ خبر آخر' if limit == 100 else 'کل خبرها'} را از فایل استخراج می‌کند.\n\nفایل معمولاً result.json نام دارد.")
    await c.answer()


@router.message(F.document)
async def document_handler(message: Message):
    if not message.from_user or not is_primary(message.from_user.id):
        return
    uid = message.from_user.id
    waiting = AWAITING.get(uid)
    if not waiting or waiting[0] != "archive":
        return
    if uid in IMPORTING:
        await message.answer("⏳ یک عملیات آرشیو در حال اجراست.")
        return
    limit = waiting[1]
    doc = message.document
    filename = doc.file_name or "result.json"
    if Path(filename).suffix.lower() not in (".json", ".txt"):
        await message.answer("❌ فقط فایل JSON یا TXT ارسال کن.")
        return
    if doc.file_size and doc.file_size > 50 * 1024 * 1024:
        await message.answer("❌ حجم فایل بیشتر از ۵۰ مگابایت است.")
        return
    IMPORTING.add(uid)
    AWAITING.pop(uid, None)
    status = await message.answer("📥 در حال دریافت فایل...")
    try:
        f = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(f.file_path, buf)
        parsed = parse_archive(buf.getvalue(), filename)
        if not parsed:
            await status.edit_text("❌ هیچ پیام متنی قابل استفاده‌ای پیدا نشد.")
            return

        # مهم: برای درخواست ۱۰۰ خبر، آخرین ۱۰۰ پیام متن‌دار انتخاب می‌شوند.
        selected = parsed[-limit:]
        news = load_news()
        existing = {normalize(x.get("text", "")) for x in news}
        new_items = []
        skipped = 0
        nid = next_id(news)

        for index, record in enumerate(selected, 1):
            text = record["text"].strip()
            n = normalize(text)
            if not n or n in existing:
                skipped += 1
                continue
            # جلوگیری از ورود خبرهای بسیار مشابه داخل همان آرشیو
            duplicate = False
            for old in news[-1000:]:
                if similarity(text, old.get("text", "")) >= float(SETTINGS.get("similarity_threshold", 0.72)):
                    duplicate = True
                    break
            if duplicate:
                skipped += 1
                continue
            analysis = await analyze(text) if ai else None
            title = (analysis or {}).get("title") or text.splitlines()[0][:300]
            new_items.append({
                "id": nid,
                "title": title,
                "text": text,
                "url": "",
                "category": (analysis or {}).get("category"),
                "analysis": analysis,
                "source_date": record.get("date", ""),
                "telegram_message_id": record.get("id"),
                "imported": True,
                "added_by": uid,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            existing.add(n)
            nid += 1
            if ai and index % 5 == 0:
                await asyncio.sleep(0.2)

        final = (news + new_items)[-MAX_NEWS:]
        save_news(final)
        await status.edit_text(
            "✅ آرشیو با موفقیت وارد شد.\n\n"
            f"📄 پیام‌های متنی فایل: {len(parsed)}\n"
            f"📥 بررسی‌شده: {len(selected)}\n"
            f"🆕 واردشده: {len(new_items)}\n"
            f"🔁 تکراری/مشابه: {skipped}\n"
            f"📚 آرشیو فعلی: {len(final)}/{MAX_NEWS}\n\n"
            "💡 API_ID و API_HASH در این روش لازم نیست."
        )
    except json.JSONDecodeError:
        await status.edit_text("❌ result.json معتبر نیست.")
    except Exception:
        logger.exception("Import error")
        await status.edit_text("❌ خطا هنگام وارد کردن آرشیو. جزئیات را در Railway Logs ببین.")
    finally:
        IMPORTING.discard(uid)


@router.callback_query(F.data == "stats")
async def stats(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    news = load_news()
    await c.message.answer(f"📊 آمار آرشیو\n\n📚 تعداد خبرها: {len(news)}/{MAX_NEWS}\n🧠 هوش مصنوعی: {'فعال' if ai else 'غیرفعال'}")
    await c.answer()


@router.callback_query(F.data == "check")
async def check(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    AWAITING[c.from_user.id] = ("check",)
    await c.message.answer("📰 متن خبر را بفرست تا با آرشیو مقایسه کنم.")
    await c.answer()


@router.message(F.text | F.caption)
async def text_handler(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    uid = message.from_user.id
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return
    waiting = AWAITING.pop(uid, None)
    if waiting and waiting[0] == "check":
        news = load_news()
        matches = sorted(((similarity(text, x.get("text", "")), x) for x in news), key=lambda z: z[0], reverse=True)[:3]
        if matches and matches[0][0] >= float(SETTINGS.get("similarity_threshold", 0.72)):
            s, old = matches[0]
            await message.answer(f"🔴 خبر مشابه پیدا شد.\n\n📊 شباهت: {round(s*100)}%\n\n📰 خبر قبلی:\n{old.get('text','')}")
        else:
            await message.answer("🟢 خبر جدید به نظر می‌رسد.\n\nبرای ذخیره، می‌توانی آن را دوباره با دستور /save ارسال کنی.")
        return
    await message.answer("ℹ️ از /start برای باز کردن پنل استفاده کن.")


@router.message(Command("save"))
async def save_cmd(message: Message):
    if not message.from_user or not is_admin(message.from_user.id): return
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer("مثال: /save متن خبر")
        return
    news = load_news()
    if any(normalize(x.get("text", "")) == normalize(text) for x in news):
        await message.answer("🔴 این خبر قبلاً در آرشیو وجود دارد.")
        return
    item = {"id": next_id(news), "title": text.splitlines()[0][:300], "text": text, "url": "", "imported": False, "added_by": message.from_user.id, "created_at": datetime.now(timezone.utc).isoformat()}
    news.append(item)
    save_news(news)
    await message.answer(f"✅ ذخیره شد.\n📚 آرشیو: {len(load_news())}/{MAX_NEWS}")


async def main():
    logger.info("GAMEFA BOT STARTED | admins=%s | AI=%s", admins(), bool(ai))
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
