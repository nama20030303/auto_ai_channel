import os
import re
import random
import asyncio
import base64
import requests
import feedparser
import aiosqlite
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

PUBMED_RSS = "https://pubmed.ncbi.nlm.nih.gov/rss/search/1R5oYJkXyZgKkZsXx0H8/?limit=10"

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
scheduler = AsyncIOScheduler()
DB = "posts.db"
preview_storage = {}
DISCLAIMER = "\n\n⚠️ Материал носит информационный характер."

# ================= DATABASE =================

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            views INTEGER DEFAULT 0
        )
        """)
        await db.commit()

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
        prompt=f"Minimalistic scientific fitness cover about {title}",
        size="1024x1024"
    )
    return base64.b64decode(img.data[0].b64_json)

# ================= AUTO PUBLISH =================

async def auto_publish(application):
    url = get_pubmed_article()
    if not url:
        return

    text = parse_article(url)
    if not text:
        return

    post = await generate_post(text)
    cover = await generate_cover(post[:80])

    await application.bot.send_photo(
        chat_id=CHANNEL_ID,
        photo=cover,
        caption=post
    )

# ================= TELEGRAM =================

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return
        return await func(update, context)
    return wrapper

@admin_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = re.findall(r'https?://[^\s]+', text)

    if not urls:
        await update.message.reply_text("Ссылка не найдена.")
        return

    await update.message.reply_text("⏳ Генерирую пост...")

    article_text = parse_article(urls[0])
    if not article_text:
        await update.message.reply_text("❌ Не удалось прочитать статью.")
        return

    post = await generate_post(article_text)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать", callback_data="approve")]
    ])

    preview_storage[ADMIN_ID] = {"url": urls[0], "post": post}

    await update.message.reply_text(post, reply_markup=keyboard)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = preview_storage.get(ADMIN_ID)
    if not data:
        return

    cover = await generate_cover(data["post"][:80])

    await context.bot.send_photo(
        chat_id=CHANNEL_ID,
        photo=cover,
        caption=data["post"]
    )

    preview_storage.pop(ADMIN_ID)
    await query.edit_message_text("✅ Опубликовано")

# ================= MAIN =================

async def post_init(application):
    await init_db()
    scheduler.add_job(auto_publish, "cron", hour=12, minute=0, args=[application])
    scheduler.add_job(auto_publish, "cron", hour=18, minute=0, args=[application])
    scheduler.start()

def main():
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))

    # ✅ ВАЖНО: встроенный webserver для Render
    port = int(os.environ.get("PORT", 10000))
    application.run_polling(stop_signals=None)

if __name__ == "__main__":
    main()
