from fastapi import FastAPI, Request
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta
import os
import re

# ══════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════════════════════
TOKEN = os.getenv("BOT_TOKEN")
bot   = Bot(token=TOKEN)

# ══════════════════════════════════════════════════════════
#  BASE DE DATOS EN MEMORIA
# ══════════════════════════════════════════════════════════
usuarios: dict           = {}
senales_pendientes: list = []

# ══════════════════════════════════════════════════════════
#  ESTADO CONVERSACIONAL
#  Guarda en qué paso del flujo está cada usuario.
#
#  conversacion[chat_id] = {
#    "flujo":   "setdate" | "settime" | "profile" | None,
#    "paso":    1 | 2 | 3 | 4,
#    "datos":   { ... datos temporales del flujo ... }
#  }
# ══════════════════════════════════════════════════════════
conversacion: dict = {}

# ══════════════════════════════════════════════════════════
#  MESES EN ESPAÑOL — para parsear fechas naturales
# ══════════════════════════════════════════════════════════
MESES = {
    "enero": 1,   "ene": 1,   "jan": 1,
    "febrero": 2, "feb": 2,
    "marzo": 3,   "mar": 3,
    "abril": 4,   "abr": 4,   "apr": 4,
    "mayo": 5,    "may": 5,
    "junio": 6,   "jun": 6,
    "julio": 7,   "jul": 7,
    "agosto": 8,  "ago": 8,   "aug": 8,
    "septiembre": 9, "sep": 9, "sept": 9,
    "octubre": 10,   "oct": 10,
    "noviembre": 11, "nov": 11,
    "diciembre": 12, "dic": 12, "dec": 12,
}

NOMBRE_MES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
    5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
    9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
}

# ══════════════════════════════════════════════════════════
#  MENSAJES DE AYUDA POR COMANDO
# ══════════════════════════════════════════════════════════
AYUDA = {
    "/addmed": (
        "➕ *Agregar medicamento*\n\n"
        "Registra un nuevo medicamento en tu lista.\n\n"
        "📌 *Uso:*\n"
        "`/addmed <nombre>`\n\n"
        "📋 *Ejemplo:*\n"
        "`/addmed Paracetamol`\n\n"
        "💡 Después de agregarlo, configura sus horarios con `/settime`\n"
        "y las fechas del tratamiento con `/setdate`."
    ),
    "/deletemed": (
        "🗑️ *Eliminar medicamento*\n\n"
        "Elimina un medicamento y todos sus horarios y fechas.\n\n"
        "📌 *Uso:*\n"
        "`/deletemed <nombre>`\n\n"
        "📋 *Ejemplo:*\n"
        "`/deletemed Paracetamol`\n\n"
        "⚠️ Esta acción no se puede deshacer.\n"
        "Si solo quieres detenerlo temporalmente, usa `/pause`."
    ),
    "/settime": (
        "⏰ *Configurar horarios de recordatorio*\n\n"
        "Asigna una o varias horas a un medicamento.\n\n"
        "📌 *Uso:*\n"
        "`/settime <nombre> <HH:MM> [HH:MM] ...`\n\n"
        "📋 *Ejemplos:*\n"
        "`/settime Paracetamol 08:00`\n"
        "`/settime Ibuprofeno 07:30 13:00 20:00`\n\n"
        "🕐 Usa formato de 24 horas."
    ),
    "/setdate": (
        "📅 *Configurar duración del tratamiento*\n\n"
        "El bot te guiará paso a paso con preguntas.\n"
        "Solo escribe `/setdate` y sigue las instrucciones."
    ),
    "/pause": (
        "⏸ *Pausar recordatorios*\n\n"
        "Detiene temporalmente el dispensador para ese medicamento.\n\n"
        "📌 *Uso:*\n"
        "`/pause <nombre>`\n\n"
        "📋 *Ejemplo:*\n"
        "`/pause Paracetamol`\n\n"
        "▶️ Para reactivarlo: `/resume Paracetamol`"
    ),
    "/resume": (
        "▶️ *Reactivar recordatorios*\n\n"
        "Vuelve a activar el dispensador para un medicamento pausado.\n\n"
        "📌 *Uso:*\n"
        "`/resume <nombre>`\n\n"
        "📋 *Ejemplo:*\n"
        "`/resume Paracetamol`"
    ),
    "/profile": (
        "👤 *Configurar perfil del paciente*\n\n"
        "📌 *Uso:*\n"
        "`/profile <nombre> <edad> <notas>`\n\n"
        "📋 *Ejemplos:*\n"
        "`/profile Juan 30`\n"
        "`/profile Maria 45 Diabética, alérgica a penicilina`\n\n"
        "Escribe `/profile` solo para ver tu perfil actual."
    ),
}

