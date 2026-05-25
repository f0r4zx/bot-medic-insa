from fastapi import FastAPI, Request
from telegram import Bot
import os

app = FastAPI()

# Leer token desde Render Environment Variables
TOKEN = os.getenv("BOT_TOKEN")

# Debug para verificar que Render sí lo está leyendo
print("TOKEN:", TOKEN)

# Validación
if not TOKEN:
    raise ValueError("BOT_TOKEN no encontrado en Environment Variables")

# Inicializar bot
bot = Bot(token=TOKEN)

# Ruta principal
@app.get("/")
async def home():
    return {
        "status": "Bot Medic funcionando"
    }

# Webhook Telegram
@app.post("/webhook")
async def webhook(req: Request):

    data = await req.json()

    print("UPDATE:", data)

    # Verificar si viene mensaje
    if "message" in data:

        chat_id = data["message"]["chat"]["id"]

        text = data["message"].get("text", "")

        # Respuesta básica
        response = f"Recibí tu mensaje: {text}"

        # Comandos
        if text == "/start":
            response = (
                " Bienvenido a Bot Medic\n"
                "Tu asistente de medicamentos."
            )

        elif text == "/help":
            response = (
                "/start - Iniciar bot\n"
                "/help - Ver comandos\n"
                "/status - Estado del sistema"
            )

        elif text == "/status":
            response = " Sistema funcionando correctamente"

        # Enviar mensaje
        await bot.send_message(
            chat_id=chat_id,
            text=response
        )

    return {"ok": True}
