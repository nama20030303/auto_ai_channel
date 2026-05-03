import asyncio
import os
import re
import requests
import feedparser
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiosqlite
from fastapi import FastAPI
import uvicorn
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ================== CONFIG ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

POST_TIMES = ["12:00", "18:00"]

PUBMED_RSS = "https://pubmed.ncbi.nlm.nih.gov/rss/search/1R5oYJkXyZgKkZsXx0H8/?limit=15&utm_campaign=pubmed-2&fc=20240101000000"

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=BOT_TOKEN)
app = FastAPI()
DB = "posts.db"

preview_storage = {}

DISCLAIMER = "\n\n⚠️ Материал носит информационный характер. Возможны риски."

# ================== DATABASE ==================

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            published INTEGER DEFAULT 0
        )
        """)
        await db.commit()

async def add_url(url):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO queue (url) VALUES (?)", (url,))
        await db.commit()

async def get_next_url():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT id, url FROM queue WHERE published=0 LIMIT 1") as cur:
            row = await cur.fetchone()
            if row:
                await db.execute("UPDATE queue SET published=1 WHERE id=?", (row[0],))
                await db.commit()
            return row

async def count_queue():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT COUNT(*) FROM queue WHERE published=0") as cur:
            row = await cur.fetchone()
            return row[0]

# ================== PARSING ==================

def parse_article(url):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        text = "\n".join([p.get_text() for p in soup.find_all("p")])
        return text[:6000]
    except:
        return None

def get_pubmed_article():
    feed = feedparser.parse(PUBMED_RSS)
    if not feed.entries:
        return None
    return feed.entries[0].link

# ================== AI ==================

async def generate_post(text):
    prompt = f"""
Ты экспертный анонимный канал о спортивных добавках.

Сделай научный разбор:
- что изучали
- результаты с цифрами
- вывод

Добавь 3-5 релевантных хештегов в конце.
Без медицинских обещаний.

Текст:
{text}
"""
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    )

    content = response.choices[0].message.content
    return content + DISCLAIMER

# ================== PUBLISH ==================

async def publish_from_url(url):
    text = parse_article(url)
    if not text:
        return

    post = await generate_post(text)
    await bot.send_message(CHANNEL_ID, post)

async def auto_publish():
    row = await get_next_url()
    if row:
        _, url = row
        await publish_from_url(url)
    else:
        url = get_pubmed_article()
        if url:
            await publish_from_url(url)

# ================== TELEGRAM ==================

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return
        return await func(update, context)
    return wrapper

@admin_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    urls = re.findall(r'(https?://\S+)', update.message.text)
    if not urls:
        await update.message.reply_text("Пришли ссылку.")
        return

    url = urls[0]
    text = parse_article(url)
    if not text:
        await update.message.reply_text("Не удалось прочитать статью.")
        return

    post = await generate_post(text)

    preview_storage[ADMIN_ID] = {"url": url, "post": post}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data="approve"),
            InlineKeyboardButton("📥 В очередь", callback_data="queue"),
        ]
    ])

    await update.message.reply_text(post, reply_markup=keyboard)

@admin_only
async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = await count_queue()
    await update.message.reply_text(f"📦 В очереди: {count}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    data = preview_storage.get(ADMIN_ID)
    if not data:
        await query.edit_message_text("Нет активного предпросмотра.")
        return

    if query.data == "approve":
        await bot.send_message(CHANNEL_ID, data["post"])
        preview_storage.pop(ADMIN_ID)
        await query.edit_message_text("✅ Опубликовано")

    elif query.data == "queue":
        await add_url(data["url"])
        preview_storage.pop(ADMIN_ID)
        await query.edit_message_text("📥 Добавлено в очередь")

# ================== FASTAPI ==================

@app.get("/")
def home():
    return {"status": "Bot running ✅"}

# ================== START ==================

async def start_scheduler():
    await init_db()
    scheduler = AsyncIOScheduler()

    for time_str in POST_TIMES:
        hour, minute = map(int, time_str.split(":"))
        scheduler.add_job(auto_publish, "cron", hour=hour, minute=minute)

    scheduler.start()

async def start_telegram():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CallbackQueryHandler(button_handler))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

async def main():
    asyncio.create_task(start_scheduler())
    asyncio.create_task(start_telegram())

    port = int(os.environ.get("PORT", 10000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port)
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