# ══════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════
scheduler = AsyncIOScheduler(timezone="America/El_Salvador")


def registrar_todos_los_horarios():
    scheduler.remove_all_jobs()
    for chat_id, data in usuarios.items():
        for med_nombre, med in data.get("meds", {}).items():
            if med.get("pausado", False):
                continue
            for horario in med.get("horarios", []):
                hora, minuto = horario.split(":")
                scheduler.add_job(
                    disparar_medicamento,
                    CronTrigger(hour=int(hora), minute=int(minuto)),
                    id=f"{chat_id}_{med_nombre}_{horario}",
                    replace_existing=True,
                    args=[chat_id, med_nombre, horario]
                )


async def disparar_medicamento(chat_id: int, med_nombre: str, horario: str):
    med = usuarios.get(chat_id, {}).get("meds", {}).get(med_nombre)
    if not med:
        return
    hoy = date.today()
    fi  = med.get("fecha_inicio")
    ff  = med.get("fecha_fin")
    if fi and hoy < date.fromisoformat(fi):
        return
    if ff and hoy > date.fromisoformat(ff):
        return

    nombre_p = usuarios[chat_id].get("profile", {}).get("nombre", "")
    saludo   = f"*{nombre_p}*, " if nombre_p else ""

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"⏰ {saludo}¡Es hora de tu medicamento!\n\n"
            f"💊 *{med_nombre}*\n"
            f"🕐 {horario}\n\n"
            f"_El dispensador se activará en unos segundos..._"
        ),
        parse_mode="Markdown"
    )
    senales_pendientes.append({
        "medicamento": med_nombre,
        "horario":     horario,
        "timestamp":   datetime.now().isoformat(),
        "chat_id":     chat_id
    })


# ══════════════════════════════════════════════════════════
#  LIFESPAN
# ══════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    registrar_todos_los_horarios()
    print("[BOT MEDIC] Scheduler iniciado.")
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


# ══════════════════════════════════════════════════════════
#  ENDPOINT ESP32
# ══════════════════════════════════════════════════════════
@app.get("/")
async def home():
    return {"status": "Bot Medic funcionando ✅"}


@app.get("/esp32/signal")
async def esp32_signal():
    if senales_pendientes:
        return {"dispensar": True, "datos": senales_pendientes.pop(0)}
    return {"dispensar": False}


# ══════════════════════════════════════════════════════════
#  HELPERS GENERALES
# ══════════════════════════════════════════════════════════
def init_user(chat_id):
    if chat_id not in usuarios:
        usuarios[chat_id] = {"profile": {}, "meds": {}}
    if chat_id not in conversacion:
        conversacion[chat_id] = {"flujo": None, "paso": 0, "datos": {}}


def limpiar_conversacion(chat_id):
    conversacion[chat_id] = {"flujo": None, "paso": 0, "datos": {}}


def fmt_fecha(iso) -> str:
    if not iso:
        return "—"
    try:
        d = date.fromisoformat(iso)
        return f"{d.day} de {NOMBRE_MES[d.month]} de {d.year}"
    except Exception:
        return iso


def tratamiento_activo(med: dict) -> bool:
    hoy = date.today()
    fi  = med.get("fecha_inicio")
    ff  = med.get("fecha_fin")
    if fi and hoy < date.fromisoformat(fi):
        return False
    if ff and hoy > date.fromisoformat(ff):
        return False
    return True


def estado_med(med: dict) -> str:
    if med["pausado"]:
        return "⏸ Pausado"
    if not tratamiento_activo(med):
        return "⛔ Tratamiento terminado"
    if not med["horarios"]:
        return "⚠️ Sin horarios"
    return "✅ Activo"


def _hora_valida(h: str) -> bool:
    try:
        datetime.strptime(h, "%H:%M")
        return True
    except ValueError:
        return False


