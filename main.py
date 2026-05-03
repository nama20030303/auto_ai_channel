import asyncio
import os
import re
import random
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

BASE_POST_TIMES = ["12:00", "18:00"]
PUBMED_RSS = "https://pubmed.ncbi.nlm.nih.gov/rss/search/1R5oYJkXyZgKkZsXx0H8/?limit=10"

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=BOT_TOKEN)
app = FastAPI()
scheduler = AsyncIOScheduler()
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
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            promo_enabled INTEGER DEFAULT 0,
            promo_brand TEXT,
            promo_link TEXT,
            promo_ratio INTEGER DEFAULT 5
        )
        """)
        await db.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
        await db.commit()

# ================= QUEUE =================

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

# ================= PROMO =================

async def get_promo():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT promo_enabled, promo_brand, promo_link, promo_ratio FROM settings WHERE id=1") as cur:
            return await cur.fetchone()

async def set_promo(enabled, brand=None, link=None):
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        UPDATE settings
        SET promo_enabled=?, promo_brand=?, promo_link=?
        WHERE id=1
        """, (enabled, brand, link))
        await db.commit()

# ================= AI =================

async def generate_post(text):
    promo_enabled, brand, link, ratio = await get_promo()
    is_promo = promo_enabled and random.randint(1, ratio) == 1

    if is_promo:
        prompt = f"""
Сделай научный разбор исследования.
В конце мягко упомяни бренд {brand} и добавь ссылку {link}.
Добавь 3-5 хештегов.
Текст:
{text}
"""
    else:
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
        prompt=f"Minimalistic scientific fitness cover about {title}",
        size="1024x1024"
    )
    image_base64 = img.data[0].b64_json
    return base64.b64decode(image_base64)

# ================= STATS =================

async def save_post(message_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO posts (message_id) VALUES (?)", (message_id,))
        await db.commit()

async def update_views():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT id, message_id FROM posts ORDER BY id DESC LIMIT 5") as cur:
            rows = await cur.fetchall()
            for post_id, msg_id in rows:
                try:
                    msg = await bot.forward_message(
                        chat_id=ADMIN_ID,
                        from_chat_id=CHANNEL_ID,
                        message_id=msg_id
                    )
                    views = msg.views or 0
                    await db.execute("UPDATE posts SET views=? WHERE id=?", (views, post_id))
                    await db.commit()
                except:
                    pass

async def get_avg_views():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT AVG(views) FROM (SELECT views FROM posts ORDER BY id DESC LIMIT 5)") as cur:
            row = await cur.fetchone()
            return row[0] or 0

# ================= SMART SCHEDULE =================

async def adjust_schedule():
    avg = await get_avg_views()
    scheduler.remove_all_jobs()
    times = BASE_POST_TIMES.copy()

    if avg > 1500:
        times.append("22:00")

    if avg > 3000:
        times = ["10:00", "14:00", "18:00"]

    for t in times:
        hour, minute = map(int, t.split(":"))
        scheduler.add_job(auto_publish, "cron", hour=hour, minute=minute)

# ================= PUBLISH =================

async def publish_from_url(url):
    text = parse_article(url)
    if not text:
        return

    post = await generate_post(text)
    cover = await generate_cover(post[:80])

    msg = await bot.send_photo(CHANNEL_ID, cover, caption=post)
    await save_post(msg.message_id)

async def auto_publish():
    row = await get_next_url()
    if row:
        _, url = row
        await publish_from_url(url)
    else:
        url = get_pubmed_article()
        if url:
            await publish_from_url(url)

    await update_views()
    await adjust_schedule()

# ================= TELEGRAM =================

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return
        return await func(update, context)
    return wrapper

@admin_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text or ""
    urls = []

    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "url":
                urls.append(text[entity.offset: entity.offset + entity.length])

    if not urls:
        urls = re.findall(r'https?://[^\s]+', text)

    if not urls:
        await update.message.reply_text("Ссылка не найдена.")
        return

    url = urls[0]
    await update.message.reply_text("⏳ Генерирую пост...")

    article_text = parse_article(url)
    if not article_text:
        await update.message.reply_text("❌ Не удалось прочитать статью.")
        return

    post = await generate_post(article_text)
    preview_storage[ADMIN_ID] = {"url": url, "post": post}

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать", callback_data="approve")],
        [InlineKeyboardButton("📥 В очередь", callback_data="queue")]
    ])

    await update.message.reply_text(post, reply_markup=keyboard)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = preview_storage.get(ADMIN_ID)
    if not data:
        return

    if query.data == "approve":
        await publish_from_url(data["url"])
        preview_storage.pop(ADMIN_ID)
        await query.edit_message_text("✅ Опубликовано")

    elif query.data == "queue":
        await add_url(data["url"])
        preview_storage.pop(ADMIN_ID)
        await query.edit_message_text("📥 Добавлено в очередь")

@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_views()
    avg = await get_avg_views()
    await update.message.reply_text(f"📊 Средние просмотры (5 постов): {int(avg)}")

@admin_only
async def promo_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Используй: /promo_on Бренд ссылка")
        return
    brand = context.args[0]
    link = context.args[1]
    await set_promo(1, brand, link)
    await update.message.reply_text(f"✅ Рекламный режим включён: {brand}")

@admin_only
async def promo_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_promo(0, None, None)
    await update.message.reply_text("🚫 Рекламный режим отключён")

# ================= FASTAPI =================

@app.get("/")
def home():
    return {"status": "Bot running ✅"}

# ================= START =================

async def start_scheduler():
    await init_db()
    await adjust_schedule()
    scheduler.start()

async def start_telegram():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("promo_on", promo_on))
    application.add_handler(CommandHandler("promo_off", promo_off))

    await application.initialize()
    await application.start()
    await application.run_polling()

async def main():
    asyncio.create_task(start_scheduler())
    asyncio.create_task(start_telegram())

    port = int(os.environ.get("PORT", 10000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port)
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
