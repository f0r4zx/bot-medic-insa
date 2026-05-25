from fastapi import FastAPI, Request
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from contextlib import asynccontextmanager
from datetime import datetime, date
import os

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=TOKEN)

# ─────────────────────────────────────────────
#  BASE DE DATOS EN MEMORIA
#
#  usuarios[chat_id] = {
#    "profile": {
#      "nombre": "Juan",
#      "edad": 30,
#      "notas": "Diabético"
#    },
#    "meds": {
#      "Paracetamol": {
#        "horarios": ["08:00", "14:00", "21:00"],
#        "fecha_inicio": "2025-01-01",   # o None
#        "fecha_fin":    "2025-01-07",   # o None
#        "pausado": False
#      }
#    }
#  }
# ─────────────────────────────────────────────
usuarios: dict = {}

# Cola de señales para el ESP32
senales_pendientes: list = []

# ─────────────────────────────────────────────
#  SCHEDULER
# ─────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="America/El_Salvador")


def registrar_todos_los_horarios():
    scheduler.remove_all_jobs()
    for chat_id, data in usuarios.items():
        for med_nombre, med in data.get("meds", {}).items():
            if med.get("pausado", False):
                continue
            for horario in med.get("horarios", []):
                hora, minuto = horario.split(":")
                job_id = f"{chat_id}_{med_nombre}_{horario}"
                scheduler.add_job(
                    disparar_medicamento,
                    CronTrigger(hour=int(hora), minute=int(minuto)),
                    id=job_id,
                    replace_existing=True,
                    args=[chat_id, med_nombre, horario]
                )


async def disparar_medicamento(chat_id: int, med_nombre: str, horario: str):
    med = usuarios.get(chat_id, {}).get("meds", {}).get(med_nombre)
    if not med:
        return

    # Verificar si el tratamiento sigue activo por fechas
    hoy = date.today()
    fecha_inicio = med.get("fecha_inicio")
    fecha_fin    = med.get("fecha_fin")

    if fecha_inicio and hoy < date.fromisoformat(fecha_inicio):
        return  # Aún no empieza
    if fecha_fin and hoy > date.fromisoformat(fecha_fin):
        return  # Ya terminó

    nombre_paciente = usuarios[chat_id].get("profile", {}).get("nombre", "")
    saludo = f"*{nombre_paciente}*, " if nombre_paciente else ""

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"⏰ {saludo}¡Hora de tu medicamento!\n\n"
            f"💊 *{med_nombre}*\n"
            f"🕐 {horario}\n\n"
            f"El dispensador se activará en unos segundos..."
        ),
        parse_mode="Markdown"
    )

    senales_pendientes.append({
        "medicamento": med_nombre,
        "horario": horario,
        "timestamp": datetime.now().isoformat(),
        "chat_id": chat_id
    })


# ─────────────────────────────────────────────
#  LIFESPAN
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    registrar_todos_los_horarios()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


# ─────────────────────────────────────────────
#  ENDPOINT ESP32
# ─────────────────────────────────────────────
@app.get("/")
async def home():
    return {"status": "Bot Medic funcionando ✅"}


@app.get("/esp32/signal")
async def esp32_signal():
    if senales_pendientes:
        senal = senales_pendientes.pop(0)
        return {"dispensar": True, "datos": senal}
    return {"dispensar": False}


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def init_user(chat_id):
    if chat_id not in usuarios:
        usuarios[chat_id] = {"profile": {}, "meds": {}}