# ══════════════════════════════════════════════════════════
#  PARSER DE FECHA EN LENGUAJE NATURAL
#
#  Acepta formatos como:
#  "1 mayo"  "1 de mayo"  "01/05"  "01/05/2025"
#  "1"  (solo día → asume mes actual o próximo)
#  "mañana"  "hoy"  "pasado mañana"
# ══════════════════════════════════════════════════════════
def parsear_fecha(texto: str) -> date | None:
    texto = texto.strip().lower()
    hoy   = date.today()

    # Palabras clave
    if texto in ("hoy", "today"):
        return hoy
    if texto in ("mañana", "manana", "tomorrow"):
        return hoy + timedelta(days=1)
    if texto in ("pasado mañana", "pasado manana"):
        return hoy + timedelta(days=2)

    # Formato DD/MM o DD/MM/YYYY
    m = re.match(r"^(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?$", texto)
    if m:
        dia, mes = int(m.group(1)), int(m.group(2))
        anio = int(m.group(3)) if m.group(3) else hoy.year
        if anio < 100:
            anio += 2000
        try:
            return date(anio, mes, dia)
        except ValueError:
            return None

    # Formato YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", texto)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # Formato "1 mayo", "1 de mayo", "1 de mayo 2025"
    m = re.match(
        r"^(\d{1,2})\s+(?:de\s+)?([a-záéíóúü]+)(?:\s+(?:de\s+)?(\d{2,4}))?$",
        texto
    )
    if m:
        dia      = int(m.group(1))
        mes_txt  = m.group(2)
        anio_txt = m.group(3)
        mes      = MESES.get(mes_txt)
        if not mes:
            return None
        anio = int(anio_txt) if anio_txt else hoy.year
        if anio < 100:
            anio += 2000
        try:
            d = date(anio, mes, dia)
            # Si la fecha ya pasó este año y no se especificó año, usar año siguiente
            if d < hoy and not anio_txt:
                d = date(anio + 1, mes, dia)
            return d
        except ValueError:
            return None

    # Solo número (día) → asume mes actual, si ya pasó usa el próximo mes
    m = re.match(r"^(\d{1,2})$", texto)
    if m:
        dia = int(m.group(1))
        try:
            d = date(hoy.year, hoy.month, dia)
            if d < hoy:
                # Avanzar al próximo mes
                if hoy.month == 12:
                    d = date(hoy.year + 1, 1, dia)
                else:
                    d = date(hoy.year, hoy.month + 1, dia)
            return d
        except ValueError:
            return None

    return None


