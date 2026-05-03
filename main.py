import os
import re
import requests
from bs4 import BeautifulSoup
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

# ================= PARSING =================

def parse_article(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}

        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        # ✅ 1. og:image (главное изображение)
        image = None
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            image = og_image["content"]

        # ✅ 2. fallback — первая картинка
        if not image:
            img_tag = soup.find("img")
            if img_tag and img_tag.get("src"):
                image = img_tag["src"]

        # ✅ делаем ссылку абсолютной если нужно
        if image and image.startswith("/"):
            from urllib.parse import urljoin
            image = urljoin(url, image)

        # ✅ текст
        paragraphs = soup.find_all("p")
        text = "\n".join(
            p.get_text().strip()
            for p in paragraphs
            if len(p.get_text().strip()) > 40
        )

        if len(text) < 200:
            text = soup.get_text(separator="\n")

        if len(text) < 200:
            return None, None

        return text[:5000], image

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
Ты анонимный экспертный канал о спортивных добавках.

Сделай научный разбор:
- что изучали
- результаты
- вывод

Добавь 3-5 хештегов.

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
        return "❌ Ошибка генерации текста. Проверь IAM токен."

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
