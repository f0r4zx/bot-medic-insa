from fastapi import FastAPI, Request
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from contextlib import asynccontextmanager
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, date, timedelta
import os
import re

# ══════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════════════════════
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")

bot = Bot(token=TOKEN)

# 🔹 CONEXIÓN A MONGODB ATLAS
cliente_mongo = AsyncIOMotorClient(MONGO_URL)
db = cliente_mongo.bot_medic
coleccion_datos = db.memoria

# ══════════════════════════════════════════════════════════
#  BASE DE DATOS EN MEMORIA (Se sincroniza con Mongo)
# ══════════════════════════════════════════════════════════
usuarios: dict = {}
senales_pendientes: list = []
conversacion: dict = {} # Transitorio

# 🔹 PERSISTENCIA: Funciones asíncronas para MongoDB
async def cargar_datos():
    doc = await coleccion_datos.find_one({"_id": "memoria_global"})
    if doc:
        return doc.get("usuarios", {})
    return {}

async def guardar_datos(data: dict):
    await coleccion_datos.update_one(
        {"_id": "memoria_global"},
        {"$set": {"usuarios": data}},
        upsert=True
    )

# ══════════════════════════════════════════════════════════
#  MESES Y AYUDA
# ══════════════════════════════════════════════════════════
MESES = {
    "enero": 1,   "ene": 1,   "jan": 1, "febrero": 2, "feb": 2,
    "marzo": 3,   "mar": 3,   "abril": 4,   "abr": 4,   "apr": 4,
    "mayo": 5,    "may": 5,   "junio": 6,   "jun": 6,
    "julio": 7,   "jul": 7,   "agosto": 8,  "ago": 8,   "aug": 8,
    "septiembre": 9, "sep": 9, "sept": 9, "octubre": 10,   "oct": 10,
    "noviembre": 11, "nov": 11, "diciembre": 12, "dic": 12, "dec": 12,
}

NOMBRE_MES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
    5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
    9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
}