# ══════════════════════════════════════════════════════════
#  FLUJO CONVERSACIONAL: /setdate
#
#  Paso 1 — Pedir medicamento (o ya lo tiene del comando)
#  Paso 2 — ¿Qué día comienza el tratamiento?
#  Paso 3 — ¿Cuántos días dura?
#  Paso 4 — Confirmación (sí / no)
# ══════════════════════════════════════════════════════════
async def flujo_setdate(chat_id: int, texto: str) -> str:
    conv  = conversacion[chat_id]
    datos = conv["datos"]
    meds  = usuarios[chat_id]["meds"]
    paso  = conv["paso"]

    # Cancelar en cualquier momento
    if texto.lower() in ("/cancelar", "cancelar", "cancel", "salir"):
        limpiar_conversacion(chat_id)
        return (
            "❌ *Configuración de fechas cancelada.*\n\n"
            "Puedes iniciarla de nuevo con `/setdate`."
        )

    # ── PASO 1: ¿Para qué medicamento? ──────────────────
    if paso == 1:
        if not meds:
            limpiar_conversacion(chat_id)
            return (
                "📭 No tienes medicamentos registrados.\n\n"
                "Agrega uno primero con `/addmed nombre`."
            )

        # Si el usuario escribió el nombre directamente
        nombre = texto.strip()
        if nombre not in meds:
            # Mostrar lista para que elija
            lista = "\n".join(f"  • {n}" for n in meds.keys())
            return (
                f"❓ No encontré *{nombre}* en tu lista.\n\n"
                f"Tus medicamentos registrados son:\n{lista}\n\n"
                f"Escribe exactamente el nombre del medicamento\n"
                f"o escribe *cancelar* para salir."
            )

        datos["med"]  = nombre
        conv["paso"]  = 2
        return (
            f"📅 *Configurando tratamiento para: {nombre}*\n\n"
            f"*Paso 1 de 3 — Fecha de inicio*\n\n"
            f"¿Qué día comienza el tratamiento?\n\n"
            f"Puedes escribirlo como:\n"
            f"  • `hoy` o `mañana`\n"
            f"  • `1 mayo` · `15 de junio` · `3 julio 2025`\n"
            f"  • `01/06` · `15/06/2025`\n"
            f"  • Solo el número del día: `1`, `15`, `28`\n\n"
            f"_Escribe *cancelar* en cualquier momento para salir._"
        )

    # ── PASO 2: Fecha de inicio ──────────────────────────
    elif paso == 2:
        fecha = parsear_fecha(texto)
        if not fecha:
            return (
                f"❌ No pude entender esa fecha: `{texto}`\n\n"
                f"Intenta con alguno de estos formatos:\n"
                f"  • `hoy` · `mañana`\n"
                f"  • `1 mayo` · `15 de junio`\n"
                f"  • `01/06` · `15/06/2025`\n"
                f"  • Solo el día: `1`, `15`, `28`"
            )

        datos["fecha_inicio"] = fecha.isoformat()
        conv["paso"] = 3
        return (
            f"✅ Fecha de inicio: *{fmt_fecha(fecha.isoformat())}*\n\n"
            f"*Paso 2 de 3 — Duración*\n\n"
            f"¿Cuántos días dura el tratamiento?\n\n"
            f"Puedes escribir:\n"
            f"  • Solo el número: `7`, `14`, `30`\n"
            f"  • Con texto: `7 días` · `dos semanas` · `un mes`"
        )

    # ── PASO 3: Duración en días ─────────────────────────
    elif paso == 3:
        dias = parsear_duracion(texto)
        if not dias:
            return (
                f"❌ No pude entender esa duración: `{texto}`\n\n"
                f"Escribe la cantidad de días:\n"
                f"  • `7` · `14` · `30`\n"
                f"  • `7 días` · `2 semanas` · `1 mes`"
            )

        fi   = date.fromisoformat(datos["fecha_inicio"])
        ff   = fi + timedelta(days=dias - 1)
        datos["fecha_fin"] = ff.isoformat()
        datos["dias"]      = dias
        conv["paso"]       = 4

        return (
            f"✅ Duración: *{dias} día{'s' if dias != 1 else ''}*\n\n"
            f"*Paso 3 de 3 — Confirmación*\n\n"
            f"Resumen del tratamiento para *{datos['med']}*:\n\n"
            f"📅 Inicio    : *{fmt_fecha(datos['fecha_inicio'])}*\n"
            f"📅 Fin       : *{fmt_fecha(datos['fecha_fin'])}*\n"
            f"⏳ Duración  : *{dias} día{'s' if dias != 1 else ''}*\n\n"
            f"¿Confirmas esta configuración?\n"
            f"Responde *sí* para guardar o *no* para cancelar."
        )

    # ── PASO 4: Confirmación ─────────────────────────────
    elif paso == 4:
        if texto.lower() in ("sí", "si", "s", "yes", "confirmar", "ok", "vale", "correcto"):
            med_nombre = datos["med"]
            usuarios[chat_id]["meds"][med_nombre]["fecha_inicio"] = datos["fecha_inicio"]
            usuarios[chat_id]["meds"][med_nombre]["fecha_fin"]    = datos["fecha_fin"]
            dias = datos["dias"]
            limpiar_conversacion(chat_id)
            return (
                f"✅ *¡Tratamiento configurado correctamente!*\n\n"
                f"💊 Medicamento : *{med_nombre}*\n"
                f"📅 Inicio      : {fmt_fecha(datos['fecha_inicio'])}\n"
                f"📅 Fin         : {fmt_fecha(datos['fecha_fin'])}\n"
                f"⏳ Duración    : {dias} día{'s' if dias != 1 else ''}\n\n"
                f"_El dispensador solo se activará dentro de este rango._\n\n"
                f"Usa `/listmed` para ver el estado completo."
            )
        elif texto.lower() in ("no", "n", "cancelar", "cancel"):
            limpiar_conversacion(chat_id)
            return (
                "❌ *Configuración cancelada.*\n\n"
                "Las fechas anteriores no fueron modificadas.\n"
                "Puedes intentarlo de nuevo con `/setdate`."
            )
        else:
            return (
                "Por favor responde *sí* para guardar\n"
                "o *no* para cancelar."
            )

    limpiar_conversacion(chat_id)
    return "Ocurrió un error. Intenta de nuevo con `/setdate`."


