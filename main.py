import asyncio
import os
import requests
import feedparser
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiosqlite
from fastapi import FastAPI
import uvicorn

# ================== ПЕРЕМЕННЫЕ ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

RSS_FEEDS = [
    "https://lenta.ru/rss",
    "https://ria.ru/export/rss2/archive/index.xml"
]

POST_INTERVAL = 30  # минут

# ================== ИНИЦИАЛИЗАЦИЯ ==================

bot = Bot(token=BOT_TOKEN)
client = AsyncOpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

DB = "posts.db"

# ================== БАЗА ==================

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            image TEXT,
            published INTEGER DEFAULT 0
        )
        """)
        await db.commit()

async def add_post(text, image):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO posts (text, image) VALUES (?, ?)",
            (text, image)
        )
        await db.commit()

async def get_next_post():
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT id, text, image FROM posts WHERE published=0 LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                await db.execute(
                    "UPDATE posts SET published=1 WHERE id=?",
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
    Стиль — живой и вовлекающий.
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

# ================== RSS ==================

async def collect_news():
    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)

        for entry in feed.entries[:3]:
            text, image = parse_article(entry.link)
            if text:
                post = await generate_post(text)
                await add_post(post, image)

# ================== ПУБЛИКАЦИЯ ==================

async def publish():
    post = await get_next_post()
    if post:
        _, text, image = post
        try:
            if image:
                await bot.send_photo(CHANNEL_ID, image, caption=text)
            else:
                await bot.send_message(CHANNEL_ID, text)
        except Exception as e:
            print("Ошибка публикации:", e)

# ================== FASTAPI ==================

@app.get("/")
def home():
    return {"status": "Bot is running ✅"}

# ================== ЗАПУСК ==================

async def start_bot():
    await init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(collect_news, "interval", hours=2)
    scheduler.add_job(publish, "interval", minutes=POST_INTERVAL)
    scheduler.start()

    print("Бот полностью автономен ✅")

async def main():
    asyncio.create_task(start_bot())

    port = int(os.environ.get("PORT", 10000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port)
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