def fmt_fecha(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        d = date.fromisoformat(iso)
        return d.strftime("%d/%m/%Y")
    except Exception:
        return iso


def tratamiento_activo(med: dict) -> bool:
    hoy = date.today()
    fi = med.get("fecha_inicio")
    ff = med.get("fecha_fin")
    if fi and hoy < date.fromisoformat(fi):
        return False
    if ff and hoy > date.fromisoformat(ff):
        return False
    return True


# ─────────────────────────────────────────────
#  WEBHOOK
# ─────────────────────────────────────────────
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
    partes  = text.split()
    cmd     = partes[0].lower() if partes else ""

    response = "❓ Comando no reconocido. Usa /help para ver los comandos."

    # ── /start ──────────────────────────────────────────
    if cmd == "/start":
        response = (
            "💊 *Bienvenido a Bot Medic*\n"
            "Tu dispensador inteligente de medicamentos.\n\n"
            "Configura tu perfil primero:\n"
            "`/profile Nombre 25 Notas opcionales`\n\n"
            "Luego agrega medicamentos:\n"
            "`/addmed Paracetamol`\n\n"
            "Y sus horarios:\n"
            "`/settime Paracetamol 08:00 14:00 21:00`\n\n"
            "Usa /help para ver todos los comandos."
        )

    # ── /help ────────────────────────────────────────────
    elif cmd == "/help":
        response = (
            "📋 *Comandos disponibles:*\n\n"
            "*👤 Perfil:*\n"
            "`/profile Nombre Edad Notas`\n\n"
            "*💊 Medicamentos:*\n"
            "`/addmed nombre`\n"
            "`/listmed`\n"
            "`/deletemed nombre`\n\n"
            "*⏰ Horarios:*\n"
            "`/settime nombre 08:00 14:00 21:00`\n\n"
            "*📅 Fechas de tratamiento:*\n"
            "`/setdate nombre 2025-01-01 2025-01-07`\n\n"
            "*🔔 Recordatorios:*\n"
            "`/reminders` — Ver todos los activos\n"
            "`/pause nombre` — Pausar un medicamento\n"
            "`/resume nombre` — Reactivar un medicamento\n\n"
            "*📊 Estado:*\n"
            "`/status` — Resumen del tratamiento\n\n"
            "*🧪 Prueba:*\n"
            "`/test` — Enviar señal de prueba al dispensador"
        )

    # ── /profile Nombre Edad Notas ───────────────────────
    elif cmd == "/profile":
        if len(partes) < 3:
            # Mostrar perfil actual si existe
            if profile:
                response = (
                    f"👤 *Tu perfil:*\n\n"
                    f"Nombre: {profile.get('nombre', '—')}\n"
                    f"Edad:   {profile.get('edad', '—')}\n"
                    f"Notas:  {profile.get('notas', '—')}\n\n"
                    f"Para actualizar:\n"
                    f"`/profile Nombre Edad Notas médicas`"
                )
            else:
                response = (
                    "❌ Debes indicar al menos nombre y edad.\n\n"
                    "Ejemplo:\n"
                    "`/profile Juan 30 Diabético, alérgico a la penicilina`"
                )
        else:
            nombre = partes[1]
            try:
                edad = int(partes[2])
            except ValueError:
                await bot.send_message(chat_id=chat_id,
                    text="❌ La edad debe ser un número. Ej: `/profile Juan 30 notas`",
                    parse_mode="Markdown")
                return {"ok": True}
            notas = " ".join(partes[3:]) if len(partes) > 3 else "—"
            profile["nombre"] = nombre
            profile["edad"]   = edad
            profile["notas"]  = notas
            response = (
                f"✅ *Perfil guardado*\n\n"
                f"👤 Nombre: {nombre}\n"
                f"🎂 Edad:   {edad}\n"
                f"📝 Notas:  {notas}"
            )

    # ── /addmed nombre ───────────────────────────────────
    elif cmd == "/addmed":
        if len(partes) < 2:
            response = "❌ Ejemplo: `/addmed Paracetamol`"
        else:
            nombre = partes[1]
            if nombre in meds:
                response = f"⚠️ *{nombre}* ya está en tu lista."
            else:
                meds[nombre] = {
                    "horarios": [],
                    "fecha_inicio": None,
                    "fecha_fin": None,
                    "pausado": False
                }
                response = (
                    f"✅ Medicamento agregado: *{nombre}*\n\n"
                    f"Configura sus horarios:\n"
                    f"`/settime {nombre} 08:00 14:00 21:00`"
                )

    # ── /listmed ─────────────────────────────────────────
    elif cmd == "/listmed":
        if not meds:
            response = "📭 No tienes medicamentos.\n\nAgrega uno con `/addmed nombre`"
        else:
            response = "💊 *Tus medicamentos:*\n\n"
            for nombre, med in meds.items():
                estado = "⏸ Pausado" if med["pausado"] else ("✅ Activo" if tratamiento_activo(med) else "⛔ Terminado")
                horas  = ", ".join(med["horarios"]) if med["horarios"] else "sin horarios"
                fi     = fmt_fecha(med.get("fecha_inicio"))
                ff     = fmt_fecha(med.get("fecha_fin"))
                response += (
                    f"*{nombre}* — {estado}\n"
                    f"  ⏰ {horas}\n"
                    f"  📅 {fi} → {ff}\n\n"
                )

    # ── /deletemed nombre ────────────────────────────────
    elif cmd == "/deletemed":
        if len(partes) < 2:
            response = "❌ Ejemplo: `/deletemed Paracetamol`"
        else:
            nombre = partes[1]
            if nombre not in meds:
                response = f"❌ No existe *{nombre}*."
            else:
                del meds[nombre]
                registrar_todos_los_horarios()
                response = f"🗑️ *{nombre}* eliminado junto con sus horarios."

    # ── /settime nombre HH:MM [HH:MM ...] ───────────────
    elif cmd == "/settime":
        if len(partes) < 3:
            response = (
                "❌ Debes indicar medicamento y al menos una hora.\n\n"
                "Ejemplo: `/settime Paracetamol 08:00 14:00 21:00`"
            )
        else:
            nombre  = partes[1]
            horas   = partes[2:]

            if nombre not in meds:
                response = (
                    f"❌ El medicamento *{nombre}* no existe.\n"
                    f"Agrégalo primero con `/addmed {nombre}`"
                )
            else:
                invalidas = []
                validas   = []
                for h in horas:
                    try:
                        datetime.strptime(h, "%H:%M")
                        validas.append(h)
                    except ValueError:
                        invalidas.append(h)

                if invalidas:
                    response = (
                        f"❌ Horas inválidas: {', '.join(invalidas)}\n"
                        f"Usa formato 24h. Ej: `08:00`, `14:30`, `21:00`"
                    )
                else:
                    meds[nombre]["horarios"] = sorted(list(set(validas)))
                    registrar_todos_los_horarios()
                    horas_str = "\n".join(f"  • `{h}`" for h in meds[nombre]["horarios"])
                    response = (
                        f"⏰ *Horarios configurados para {nombre}:*\n\n"
                        f"{horas_str}\n\n"
                        f"El dispensador se activará automáticamente."
                    )

    # ── /setdate nombre YYYY-MM-DD YYYY-MM-DD ───────────
    elif cmd == "/setdate":
        if len(partes) < 4:
            response = (
                "❌ Debes indicar medicamento, fecha inicio y fecha fin.\n\n"
                "Ejemplo: `/setdate Paracetamol 2025-01-01 2025-01-07`"
            )
        else:
            nombre = partes[1]
            f_ini  = partes[2]
            f_fin  = partes[3]

            if nombre not in meds:
                response = f"❌ El medicamento *{nombre}* no existe."
            else:
                try:
                    di = date.fromisoformat(f_ini)
                    df = date.fromisoformat(f_fin)
                    if df < di:
                        raise ValueError("Fin antes que inicio")
                    meds[nombre]["fecha_inicio"] = f_ini
                    meds[nombre]["fecha_fin"]    = f_fin
                    dias = (df - di).days + 1
                    response = (
                        f"📅 *Fechas del tratamiento — {nombre}:*\n\n"
                        f"Inicio: {fmt_fecha(f_ini)}\n"
                        f"Fin:    {fmt_fecha(f_fin)}\n"
                        f"Duración: {dias} días"
                    )
                except ValueError as e:
                    response = (
                        f"❌ Fechas inválidas.\n"
                        f"Usa formato `YYYY-MM-DD`. Ej: `2025-01-01`\n"
                        f"La fecha de fin debe ser mayor que la de inicio."
                    )

    # ── /reminders ───────────────────────────────────────
    elif cmd == "/reminders":
        activos = {n: m for n, m in meds.items() if not m["pausado"] and tratamiento_activo(m) and m["horarios"]}
        if not activos:
            response = "📭 No tienes recordatorios activos.\n\nConfigura medicamentos y horarios primero."
        else:
            response = "🔔 *Recordatorios activos:*\n\n"
            for nombre, med in activos.items():
                horas_str = " | ".join(f"`{h}`" for h in med["horarios"])
                fi = fmt_fecha(med.get("fecha_inicio"))
                ff = fmt_fecha(med.get("fecha_fin"))
                response += f"💊 *{nombre}*\n  ⏰ {horas_str}\n  📅 {fi} → {ff}\n\n"

    # ── /pause nombre ────────────────────────────────────
    elif cmd == "/pause":
        if len(partes) < 2:
            response = "❌ Ejemplo: `/pause Paracetamol`"
        else:
            nombre = partes[1]
            if nombre not in meds:
                response = f"❌ No existe *{nombre}*."
            elif meds[nombre]["pausado"]:
                response = f"⚠️ *{nombre}* ya está pausado."
            else:
                meds[nombre]["pausado"] = True
                registrar_todos_los_horarios()
                response = (
                    f"⏸ *{nombre}* pausado.\n\n"
                    f"El dispensador no se activará hasta que uses:\n"
                    f"`/resume {nombre}`"
                )

    # ── /resume nombre ───────────────────────────────────
    elif cmd == "/resume":
        if len(partes) < 2:
            response = "❌ Ejemplo: `/resume Paracetamol`"
        else:
            nombre = partes[1]
            if nombre not in meds:
                response = f"❌ No existe *{nombre}*."
            elif not meds[nombre]["pausado"]:
                response = f"⚠️ *{nombre}* ya está activo."
            else:
                meds[nombre]["pausado"] = False
                registrar_todos_los_horarios()
                response = (
                    f"▶️ *{nombre}* reactivado.\n\n"
                    f"Los recordatorios y el dispensador volverán a funcionar."
                )

    # ── /status ──────────────────────────────────────────
    elif cmd == "/status":
        total       = len(meds)
        activos     = sum(1 for m in meds.values() if not m["pausado"] and tratamiento_activo(m))
        pausados    = sum(1 for m in meds.values() if m["pausado"])
        terminados  = sum(1 for m in meds.values() if not tratamiento_activo(m) and not m["pausado"])
        pendientes  = len(senales_pendientes)
        nombre_p    = profile.get("nombre", "—")

        response = (
            f"📊 *Estado del tratamiento*\n\n"
            f"👤 Paciente: {nombre_p}\n\n"
            f"💊 Medicamentos: {total}\n"
            f"  ✅ Activos:    {activos}\n"
            f"  ⏸ Pausados:   {pausados}\n"
            f"  ⛔ Terminados: {terminados}\n\n"
            f"📡 Señales ESP32 pendientes: {pendientes}\n"
            f"🕐 Hora actual: {datetime.now().strftime('%H:%M')} (El Salvador)"
        )

    # ── /test ────────────────────────────────────────────
    elif cmd == "/test":
        senales_pendientes.append({
            "medicamento": "PRUEBA",
            "horario": datetime.now().strftime("%H:%M"),
            "timestamp": datetime.now().isoformat(),
            "chat_id": chat_id
        })
        response = (
            "🧪 *Señal de prueba enviada*\n\n"
            "El ESP32 la recibirá en su próxima consulta (máx. 10 segundos).\n"
            "El servo debería moverse brevemente."
        )

    await bot.send_message(chat_id=chat_id, text=response, parse_mode="Markdown")
    return {"ok": True}