# ══════════════════════════════════════════════════════════
#  PARSER DE DURACIÓN
#  Acepta: "7", "7 días", "2 semanas", "un mes", etc.
# ══════════════════════════════════════════════════════════
def parsear_duracion(texto: str) -> int | None:
    texto = texto.strip().lower()

    NUMEROS_ES = {
        "uno": 1, "una": 1, "un": 1,
        "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
        "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
        "once": 11, "doce": 12, "quince": 15, "veinte": 20,
        "treinta": 30, "sesenta": 60, "noventa": 90,
    }

    # Número solo: "7"
    m = re.match(r"^(\d+)(?:\s+d[ií]as?)?$", texto)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 365 else None

    # Semanas: "2 semanas", "una semana"
    m = re.match(r"^(\d+|[a-záéíóú]+)\s+semanas?$", texto)
    if m:
        raw = m.group(1)
        n   = int(raw) if raw.isdigit() else NUMEROS_ES.get(raw)
        return n * 7 if n and 1 <= n * 7 <= 365 else None

    # Meses: "1 mes", "dos meses"
    m = re.match(r"^(\d+|[a-záéíóú]+)\s+meses?$", texto)
    if m:
        raw = m.group(1)
        n   = int(raw) if raw.isdigit() else NUMEROS_ES.get(raw)
        return n * 30 if n and 1 <= n * 30 <= 365 else None

    # Texto puro sin número: "una semana", "un mes"
    for palabra, valor in NUMEROS_ES.items():
        if texto == palabra or texto == f"{palabra} día" or texto == f"{palabra} dias":
            return valor if 1 <= valor <= 365 else None

    return None


