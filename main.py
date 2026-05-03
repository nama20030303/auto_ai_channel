import asyncio
import os
import re
import requests
import feedparser
import base64
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

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

POST_TIMES = ["12:00", "18:00"]

PUBMED_RSS = "https://pubmed.ncbi.nlm.nih.gov/rss/search/1R5oYJkXyZgKkZsXx0H8/?limit=15"

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=BOT_TOKEN)
app = FastAPI()
DB = "posts.db"

preview_storage = {}

DISCLAIMER = "\n\n⚠️ Материал носит информационный характер."

# ================= DATABASE =================

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            published INTEGER DEFAULT 0
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            views INTEGER DEFAULT 0,
            published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        await db.commit()

async def save_post(message_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO posts (message_id) VALUES (?)", (message_id,))
        await db.commit()

async def update_views():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT id, message_id FROM posts") as cur:
            rows = await cur.fetchall()
            for row in rows:
                post_id, msg_id = row
                try:
                    msg = await bot.forward_message(
                        chat_id=ADMIN_ID,
                        from_chat_id=CHANNEL_ID,
                        message_id=msg_id
                    )
                    views = msg.views or 0
                    await db.execute(
                        "UPDATE posts SET views=? WHERE id=?",
                        (views, post_id)
                    )
                    await db.commit()
                except:
                    pass

async def get_stats():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT AVG(views), MAX(views) FROM posts") as cur:
            row = await cur.fetchone()
            return row

# ================= PARSING =================

def parse_article(url):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        text = "\n".join([p.get_text() for p in soup.find_all("p")])
        return text[:5000]
    except:
        return None

def get_pubmed_article():
    feed = feedparser.parse(PUBMED_RSS)
    if not feed.entries:
        return None
    return feed.entries[0].link

# ================= AI =================

async def generate_post(text):
    prompt = f"""
Сделай научный разбор исследования.
Добавь 3-5 хештегов.
Текст:
{text}
"""
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    )
    return response.choices[0].message.content + DISCLAIMER

async def generate_cover(title):
    img = await client.images.generate(
        model="gpt-image-1",
        prompt=f"Scientific minimalistic fitness cover image about {title}",
        size="1024x1024"
    )
    image_base64 = img.data[0].b64_json
    return base64.b64decode(image_base64)

# ================= PUBLISH =================

async def publish_from_url(url):
    text = parse_article(url)
    if not text:
        return

    post = await generate_post(text)
    cover = await generate_cover(post[:100])

    msg = await bot.send_photo(
        CHANNEL_ID,
        cover,
        caption=post
    )

    await save_post(msg.message_id)

async def auto_publish():
    url = get_pubmed_article()
    if url:
        await publish_from_url(url)

# ================= TELEGRAM =================

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
        return

    url = urls[0]
    text = parse_article(url)
    if not text:
        return

    post = await generate_post(text)
    preview_storage[ADMIN_ID] = {"url": url, "post": post}

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать", callback_data="approve")]
    ])

    await update.message.reply_text(post, reply_markup=keyboard)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = preview_storage.get(ADMIN_ID)
    if not data:
        return

    await publish_from_url(data["url"])
    preview_storage.pop(ADMIN_ID)
    await query.edit_message_text("✅ Опубликовано")

@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_views()
    avg, maxv = await get_stats()
    await update.message.reply_text(
        f"📊 Средние просмотры: {int(avg or 0)}\n🔥 Лучший пост: {int(maxv or 0)}"
    )

# ================= FASTAPI =================

@app.get("/")
def home():
    return {"status": "Bot running ✅"}

# ================= START =================

async def start_scheduler():
    await init_db()
    scheduler = AsyncIOScheduler()

    for t in POST_TIMES:
        hour, minute = map(int, t.split(":"))
        scheduler.add_job(auto_publish, "cron", hour=hour, minute=minute)

    scheduler.start()

async def start_telegram():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("stats", stats_command))

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
