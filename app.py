from fastapi import FastAPI, Request
from telegram import Bot
import os

app = FastAPI()

TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)

@app.get("/")
async def home():
    return {"status": "activo"}

@app.post("/webhook")
async def webhook(req: Request):

    data = await req.json()

    print(data)

    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")

        await bot.send_message(
            chat_id=chat_id,
            text=f"Recibí: {text}"
        )

    return {"ok": True}