AYUDA = {
    "/addmed": "➕ *Agregar medicamento*\n\n`/addmed <nombre>`\n\nEj: `/addmed Paracetamol`",
    "/deletemed": "🗑️ *Eliminar medicamento*\n\n`/deletemed <nombre>`",
    "/settime": "⏰ *Configurar horarios*\n\n`/settime <nombre> <HH:MM> [HH:MM] ...`\n\nEj: `/settime Paracetamol 08:00 14:00`",
    "/setdate": "📅 *Configurar duración*\n\nSolo escribe `/setdate` y sigue las instrucciones.",
    "/pause": "⏸ *Pausar*\n\n`/pause <nombre>`",
    "/resume": "▶️ *Reactivar*\n\n`/resume <nombre>`",
    "/profile": "👤 *Perfil*\n\n`/profile <Nombre> <Edad> <Notas>`"
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
    # Asegurar que chat_id sea entero o string según cómo se guardó
    chat_id_str = str(chat_id)
    med = usuarios.get(chat_id_str, {}).get("meds", {}).get(med_nombre)
    if not med:
        return
    hoy = date.today()
    fi  = med.get("fecha_inicio")
    ff  = med.get("fecha_fin")
    if fi and hoy < date.fromisoformat(fi):
        return
    if ff and hoy > date.fromisoformat(ff):
        return

    nombre_p = usuarios[chat_id_str].get("profile", {}).get("nombre", "")
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
    global usuarios
    usuarios_db = await cargar_datos()
    usuarios.update(usuarios_db)
    
    scheduler.start()
    registrar_todos_los_horarios()
    print("[BOT MEDIC] Scheduler iniciado. Datos cargados desde MongoDB Atlas.")
    
    yield
    
    scheduler.shutdown()
    await guardar_datos(usuarios) 

app = FastAPI(lifespan=lifespan)

# ══════════════════════════════════════════════════════════
#  ENDPOINT ESP32
# ══════════════════════════════════════════════════════════
@app.get("/")
async def home():
    return {"status": "Bot Medic funcionando ✅ Conectado a MongoDB"}

@app.get("/esp32/signal")
async def esp32_signal():
    if senales_pendientes:
        return {"dispensar": True, "datos": senales_pendientes.pop(0)}
    return {"dispensar": False}

# ══════════════════════════════════════════════════════════
#  HELPERS GENERALES
# ══════════════════════════════════════════════════════════
async def init_user(chat_id):
    chat_id_str = str(chat_id) # JSON/Mongo guarda las llaves numéricas como strings
    if chat_id_str not in usuarios:
        usuarios[chat_id_str] = {"profile": {}, "meds": {}}
        await guardar_datos(usuarios) 
    if chat_id_str not in conversacion:
        conversacion[chat_id_str] = {"flujo": None, "paso": 0, "datos": {}}
    return chat_id_str

def limpiar_conversacion(chat_id_str):
    conversacion[chat_id_str] = {"flujo": None, "paso": 0, "datos": {}}

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

def parsear_fecha(texto: str) -> date | None:
    texto = texto.strip().lower()
    hoy   = date.today()
    if texto in ("hoy", "today"): return hoy
    if texto in ("mañana", "manana", "tomorrow"): return hoy + timedelta(days=1)
    if texto in ("pasado mañana", "pasado manana"): return hoy + timedelta(days=2)

    m = re.match(r"^(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?$", texto)
    if m:
        dia, mes = int(m.group(1)), int(m.group(2))
        anio = int(m.group(3)) if m.group(3) else hoy.year
        if anio < 100: anio += 2000
        try: return date(anio, mes, dia)
        except ValueError: return None

    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", texto)
    if m:
        try: return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError: return None

    m = re.match(r"^(\d{1,2})\s+(?:de\s+)?([a-záéíóúü]+)(?:\s+(?:de\s+)?(\d{2,4}))?$", texto)
    if m:
        dia, mes_txt, anio_txt = int(m.group(1)), m.group(2), m.group(3)
        mes = MESES.get(mes_txt)
        if not mes: return None
        anio = int(anio_txt) if anio_txt else hoy.year
        if anio < 100: anio += 2000
        try:
            d = date(anio, mes, dia)
            if d < hoy and not anio_txt: d = date(anio + 1, mes, dia)
            return d
        except ValueError: return None

    m = re.match(r"^(\d{1,2})$", texto)
    if m:
        dia = int(m.group(1))
        try:
            d = date(hoy.year, hoy.month, dia)
            if d < hoy:
                d = date(hoy.year + 1, 1, dia) if hoy.month == 12 else date(hoy.year, hoy.month + 1, dia)
            return d
        except ValueError: return None
    return None

def parsear_duracion(texto: str) -> int | None:
    texto = texto.strip().lower()
    NUMEROS_ES = {"uno": 1, "una": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "quince": 15, "veinte": 20, "treinta": 30}
    m = re.match(r"^(\d+)(?:\s+d[ií]as?)?$", texto)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 365 else None
    m = re.match(r"^(\d+|[a-záéíóú]+)\s+semanas?$", texto)
    if m:
        raw = m.group(1)
        n = int(raw) if raw.isdigit() else NUMEROS_ES.get(raw)
        return n * 7 if n and 1 <= n * 7 <= 365 else None
    m = re.match(r"^(\d+|[a-záéíóú]+)\s+meses?$", texto)
    if m:
        raw = m.group(1)
        n = int(raw) if raw.isdigit() else NUMEROS_ES.get(raw)
        return n * 30 if n and 1 <= n * 30 <= 365 else None
    for palabra, valor in NUMEROS_ES.items():
        if texto == palabra or texto == f"{palabra} día" or texto == f"{palabra} dias":
            return valor if 1 <= valor <= 365 else None
    return None

async def flujo_setdate(chat_id_str: str, texto: str) -> str:
    conv  = conversacion[chat_id_str]
    datos = conv["datos"]
    meds  = usuarios[chat_id_str]["meds"]
    paso  = conv["paso"]

    if texto.lower() in ("/cancelar", "cancelar", "cancel", "salir"):
        limpiar_conversacion(chat_id_str)
        return "❌ *Configuración de fechas cancelada.*"

    if paso == 1:
        if not meds:
            limpiar_conversacion(chat_id_str)
            return "📭 No tienes medicamentos registrados.\n\nAgrega uno primero con `/addmed nombre`."
        nombre = texto.strip()
        if nombre not in meds:
            lista = "\n".join(f"  • {n}" for n in meds.keys())
            return f"❓ No encontré *{nombre}*\n\nTus medicamentos:\n{lista}"
        datos["med"]  = nombre
        conv["paso"]  = 2
        return (f"📅 *Configurando tratamiento para: {nombre}*\n\n*Paso 1 de 3 — Fecha de inicio*\n\n"
                f"¿Qué día comienza el tratamiento?\n\n"
                f"Puedes escribir: `hoy`, `mañana`, `1 mayo`, `01/06`, `15`\n\n"
                f"_Escribe *cancelar* para salir._")

    elif paso == 2:
        fecha = parsear_fecha(texto)
        if not fecha:
            return f"❌ No entendí esa fecha: `{texto}`\n\nIntenta: `hoy`, `1 mayo`, `01/06`"
        datos["fecha_inicio"] = fecha.isoformat()
        conv["paso"] = 3
        return (f"✅ Fecha de inicio: *{fmt_fecha(fecha.isoformat())}*\n\n"
                f"*Paso 2 de 3 — Duración*\n\n"
                f"¿Cuántos días dura el tratamiento?\n"
                f"Ej: `7`, `14 días`, `2 semanas`, `1 mes`")

    elif paso == 3:
        dias = parsear_duracion(texto)
        if not dias:
            return f"❌ No entendí esa duración: `{texto}`\n\nEj: `7`, `14 días`, `2 semanas`"
        fi   = date.fromisoformat(datos["fecha_inicio"])
        ff   = fi + timedelta(days=dias - 1)
        datos["fecha_fin"] = ff.isoformat()
        datos["dias"]      = dias
        conv["paso"]       = 4
        return (f"✅ Duración: *{dias} día{'s' if dias != 1 else ''}*\n\n"
                f"*Paso 3 de 3 — Confirmación*\n\n"
                f"Resumen para *{datos['med']}*:\n"
                f"📅 Inicio: *{fmt_fecha(datos['fecha_inicio'])}*\n"
                f"📅 Fin   : *{fmt_fecha(datos['fecha_fin'])}*\n\n"
                f"¿Confirmas? Responde *sí* o *no*")

    elif paso == 4:
        if texto.lower() in ("sí", "si", "s", "yes", "confirmar", "ok", "vale", "correcto"):
            med_nombre = datos["med"]
            usuarios[chat_id_str]["meds"][med_nombre]["fecha_inicio"] = datos["fecha_inicio"]
            usuarios[chat_id_str]["meds"][med_nombre]["fecha_fin"]    = datos["fecha_fin"]
            await guardar_datos(usuarios) 
            limpiar_conversacion(chat_id_str)
            return (f"✅ *¡Tratamiento configurado!*\n\n"
                    f"💊 {med_nombre}\n📅 {fmt_fecha(datos['fecha_inicio'])} → {fmt_fecha(datos['fecha_fin'])}")
        elif texto.lower() in ("no", "n", "cancelar", "cancel"):
            limpiar_conversacion(chat_id_str)
            return "❌ *Configuración cancelada.*"
        else:
            return "Por favor responde *sí* o *no*."

    limpiar_conversacion(chat_id_str)
    return "Ocurrió un error. Intenta de nuevo con `/setdate`."

# ══════════════════════════════════════════════════════════
#  WEBHOOK TELEGRAM
# ══════════════════════════════════════════════════════════
@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    if "message" not in data:
        return {"ok": True}

    chat_id_raw = data["message"]["chat"]["id"]
    text    = data["message"].get("text", "").strip()
    
    # Inicializar o recuperar usuario y guardar en variable de tipo string
    chat_id = await init_user(chat_id_raw)

    meds    = usuarios[chat_id]["meds"]
    profile = usuarios[chat_id]["profile"]
    conv    = conversacion[chat_id]
    partes  = text.split()
    cmd     = partes[0].lower() if partes else ""

    if conv["flujo"] == "setdate" and not cmd.startswith("/"):
        response = await flujo_setdate(chat_id, text)
        await bot.send_message(chat_id=chat_id_raw, text=response, parse_mode="Markdown")
        return {"ok": True}
    if conv["flujo"] == "setdate" and cmd not in ("/cancelar", "/setdate"):
        response = await flujo_setdate(chat_id, text)
        await bot.send_message(chat_id=chat_id_raw, text=response, parse_mode="Markdown")
        return {"ok": True}

    response = "❓ Comando no reconocido. Usa /help para ver los comandos."

    if cmd == "/start":
        nombre = profile.get("nombre", "")
        saludo = f"Hola, *{nombre}*. " if nombre else ""
        response = (f"💊 *Bot Medic*\n{saludo}Guía rápida:\n\n"
                    f"1️⃣ `/profile Nombre Edad`\n2️⃣ `/addmed Paracetamol`\n"
                    f"3️⃣ `/settime Paracetamol 08:00`\n4️⃣ `/setdate`\n\n"
                    f"Escribe /help para ver todos los comandos.")

    elif cmd == "/help":
        response = ("📋 *Comandos*\n\n"
                    "👤 `/profile` | 💊 `/addmed` `/listmed` `/deletemed`\n"
                    "⏰ `/settime` | 📅 `/setdate`\n"
                    "🔔 `/reminders` `/pause` `/resume`\n"
                    "📊 `/status` | 🧪 `/test`")

    elif cmd == "/profile":
        if len(partes) < 3:
            if profile:
                response = (f"👤 *Tu perfil*\n"
                            f"Nombre: {profile.get('nombre', '—')}\n"
                            f"Edad  : {profile.get('edad', '—')}\n"
                            f"Notas : {profile.get('notas', '—')}")
            else:
                response = AYUDA["/profile"]
        else:
            nombre = partes[1]
            try: edad = int(partes[2])
            except ValueError:
                response = "❌ La edad debe ser un número."
                await bot.send_message(chat_id=chat_id_raw, text=response, parse_mode="Markdown")
                return {"ok": True}
            notas = " ".join(partes[3:]) if len(partes) > 3 else "—"
            profile.update({"nombre": nombre, "edad": edad, "notas": notas})
            await guardar_datos(usuarios) 
            response = f"✅ *Perfil guardado*\n👤 {nombre} ({edad} años)"

    elif cmd == "/addmed":
        if len(partes) < 2:
            response = AYUDA["/addmed"]
        else:
            nombre = partes[1]
            if nombre in meds:
                response = f"⚠️ *{nombre}* ya existe."
            else:
                meds[nombre] = {"horarios": [], "fecha_inicio": None, "fecha_fin": None, "pausado": False}
                await guardar_datos(usuarios) 
                response = f"✅ Agregado: *{nombre}*\nConfigura horarios: `/settime {nombre} 08:00`"

    elif cmd == "/listmed":
        if not meds:
            response = "📭 No hay medicamentos."
        else:
            response = f"💊 *Tus medicamentos ({len(meds)}):*\n\n"
            for n, m in meds.items():
                horas = " · ".join(f"`{h}`" for h in m["horarios"]) or "_sin horarios_"
                response += f"*{n}* — {estado_med(m)}\n  ⏰ {horas}\n  📅 {fmt_fecha(m['fecha_inicio'])} → {fmt_fecha(m['fecha_fin'])}\n\n"

    elif cmd == "/deletemed":
        if len(partes) < 2:
            response = AYUDA["/deletemed"]
        else:
            nombre = partes[1]
            if nombre not in meds:
                response = f"❌ No existe *{nombre}*."
            else:
                del meds[nombre]
                await guardar_datos(usuarios) 
                registrar_todos_los_horarios()
                response = f"🗑️ *{nombre}* eliminado."

    elif cmd == "/settime":
        if len(partes) < 3:
            response = AYUDA["/settime"]
        else:
            nombre, horas = partes[1], partes[2:]
            if nombre not in meds:
                response = f"❌ *{nombre}* no existe. Agrégalo con `/addmed {nombre}`"
            else:
                invalidas = [h for h in horas if not _hora_valida(h)]
                if invalidas:
                    response = f"❌ Horas inválidas: `{'`, `'.join(invalidas)}`"
                else:
                    meds[nombre]["horarios"] = sorted(list(set(horas)))
                    await guardar_datos(usuarios) 
                    registrar_todos_los_horarios()
                    response = f"✅ Horarios para *{nombre}*:\n" + "\n".join(f"  • `{h}`" for h in meds[nombre]["horarios"])

    elif cmd == "/setdate":
        if not meds:
            response = "📭 No hay medicamentos. Usa `/addmed` primero."
        else:
            nombre_directo = partes[1] if len(partes) > 1 else None
            conversacion[chat_id] = {"flujo": "setdate", "paso": 1, "datos": {}}
            if nombre_directo and nombre_directo in meds:
                conversacion[chat_id]["datos"]["med"] = nombre_directo
                conversacion[chat_id]["paso"] = 2
                response = (f"📅 *Configurando: {nombre_directo}*\n\n"
                            f"*Paso 1/3 — Fecha inicio*\n"
                            f"Escribe: `hoy`, `mañana`, `1 mayo`, `01/06`, `15`")
            else:
                lista = "\n".join(f"  • {n}" for n in meds.keys())
                response = f"📅 *Asistente de fechas*\n\n¿Para qué medicamento?\n{lista}"

    elif cmd == "/reminders":
        activos = {n: m for n, m in meds.items() if not m["pausado"] and tratamiento_activo(m) and m["horarios"]}
        if not activos:
            response = "📭 *Sin recordatorios activos.*\nRevisa con `/listmed`"
        else:
            response = f"🔔 *Recordatorios ({len(activos)}):*\n\n"
            for n, m in activos.items():
                response += f"💊 *{n}*\n  ⏰ {' · '.join(f'`{h}`' for h in m['horarios'])}\n"

    elif cmd == "/pause":
        if len(partes) < 2: response = AYUDA["/pause"]
        else:
            nombre = partes[1]
            if nombre not in meds: response = f"❌ *{nombre}* no existe."
            elif meds[nombre]["pausado"]: response = f"⚠️ *{nombre}* ya está pausado."
            else:
                meds[nombre]["pausado"] = True
                await guardar_datos(usuarios) 
                registrar_todos_los_horarios()
                response = f"⏸ *{nombre}* pausado."

    elif cmd == "/resume":
        if len(partes) < 2: response = AYUDA["/resume"]
        else:
            nombre = partes[1]
            if nombre not in meds: response = f"❌ *{nombre}* no existe."
            elif not meds[nombre]["pausado"]: response = f"⚠️ *{nombre}* ya está activo."
            else:
                meds[nombre]["pausado"] = False
                await guardar_datos(usuarios) 
                registrar_todos_los_horarios()
                response = f"▶️ *{nombre}* reactivado."

    elif cmd == "/status":
        total = len(meds)
        activos = sum(1 for m in meds.values() if not m["pausado"] and tratamiento_activo(m) and m["horarios"])
        response = (f"📊 *Estado del sistema*\n"
                    f"💊 Medicamentos: {total} | ✅ Activos: {activos}\n"
                    f"📡 Señales pendientes: {len(senales_pendientes)}\n"
                    f"_Usa /reminders o /listmed para detalles._")

    elif cmd == "/test":
        senales_pendientes.append({
            "medicamento": "PRUEBA", "horario": datetime.now().strftime("%H:%M"),
            "timestamp": datetime.now().isoformat(), "chat_id": chat_id_raw
        })
        response = "🧪 *Señal de prueba enviada.*\nEl ESP32 la recibirá en su próxima consulta."

    else:
        if cmd in AYUDA: response = AYUDA[cmd]
        elif cmd.startswith("/"): response = f"❓ Comando `{cmd}` no reconocido."
        else: response = "💬 Usa /help para ver comandos."

    await bot.send_message(chat_id=chat_id_raw, text=response, parse_mode="Markdown")
    return {"ok": True}
