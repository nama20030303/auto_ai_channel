import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

YANDEX_IAM_TOKEN = os.getenv("YANDEX_IAM_TOKEN")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

WEBHOOK_URL = "https://auto-ai-channel.onrender.com/webhook"

app = FastAPI()
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

preview_storage = {}
DISCLAIMER = "\n\n⚠️ Материал носит информационный характер."

# ================= UNIVERSAL PARSER =================

def parse_article(url):
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120 Safari/537.36"
            )
        }

        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        # ========= IMAGE SEARCH =========

        image = None

        # 1️⃣ og:image
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            image = og["content"]

        # 2️⃣ twitter:image
        if not image:
            tw = soup.find("meta", property="twitter:image")
            if tw and tw.get("content"):
                image = tw["content"]

        # 3️⃣ article image
        if not image:
            article = soup.find("article")
            if article:
                img = article.find("img")
                if img and img.get("src"):
                    image = img["src"]

        # 4️⃣ figure img
        if not image:
            figure = soup.find("figure")
            if figure:
                img = figure.find("img")
                if img and img.get("src"):
                    image = img["src"]

        # 5️⃣ fallback — первая нормальная картинка
        if not image:
            for img in soup.find_all("img"):
                src = img.get("src")
                if src and len(src) > 20 and not src.endswith(".svg"):
                    image = src
                    break

        # абсолютная ссылка
        if image:
            image = urljoin(url, image)

        # ========= TEXT SEARCH =========

        text_blocks = []

        # 1️⃣ article tag
        article = soup.find("article")
        if article:
            text_blocks = article.find_all("p")

        # 2️⃣ main content fallback
        if not text_blocks:
            main = soup.find("main")
            if main:
                text_blocks = main.find_all("p")

        # 3️⃣ general paragraphs
        if not text_blocks:
            text_blocks = soup.find_all("p")

        text = "\n".join(
            p.get_text().strip()
            for p in text_blocks
            if len(p.get_text().strip()) > 50
        )

        # 4️⃣ full page fallback
        if len(text) < 300:
            text = soup.get_text(separator="\n")

        text = text.strip()

        if len(text) < 300:
            print("PARSE ERROR: Not enough text")
            return None, None

        return text[:6000], image

    except Exception as e:
        print("PARSE ERROR:", e)
        return None, None

# ================= YANDEX GPT =================

def generate_post(text):
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    headers = {
        "Authorization": f"Bearer {YANDEX_IAM_TOKEN}",
        "Content-Type": "application/json"
    }

    prompt = f"""
Ты анонимный экспертный Telegram-канал о спортивных добавках.

Сделай научный разбор:
- что изучали
- результаты
- вывод

Добавь 3-5 релевантных хештегов.

Текст:
{text}
"""

    data = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt-lite/latest",
        "completionOptions": {
            "stream": False,
            "temperature": 0.7,
            "maxTokens": 1500
        },
        "messages": [
            {"role": "user", "text": prompt}
        ]
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status()

        result = response.json()
        return result["result"]["alternatives"][0]["message"]["text"] + DISCLAIMER

    except Exception as e:
        print("YANDEX ERROR:", e)
        return "❌ Ошибка генерации текста."

# ================= TELEGRAM =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text or ""
    urls = re.findall(r'https?://[^\s]+', text)

    if not urls:
        await update.message.reply_text("Ссылка не найдена.")
        return

    await update.message.reply_text("⏳ Анализирую статью...")

    article_text, image_url = parse_article(urls[0])

    if not article_text:
        await update.message.reply_text("❌ Не удалось прочитать статью.")
        return

    post = generate_post(article_text)

    preview_storage[ADMIN_ID] = {
        "post": post,
        "image": image_url
    }

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

    if data["image"]:
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=data["image"],
            caption=data["post"]
        )
    else:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=data["post"]
        )

    preview_storage.pop(ADMIN_ID)
    await query.edit_message_text("✅ Опубликовано")

telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
)
telegram_app.add_handler(
    CallbackQueryHandler(button_handler)
)

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

@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(WEBHOOK_URL)

# ================= MAIN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
