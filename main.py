import asyncio
import os
import re
import requests
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiosqlite
from fastapi import FastAPI, Request
import uvicorn
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

# ================== ПЕРЕМЕННЫЕ ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

POST_INTERVAL = 30  # минут

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=BOT_TOKEN)
app = FastAPI()
DB = "posts.db"

# ================== БАЗА ==================

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
        await db.execute(
            "INSERT INTO queue (url) VALUES (?)",
            (url,)
        )
        await db.commit()

async def get_next_url():
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT id, url FROM queue WHERE published=0 LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                await db.execute(
                    "UPDATE queue SET published=1 WHERE id=?",
                    (row[0],)
                )
                await db.commit()
            return row

# ================== ПАРСИНГ ==================

def parse_article(url):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        text = "\n".join([p.get_text() for p in soup.find_all("p")])
        img = soup.find("img")
        image = img["src"] if img and img.get("src") else None

        return text[:6000], image
    except:
        return None, None

# ================== ИИ ==================

async def generate_post(text):
    prompt = f"""
    Сделай короткий интересный Telegram-пост.
    До 1200 символов.
    Добавь эмодзи.
    В конце задай вопрос.

    Текст:
    {text}
    """

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7
    )

    return response.choices[0].message.content

# ================== ПУБЛИКАЦИЯ ==================

async def publish():
    row = await get_next_url()
    if not row:
        return

    _, url = row
    text, image = parse_article(url)

    if not text:
        return

    post = await generate_post(text)

    try:
        if image:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=image, caption=post)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=post)
    except Exception as e:
        print("Ошибка публикации:", e)

# ================== TELEGRAM ОБРАБОТКА ==================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    urls = re.findall(r'(https?://\S+)', text)

    if not urls:
        await update.message.reply_text("Пришли ссылку на статью.")
        return

    for url in urls:
        await add_url(url)

    await update.message.reply_text(f"✅ Добавлено ссылок: {len(urls)}")

# ================== FASTAPI ==================

@app.get("/")
def home():
    return {"status": "Bot running ✅"}

# ================== ЗАПУСК ==================

async def start_scheduler():
    await init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(publish, "interval", minutes=POST_INTERVAL)
    scheduler.start()

async def start_telegram():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
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
