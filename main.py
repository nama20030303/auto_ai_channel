import os
import re
import base64
import requests
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import uvicorn

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

WEBHOOK_URL = "https://auto-ai-channel.onrender.com/webhook"

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

preview_storage = {}
DISCLAIMER = "\n\n⚠️ Материал носит информационный характер."

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
    )
    return response.choices[0].message.content + DISCLAIMER

async def generate_cover(title):
    img = await client.images.generate(
        model="gpt-image-1",
        prompt=f"Scientific minimalistic fitness cover about {title}",
        size="1024x1024"
    )
    return base64.b64decode(img.data[0].b64_json)

# ================= PARSE =================

def parse_article(url):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        text = "\n".join([p.get_text() for p in soup.find_all("p")])
        return text[:4000]
    except:
        return None

# ================= TELEGRAM =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text or ""
    urls = re.findall(r'https?://[^\s]+', text)

    if not urls:
        await update.message.reply_text("Ссылка не найдена.")
        return

    await update.message.reply_text("⏳ Генерирую пост...")

    article_text = parse_article(urls[0])
    if not article_text:
        await update.message.reply_text("Не удалось прочитать статью.")
        return

    post = await generate_post(article_text)
    preview_storage[ADMIN_ID] = {"post": post}

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

    cover = await generate_cover(data["post"][:80])

    await context.bot.send_photo(
        chat_id=CHANNEL_ID,
        photo=cover,
        caption=data["post"]
    )

    preview_storage.pop(ADMIN_ID)
    await query.edit_message_text("✅ Опубликовано")

telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
telegram_app.add_handler(CallbackQueryHandler(button_handler))

# ================= WEBHOOK =================

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.get("/")
async def health():
    return {"status": "running"}

# ================= STARTUP =================

@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(WEBHOOK_URL)

# ================= MAIN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
