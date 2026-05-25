from fastapi import FastAPI, Request
from telegram import Bot
import os

app = FastAPI()

TOKEN = os.getenv("BOT_TOKEN")

print("TOKEN:", TOKEN)

bot = Bot(token=TOKEN)

# Base de datos temporal
medicamentos = {}

@app.get("/")
async def home():
    return {"status": "Bot Medic funcionando"}

@app.post("/webhook")
async def webhook(req: Request):

    data = await req.json()

    print(data)

    if "message" not in data:
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]

    text = data["message"].get("text", "").strip()

    response = "No entendí el comando."

    # Crear usuario si no existe
    if chat_id not in medicamentos:
        medicamentos[chat_id] = []

    # START
    if text == "/start":

        response = (
            "💊 Bienvenido a Bot Medic\n\n"
            "Comandos:\n"
            "/addmed nombre\n"
            "/listmed\n"
            "/deletemed nombre\n"
            "/status"
        )

    # STATUS
    elif text == "/status":

        response = "✅ Sistema funcionando correctamente"

    # ADD MED
    elif text.startswith("/addmed"):

        partes = text.split(maxsplit=1)

        if len(partes) < 2:

            response = (
                "❌ Debes escribir el nombre.\n\n"
                "Ejemplo:\n"
                "/addmed Paracetamol"
            )

        else:

            medicamento = partes[1]

            medicamentos[chat_id].append(medicamento)

            response = f"✅ Medicamento agregado: {medicamento}"

    # LIST MEDS
    elif text == "/listmed":

        lista = medicamentos[chat_id]

        if not lista:

            response = "📭 No tienes medicamentos guardados."

        else:

            response = "💊 Tus medicamentos:\n\n"

            for i, med in enumerate(lista, start=1):
                response += f"{i}. {med}\n"

    # DELETE MED
    elif text.startswith("/deletemed"):

        partes = text.split(maxsplit=1)

        if len(partes) < 2:

            response = (
                "❌ Debes escribir el medicamento.\n\n"
                "Ejemplo:\n"
                "/deletemed Paracetamol"
            )

        else:

            medicamento = partes[1]

            if medicamento in medicamentos[chat_id]:

                medicamentos[chat_id].remove(medicamento)

                response = f"🗑️ Eliminado: {medicamento}"

            else:

                response = "❌ Ese medicamento no existe."

    # Enviar respuesta
    await bot.send_message(
        chat_id=chat_id,
        text=response
    )

    return {"ok": True}
