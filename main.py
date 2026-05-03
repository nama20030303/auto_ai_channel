import asyncio
import os
from fastapi import FastAPI
import uvicorn

# ==== ТВОЙ КОД БОТА ИМПОРТИРУЙ СЮДА ====
from bot_logic import start_bot  # если бот в отдельном файле

app = FastAPI()

@app.get("/")
def home():
    return {"status": "Bot is running ✅"}

async def main():
    asyncio.create_task(start_bot())

    port = int(os.environ.get("PORT", 10000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port)
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