# ══════════════════════════════════════════════════════════
#  WEBHOOK TELEGRAM
# ══════════════════════════════════════════════════════════
@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()

    if "message" not in data:
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]
    text    = data["message"].get("text", "").strip()
    init_user(chat_id)

    meds    = usuarios[chat_id]["meds"]
    profile = usuarios[chat_id]["profile"]
    conv    = conversacion[chat_id]
    partes  = text.split()
    cmd     = partes[0].lower() if partes else ""

    # ══════════════════════════════════════════════════════
    #  INTERCEPCIÓN: si hay un flujo activo, redirigir
    # ══════════════════════════════════════════════════════
    if conv["flujo"] == "setdate" and not cmd.startswith("/"):
        response = await flujo_setdate(chat_id, text)
        await bot.send_message(chat_id=chat_id, text=response, parse_mode="Markdown")
        return {"ok": True}

    # También interceptar si escribe un comando dentro del flujo
    # (excepto /cancelar que lo maneja el flujo mismo)
    if conv["flujo"] == "setdate" and cmd not in ("/cancelar", "/setdate"):
        response = await flujo_setdate(chat_id, text)
        await bot.send_message(chat_id=chat_id, text=response, parse_mode="Markdown")
        return {"ok": True}

    response = "❓ Comando no reconocido. Usa /help para ver los comandos."

    # ── /start ──────────────────────────────────────────
    if cmd == "/start":
        nombre = profile.get("nombre", "")
        saludo = f"Hola, *{nombre}*. " if nombre else ""
        response = (
            f"💊 *Bot Medic*\n"
            f"_Dispensador inteligente de medicamentos_\n\n"
            f"{saludo}Guía rápida para comenzar:\n\n"
            f"*1️⃣* `/profile Nombre Edad` — Configura tu perfil\n"
            f"*2️⃣* `/addmed Paracetamol` — Agrega un medicamento\n"
            f"*3️⃣* `/settime Paracetamol 08:00 14:00` — Configura horarios\n"
            f"*4️⃣* `/setdate` — Configura la duración del tratamiento\n\n"
            f"Escribe /help para ver todos los comandos."
        )

    # ── /help ────────────────────────────────────────────
    elif cmd == "/help":
        response = (
            "📋 *Comandos disponibles — Bot Medic*\n\n"
            "─────────────────────────\n"
            "👤 *PERFIL*\n"
            "`/profile` — Ver o configurar tu perfil\n\n"
            "─────────────────────────\n"
            "💊 *MEDICAMENTOS*\n"
            "`/addmed <nombre>` — Agregar medicamento\n"
            "`/listmed` — Ver todos tus medicamentos\n"
            "`/deletemed <nombre>` — Eliminar medicamento\n\n"
            "─────────────────────────\n"
            "⏰ *HORARIOS*\n"
            "`/settime <nombre> <HH:MM> ...` — Configurar horarios\n\n"
            "─────────────────────────\n"
            "📅 *FECHAS DE TRATAMIENTO*\n"
            "`/setdate` — Asistente de configuración de fechas\n\n"
            "─────────────────────────\n"
            "🔔 *RECORDATORIOS*\n"
            "`/reminders` — Ver recordatorios activos\n"
            "`/pause <nombre>` — Pausar medicamento\n"
            "`/resume <nombre>` — Reactivar medicamento\n\n"
            "─────────────────────────\n"
            "📊 *SISTEMA*\n"
            "`/status` — Resumen general\n"
            "`/test` — Probar el dispensador\n\n"
            "💡 _Escribe cualquier comando solo para ver\n"
            "instrucciones detalladas de cómo usarlo._"
        )

    # ── /profile ─────────────────────────────────────────
    elif cmd == "/profile":
        if len(partes) < 3:
            if profile:
                response = (
                    f"👤 *Tu perfil*\n\n"
                    f"Nombre : {profile.get('nombre', '—')}\n"
                    f"Edad   : {profile.get('edad', '—')} años\n"
                    f"Notas  : {profile.get('notas', '—')}\n\n"
                    f"_Para actualizar: `/profile Nombre Edad Notas`_"
                )
            else:
                response = AYUDA["/profile"]
        else:
            nombre = partes[1]
            try:
                edad = int(partes[2])
            except ValueError:
                response = "❌ La edad debe ser un número. Ej: `/profile Juan 30`"
                await bot.send_message(chat_id=chat_id, text=response, parse_mode="Markdown")
                return {"ok": True}
            notas = " ".join(partes[3:]) if len(partes) > 3 else "—"
            profile["nombre"] = nombre
            profile["edad"]   = edad
            profile["notas"]  = notas
            response = (
                f"✅ *Perfil guardado*\n\n"
                f"👤 Nombre : {nombre}\n"
                f"🎂 Edad   : {edad} años\n"
                f"📝 Notas  : {notas}"
            )

    # ── /addmed ──────────────────────────────────────────
    elif cmd == "/addmed":
        if len(partes) < 2:
            response = AYUDA["/addmed"]
        else:
            nombre = partes[1]
            if nombre in meds:
                response = f"⚠️ *{nombre}* ya está en tu lista.\n\nUsa `/listmed` para verlo."
            else:
                meds[nombre] = {"horarios": [], "fecha_inicio": None, "fecha_fin": None, "pausado": False}
                response = (
                    f"✅ Medicamento agregado: *{nombre}*\n\n"
                    f"Configura sus horarios:\n`/settime {nombre} 08:00 14:00 21:00`\n\n"
                    f"_(Opcional)_ Configura la duración:\n`/setdate`"
                )

    # ── /listmed ─────────────────────────────────────────
    elif cmd == "/listmed":
        if not meds:
            response = "📭 No tienes medicamentos.\n\nAgrega uno con `/addmed nombre`."
        else:
            response = f"💊 *Tus medicamentos ({len(meds)}):*\n\n"
            for nombre, med in meds.items():
                horas = " · ".join(f"`{h}`" for h in med["horarios"]) if med["horarios"] else "_sin horarios_"
                fi    = fmt_fecha(med.get("fecha_inicio"))
                ff    = fmt_fecha(med.get("fecha_fin"))
                est   = estado_med(med)
                response += f"*{nombre}*  —  {est}\n  ⏰ {horas}\n  📅 {fi} → {ff}\n\n"

    # ── /deletemed ───────────────────────────────────────
    elif cmd == "/deletemed":
        if len(partes) < 2:
            response = AYUDA["/deletemed"]
        else:
            nombre = partes[1]
            if nombre not in meds:
                response = f"❌ No existe *{nombre}*.\n\nUsa `/listmed` para ver tu lista."
            else:
                del meds[nombre]
                registrar_todos_los_horarios()
                response = f"🗑️ *{nombre}* eliminado correctamente.\n\nUsa `/listmed` para ver tu lista actualizada."

    # ── /settime ─────────────────────────────────────────
    elif cmd == "/settime":
        if len(partes) < 3:
            response = AYUDA["/settime"]
        else:
            nombre = partes[1]
            horas  = partes[2:]
            if nombre not in meds:
                response = f"❌ *{nombre}* no existe.\n\nAgrégalo con `/addmed {nombre}`."
            else:
                invalidas = [h for h in horas if not _hora_valida(h)]
                if invalidas:
                    response = (
                        f"❌ Horas inválidas: `{'`, `'.join(invalidas)}`\n\n"
                        f"Usa formato 24h. Ej: `08:00`, `13:30`, `21:00`"
                    )
                else:
                    meds[nombre]["horarios"] = sorted(list(set(horas)))
                    registrar_todos_los_horarios()
                    lista = "\n".join(f"  • `{h}`" for h in meds[nombre]["horarios"])
                    response = (
                        f"✅ *Horarios configurados para {nombre}:*\n\n"
                        f"{lista}\n\n"
                        f"_El dispensador se activará automáticamente._"
                    )

    # ── /setdate — inicia el flujo conversacional ────────
    elif cmd == "/setdate":
        if not meds:
            response = (
                "📭 No tienes medicamentos registrados.\n\n"
                "Agrega uno primero con `/addmed nombre`."
            )
        else:
            # Si viene con argumento: /setdate Paracetamol
            nombre_directo = partes[1] if len(partes) > 1 else None

            conversacion[chat_id] = {
                "flujo": "setdate",
                "paso":  1,
                "datos": {}
            }

            if nombre_directo and nombre_directo in meds:
                # Saltar directo al paso 2
                conversacion[chat_id]["datos"]["med"] = nombre_directo
                conversacion[chat_id]["paso"] = 2
                response = (
                    f"📅 *Configurando tratamiento para: {nombre_directo}*\n\n"
                    f"*Paso 1 de 3 — Fecha de inicio*\n\n"
                    f"¿Qué día comienza el tratamiento?\n\n"
                    f"Puedes escribirlo como:\n"
                    f"  • `hoy` o `mañana`\n"
                    f"  • `1 mayo` · `15 de junio` · `3 julio 2025`\n"
                    f"  • `01/06` · `15/06/2025`\n"
                    f"  • Solo el número del día: `1`, `15`, `28`\n\n"
                    f"_Escribe *cancelar* en cualquier momento para salir._"
                )
            else:
                # Mostrar lista de medicamentos para elegir
                lista = "\n".join(f"  • {n}" for n in meds.keys())
                response = (
                    f"📅 *Asistente de configuración de fechas*\n\n"
                    f"*Paso 0 — Selección de medicamento*\n\n"
                    f"¿Para qué medicamento deseas configurar las fechas?\n\n"
                    f"Tus medicamentos:\n{lista}\n\n"
                    f"Escribe el nombre del medicamento."
                )

    # ── /reminders ───────────────────────────────────────
    elif cmd == "/reminders":
        activos = {n: m for n, m in meds.items() if not m["pausado"] and tratamiento_activo(m) and m["horarios"]}
        if not activos:
            response = (
                "📭 *No tienes recordatorios activos.*\n\n"
                "Posibles causas:\n"
                "  • No hay medicamentos registrados\n"
                "  • Todos están pausados o sin horarios\n"
                "  • Algún tratamiento ya finalizó\n\n"
                "Usa `/listmed` para revisar el estado."
            )
        else:
            response = f"🔔 *Recordatorios activos ({len(activos)}):*\n\n"
            for nombre, med in activos.items():
                horas_str = "  ·  ".join(f"`{h}`" for h in med["horarios"])
                fi        = fmt_fecha(med.get("fecha_inicio"))
                ff        = fmt_fecha(med.get("fecha_fin"))
                response += f"💊 *{nombre}*\n  ⏰ {horas_str}\n  📅 {fi} → {ff}\n\n"

    # ── /pause ───────────────────────────────────────────
    elif cmd == "/pause":
        if len(partes) < 2:
            response = AYUDA["/pause"]
        else:
            nombre = partes[1]
            if nombre not in meds:
                response = f"❌ No existe *{nombre}*.\n\nUsa `/listmed` para ver tu lista."
            elif meds[nombre]["pausado"]:
                response = f"⚠️ *{nombre}* ya está pausado.\n\nPara reactivarlo: `/resume {nombre}`"
            else:
                meds[nombre]["pausado"] = True
                registrar_todos_los_horarios()
                response = f"⏸ *{nombre}* pausado.\n\nPara reactivarlo: `/resume {nombre}`"

    # ── /resume ──────────────────────────────────────────
    elif cmd == "/resume":
        if len(partes) < 2:
            response = AYUDA["/resume"]
        else:
            nombre = partes[1]
            if nombre not in meds:
                response = f"❌ No existe *{nombre}*.\n\nUsa `/listmed` para ver tu lista."
            elif not meds[nombre]["pausado"]:
                response = f"⚠️ *{nombre}* ya está activo.\n\nPara pausarlo: `/pause {nombre}`"
            else:
                meds[nombre]["pausado"] = False
                registrar_todos_los_horarios()
                response = f"▶️ *{nombre}* reactivado.\n\nLos recordatorios volverán a funcionar."

    # ── /status ──────────────────────────────────────────
    elif cmd == "/status":
        total      = len(meds)
        activos    = sum(1 for m in meds.values() if not m["pausado"] and tratamiento_activo(m) and m["horarios"])
        pausados   = sum(1 for m in meds.values() if m["pausado"])
        sin_horas  = sum(1 for m in meds.values() if not m["horarios"] and not m["pausado"])
        terminados = sum(1 for m in meds.values() if not tratamiento_activo(m) and not m["pausado"])
        nombre_p   = profile.get("nombre", "—")
        edad_p     = profile.get("edad", "—")
        hora_actual  = datetime.now().strftime("%H:%M")
        fecha_actual = datetime.now().strftime("%d/%m/%Y")
        response = (
            f"📊 *Estado del sistema — Bot Medic*\n\n"
            f"👤 *Paciente:* {nombre_p}"
            + (f" ({edad_p} años)" if edad_p != "—" else "") + "\n"
            f"🗓 *Fecha:* {fecha_actual}  🕐 *Hora:* {hora_actual}\n\n"
            f"─────────────────────────\n"
            f"💊 *Medicamentos:* {total}\n"
            f"  ✅ Activos con horario  : {activos}\n"
            f"  ⏸ Pausados             : {pausados}\n"
            f"  ⚠️ Sin horarios         : {sin_horas}\n"
            f"  ⛔ Tratamiento terminado: {terminados}\n\n"
            f"─────────────────────────\n"
            f"📡 *ESP32:* {len(senales_pendientes)} señal(es) pendiente(s)\n\n"
            f"_Usa /reminders para ver los recordatorios activos._"
        )

    # ── /test ────────────────────────────────────────────
    elif cmd == "/test":
        senales_pendientes.append({
            "medicamento": "PRUEBA",
            "horario":     datetime.now().strftime("%H:%M"),
            "timestamp":   datetime.now().isoformat(),
            "chat_id":     chat_id
        })
        response = (
            "🧪 *Señal de prueba enviada al dispensador*\n\n"
            f"🕐 Hora de envío: {datetime.now().strftime('%H:%M:%S')}\n\n"
            "El ESP32 recibirá la señal en su próxima consulta\n"
            "(máximo 10 segundos) y el servo se moverá brevemente.\n\n"
            "_Si el servo no responde, revisa la conexión WiFi\n"
            "del ESP32 y que la URL de Render sea correcta._"
        )

    # ── Comando desconocido ───────────────────────────────
    else:
        if cmd in AYUDA:
            response = AYUDA[cmd]
        elif cmd.startswith("/"):
            response = (
                f"❓ *Comando no reconocido:* `{cmd}`\n\n"
                f"Escribe /help para ver la lista completa de comandos."
            )
        else:
            response = (
                "💬 No entiendo ese mensaje.\n\n"
                "Usa los comandos del menú o escribe /help."
            )

    await bot.send_message(chat_id=chat_id, text=response, parse_mode="Markdown")
    return {"ok": True}
       
