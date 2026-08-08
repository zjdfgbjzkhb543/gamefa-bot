import asyncio, csv, io, json, logging, os, re, zipfile
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

BOT_TOKEN=os.getenv("BOT_TOKEN","").strip()
DATA_DIR=Path(os.getenv("DATA_DIR","/data")); DATA_DIR.mkdir(parents=True,exist_ok=True)
MAX_NEWS=1000
NEWS_FILE=DATA_DIR/"news.json"
def load():
    try: return json.loads(NEWS_FILE.read_text(encoding="utf-8")) if NEWS_FILE.exists() else []
    except: return []
def save(x):
    tmp=NEWS_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(x[-MAX_NEWS:],ensure_ascii=False,indent=2),encoding="utf-8"); tmp.replace(NEWS_FILE)
def admins():
    return {int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()}
def norm(s):
    s=(s or "").lower()
    for a,b in {"ي":"ی","ى":"ی","ك":"ک","ۀ":"ه","\u200c":" "}.items(): s=s.replace(a,b)
    return re.sub(r"\s+"," ",s).strip()
def sim(a,b):
    a,b=norm(a),norm(b)
    if not a or not b:return 0
    A,B=set(a.split()),set(b.split())
    jac=len(A&B)/max(1,len(A|B))
    return .55*jac+.45*SequenceMatcher(None,a,b).ratio()

if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN environment variable is missing")
bot=Bot(BOT_TOKEN); dp=Dispatcher(); r=Router(); dp.include_router(r)

def kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 وارد کردن آرشیو",callback_data="import")],
        [InlineKeyboardButton(text="📚 آرشیو",callback_data="archive"),InlineKeyboardButton(text="📊 آمار",callback_data="stats")]
    ])

@r.message(CommandStart())
async def start(m:Message):
    if not m.from_user or m.from_user.id not in admins(): return await m.answer("⛔ دسترسی ندارید.")
    await m.answer("🤖 ربات ضدخبرتکراری گیمفا آماده است.\n\nفایل آرشیو را می‌توانی مستقیم برای من بفرستی.",reply_markup=kb())

@r.callback_query(F.data=="import")
async def imp(c:CallbackQuery):
    await c.message.answer("📥 فایل آرشیو را همینجا ارسال کن.\n\nفرمت‌های قابل قبول: JSON، TXT، CSV یا ZIP شامل این فایل‌ها.")
    await c.answer()

@r.callback_query(F.data=="stats")
async def stats(c:CallbackQuery):
    await c.message.answer(f"📊 آرشیو: {len(load())}/{MAX_NEWS} خبر")
    await c.answer()

@r.callback_query(F.data=="archive")
async def archive(c:CallbackQuery):
    n=load()
    if not n: return await c.message.answer("📚 آرشیو خالی است.")
    await c.message.answer("📚 آخرین اخبار:\n\n"+"\n".join(f"#{x['id']} — {x.get('title','بدون عنوان')}" for x in n[-10:]))
    await c.answer()

def parse_text(name,raw):
    ext=Path(name).suffix.lower()
    if ext==".json":
        d=json.loads(raw)
        if isinstance(d,dict):
            for k in ("news","items","articles","messages","data"):
                if isinstance(d.get(k),list): d=d[k]; break
            else:d=[d]
        out=[]
        for x in d if isinstance(d,list) else []:
            if isinstance(x,str): t=x.strip()
            elif isinstance(x,dict):
                title=x.get("title",""); body=x.get("text") or x.get("body") or x.get("content") or x.get("message") or x.get("caption") or ""
                t=(title+"\n\n"+body).strip() if title and body and title not in body else (body or title).strip()
            else: continue
            if len(t)>=10: out.append(t)
        return out
    if ext==".csv":
        out=[]
        for row in csv.DictReader(io.StringIO(raw)):
            t=row.get("text") or row.get("body") or row.get("content") or row.get("message") or row.get("title")
            if t and len(t.strip())>=10: out.append(t.strip())
        return out
    return [x.strip() for x in re.split(r"\n\s*\n+",raw.replace("\r","")) if len(x.strip())>=10]

def parse_zip(data):
    out=[]
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith("/"): continue
            if Path(name).suffix.lower() in (".json",".txt",".csv",".md",".log"):
                out += parse_text(name,z.read(name).decode("utf-8-sig","ignore"))
    return out

@r.message(F.document)
async def document(m:Message):
    if not m.from_user or m.from_user.id not in admins(): return
    name=m.document.file_name or "archive"
    ext=Path(name).suffix.lower()
    if ext not in (".json",".txt",".csv",".zip",".md",".log"):
        return await m.answer("❌ فقط JSON، TXT، CSV یا ZIP قابل قبول است.")
    status=await m.answer("📥 فایل دریافت شد؛ در حال پردازش...")
    f=await bot.get_file(m.document.file_id); buf=io.BytesIO()
    await bot.download_file(f.file_path,buf)
    try:
        texts=parse_zip(buf.getvalue()) if ext==".zip" else parse_text(name,buf.getvalue().decode("utf-8-sig","ignore"))
    except Exception as e:
        logging.exception("parse"); return await status.edit_text(f"❌ خطا در خواندن فایل: {e}")
    unique=[]; seen=set()
    for t in texts:
        k=norm(t)
        if k and k not in seen: seen.add(k); unique.append(t)
    news=load(); existing={norm(x.get("text","")) for x in news}
    added=old=internal=len(texts)-len(unique)
    added=0; old=0
    next_id=max([x.get("id",0) for x in news if isinstance(x.get("id"),int)] or [0])+1
    for t in unique:
        k=norm(t)
        if k in existing: old+=1; continue
        news.append({"id":next_id,"title":t.splitlines()[0][:300],"text":t,"url":"","created_at":datetime.now(timezone.utc).isoformat(),"imported_from":name})
        next_id+=1; added+=1; existing.add(k)
    overflow=max(0,len(news)-MAX_NEWS); news=news[-MAX_NEWS:]; save(news)
    await status.edit_text(f"✅ آرشیو وارد شد.\n\n📥 رکوردهای فایل: {len(texts)}\n➕ جدید: {added}\n♻️ تکراری داخل فایل: {internal}\n📚 قبلاً موجود: {old}\n🗑 حذف به‌دلیل سقف ۱۰۰۰: {overflow}\n\n📊 آرشیو فعلی: {len(news)}/{MAX_NEWS}")

@r.message(F.text)
async def text(m:Message):
    if not m.from_user or m.from_user.id not in admins(): return
    t=(m.text or "").strip()
    if not t or t.startswith("/"): return
    n=load()
    matches=sorted(((sim(t,x.get("text","")),x) for x in n),reverse=True,key=lambda z:z[0])[:3]
    if matches and matches[0][0]>=.72:
        s,x=matches[0]
        await m.answer(f"🔴 خبر مشابه پیدا شد.\n\n📊 شباهت: {round(s*100)}%\n\n📰 خبر قبلی:\n{x.get('text','')}")
    else: await m.answer("🟢 خبر جدید به نظر می‌رسد.")

async def main():
    logging.basicConfig(level=logging.INFO)
    logging.info("GAMEFA BOT STARTING | admins=%s | news=%s",admins(),len(load()))
    await dp.start_polling(bot)
if __name__=="__main__": asyncio.run(main())
