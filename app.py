from fastapi import FastAPI, Request
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from contextlib import asynccontextmanager
from datetime import datetime, date
import os

# ══════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════════════════════
TOKEN = os.getenv("BOT_TOKEN")
bot   = Bot(token=TOKEN)

# ══════════════════════════════════════════════════════════
#  BASE DE DATOS EN MEMORIA
#
#  usuarios[chat_id] = {
#    "profile": { "nombre": str, "edad": int, "notas": str },
#    "meds": {
#      "NombreMed": {
#        "horarios":     ["08:00", "14:00", "21:00"],
#        "fecha_inicio": "YYYY-MM-DD" | None,
#        "fecha_fin":    "YYYY-MM-DD" | None,
#        "pausado":      bool
#      }
#    }
#  }
# ══════════════════════════════════════════════════════════
usuarios: dict          = {}
senales_pendientes: list = []   # Cola de señales para el ESP32

# ══════════════════════════════════════════════════════════
#  MENSAJES DE AYUDA POR COMANDO
#  Se muestran cuando el usuario escribe el comando solo,
#  sin los argumentos requeridos.
# ══════════════════════════════════════════════════════════
AYUDA = {
    "/addmed": (
        "➕ *Agregar medicamento*\n\n"
        "Registra un nuevo medicamento en tu lista.\n\n"
        "📌 *Uso:*\n"
        "`/addmed <nombre>`\n\n"
        "📋 *Ejemplo:*\n"
        "`/addmed Paracetamol`\n\n"
        "💡 Después de agregarlo, configura sus horarios con:\n"
        "`/settime Paracetamol 08:00 14:00 21:00`"
    ),
    "/deletemed": (
        "🗑️ *Eliminar medicamento*\n\n"
        "Elimina un medicamento y todos sus horarios y fechas configuradas.\n\n"
        "📌 *Uso:*\n"
        "`/deletemed <nombre>`\n\n"
        "📋 *Ejemplo:*\n"
        "`/deletemed Paracetamol`\n\n"
        "⚠️ Esta acción no se puede deshacer.\n"
        "Si solo quieres detenerlo temporalmente, usa `/pause`."
    ),
    "/settime": (
        "⏰ *Configurar horarios de recordatorio*\n\n"
        "Asigna una o varias horas de recordatorio a un medicamento.\n"
        "Reemplaza los horarios anteriores si ya existían.\n\n"
        "📌 *Uso:*\n"
        "`/settime <nombre> <HH:MM> [HH:MM] [HH:MM] ...`\n\n"
        "📋 *Ejemplos:*\n"
        "`/settime Paracetamol 08:00`\n"
        "`/settime Ibuprofeno 07:30 13:00 20:00`\n\n"
        "🕐 Usa formato de 24 horas (00:00 – 23:59).\n"
        "Puedes agregar hasta 8 horarios por medicamento."
    ),
    "/setdate": (
        "📅 *Configurar duración del tratamiento*\n\n"
        "Define la fecha de inicio y fin de un tratamiento.\n"
        "Fuera de ese rango, el dispensador no se activará para ese medicamento.\n\n"
        "📌 *Uso:*\n"
        "`/setdate <nombre> <YYYY-MM-DD> <YYYY-MM-DD>`\n"
        "               ↑ inicio            ↑ fin\n\n"
        "📋 *Ejemplo:*\n"
        "`/setdate Paracetamol 2025-06-01 2025-06-07`\n\n"
        "💡 Esto significa: tomar Paracetamol del 1 al 7 de junio de 2025."
    ),
    "/pause": (
        "⏸ *Pausar recordatorios de un medicamento*\n\n"
        "Detiene temporalmente los recordatorios y el dispensador\n"
        "para ese medicamento, sin eliminarlo.\n\n"
        "📌 *Uso:*\n"
        "`/pause <nombre>`\n\n"
        "📋 *Ejemplo:*\n"
        "`/pause Paracetamol`\n\n"
        "▶️ Para reactivarlo: `/resume Paracetamol`"
    ),
    "/resume": (
        "▶️ *Reactivar recordatorios de un medicamento*\n\n"
        "Vuelve a activar los recordatorios y el dispensador\n"
        "para un medicamento que estaba pausado.\n\n"
        "📌 *Uso:*\n"
        "`/resume <nombre>`\n\n"
        "📋 *Ejemplo:*\n"
        "`/resume Paracetamol`\n\n"
        "⏸ Para pausarlo de nuevo: `/pause Paracetamol`"
    ),
    "/profile": (
        "👤 *Configurar perfil del paciente*\n\n"
        "Guarda tu nombre, edad y notas médicas relevantes.\n\n"
        "📌 *Uso:*\n"
        "`/profile <nombre> <edad> <notas>`\n\n"
        "📋 *Ejemplos:*\n"
        "`/profile Juan 30`\n"
        "`/profile Maria 45 Diabética, alérgica a la penicilina`\n\n"
        "💡 Las notas son opcionales pero útiles para recordar\n"
        "condiciones importantes del tratamiento.\n\n"
        "Para *ver* tu perfil actual, escribe `/profile` sin argumentos."
    ),
}

# ══════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════
scheduler = AsyncIOScheduler(timezone="America/El_Salvador")


def registrar_todos_los_horarios():
    """Reconstruye todos los jobs del scheduler desde cero."""
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
    """Se ejecuta al llegar la hora. Valida fechas, notifica y encola señal para ESP32."""
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
#  El ESP32 consulta GET /esp32/signal cada ~10 segundos.
#  Si hay señal pendiente, la devuelve y la elimina de la cola.
# ══════════════════════════════════════════════════════════
@app.get("/")
async def home():
    return {"status": "Bot Medic funcionando ✅"}


@app.get("/esp32/signal")
async def esp32_signal():
    if senales_pendientes:
        senal = senales_pendientes.pop(0)
        return {"dispensar": True, "datos": senal}
    return {"dispensar": False}


# ══════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════
def init_user(chat_id):
    if chat_id not in usuarios:
        usuarios[chat_id] = {"profile": {}, "meds": {}}


def fmt_fecha(iso) -> str:
    if not iso:
        return "—"
    try:
        return date.fromisoformat(iso).strftime("%d/%m/%Y")
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
    partes  = text.split()
    cmd     = partes[0].lower() if partes else ""

    # ── Comandos reconocidos que necesitan argumentos ──
    # Si el usuario los escribe solos, mostramos su ayuda específica.
    if cmd in AYUDA and len(partes) == 1:
        response = AYUDA[cmd]

    # ── /start ──────────────────────────────────────────
    elif cmd == "/start":
        nombre = profile.get("nombre", "")
        saludo = f"Hola, *{nombre}*. " if nombre else ""
        response = (
            f"💊 *Bot Medic*\n"
            f"_Dispensador inteligente de medicamentos_\n\n"
            f"{saludo}Aquí tienes una guía rápida para comenzar:\n\n"
            f"*1️⃣ Configura tu perfil:*\n"
            f"`/profile Nombre Edad Notas`\n\n"
            f"*2️⃣ Agrega un medicamento:*\n"
            f"`/addmed Paracetamol`\n\n"
            f"*3️⃣ Define sus horarios:*\n"
            f"`/settime Paracetamol 08:00 14:00 21:00`\n\n"
            f"*4️⃣ Opcionalmente, define la duración:*\n"
            f"`/setdate Paracetamol 2025-06-01 2025-06-07`\n\n"
            f"Escribe /help para ver todos los comandos disponibles."
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
            "`/addmed` `<nombre>` — Agregar medicamento\n"
            "`/listmed` — Ver todos tus medicamentos\n"
            "`/deletemed` `<nombre>` — Eliminar medicamento\n\n"

            "─────────────────────────\n"
            "⏰ *HORARIOS*\n"
            "`/settime` `<nombre> <HH:MM> ...` — Configurar horarios\n\n"

            "─────────────────────────\n"
            "📅 *FECHAS DE TRATAMIENTO*\n"
            "`/setdate` `<nombre> <inicio> <fin>` — Duración del tratamiento\n\n"

            "─────────────────────────\n"
            "🔔 *RECORDATORIOS*\n"
            "`/reminders` — Ver recordatorios activos\n"
            "`/pause` `<nombre>` — Pausar un medicamento\n"
            "`/resume` `<nombre>` — Reactivar un medicamento\n\n"

            "─────────────────────────\n"
            "📊 *SISTEMA*\n"
            "`/status` — Resumen general del tratamiento\n"
            "`/test` — Probar el dispensador manualmente\n\n"

            "💡 _Escribe cualquier comando solo para ver\n"
            "instrucciones detalladas de cómo usarlo._"
        )

    # ── /profile [Nombre Edad Notas] ─────────────────────
    elif cmd == "/profile":
        if len(partes) < 3:
            # Sin argumentos o falta edad: mostrar perfil actual
            if profile:
                response = (
                    f"👤 *Tu perfil*\n\n"
                    f"Nombre : {profile.get('nombre', '—')}\n"
                    f"Edad   : {profile.get('edad', '—')} años\n"
                    f"Notas  : {profile.get('notas', '—')}\n\n"
                    f"_Para actualizar tu perfil:_\n"
                    f"`/profile Nombre Edad Notas médicas`"
                )
            else:
                response = AYUDA["/profile"]
        else:
            nombre = partes[1]
            try:
                edad = int(partes[2])
            except ValueError:
                response = (
                    "❌ *La edad debe ser un número entero.*\n\n"
                    "Ejemplo: `/profile Juan 30 Diabético`"
                )
                await bot.send_message(chat_id=chat_id, text=response, parse_mode="Markdown")
                return {"ok": True}

            notas = " ".join(partes[3:]) if len(partes) > 3 else "—"
            profile["nombre"] = nombre
            profile["edad"]   = edad
            profile["notas"]  = notas
            response = (
                f"✅ *Perfil guardado correctamente*\n\n"
                f"👤 Nombre : {nombre}\n"
                f"🎂 Edad   : {edad} años\n"
                f"📝 Notas  : {notas}"
            )

    # ── /addmed <nombre> ─────────────────────────────────
    elif cmd == "/addmed":
        nombre = partes[1]
        if nombre in meds:
            response = (
                f"⚠️ El medicamento *{nombre}* ya existe en tu lista.\n\n"
                f"Puedes ver todos tus medicamentos con `/listmed`."
            )
        else:
            meds[nombre] = {
                "horarios":     [],
                "fecha_inicio": None,
                "fecha_fin":    None,
                "pausado":      False
            }
            response = (
                f"✅ *Medicamento agregado: {nombre}*\n\n"
                f"Ahora configura sus horarios de recordatorio:\n"
                f"`/settime {nombre} 08:00 14:00 21:00`\n\n"
                f"_(Opcional) Define también la duración del tratamiento:_\n"
                f"`/setdate {nombre} 2025-06-01 2025-06-07`"
            )

    # ── /listmed ─────────────────────────────────────────
    elif cmd == "/listmed":
        if not meds:
            response = (
                "📭 *No tienes medicamentos registrados.*\n\n"
                "Agrega tu primer medicamento con:\n"
                "`/addmed NombreMedicamento`"
            )
        else:
            response = f"💊 *Tus medicamentos ({len(meds)}):*\n\n"
            for nombre, med in meds.items():
                horas = " · ".join(f"`{h}`" for h in med["horarios"]) if med["horarios"] else "_sin horarios_"
                fi    = fmt_fecha(med.get("fecha_inicio"))
                ff    = fmt_fecha(med.get("fecha_fin"))
                est   = estado_med(med)
                response += (
                    f"*{nombre}*  —  {est}\n"
                    f"  ⏰ {horas}\n"
                    f"  📅 {fi} → {ff}\n\n"
                )

    # ── /deletemed <nombre> ──────────────────────────────
    elif cmd == "/deletemed":
        nombre = partes[1]
        if nombre not in meds:
            response = (
                f"❌ El medicamento *{nombre}* no existe en tu lista.\n\n"
                f"Consulta tus medicamentos con `/listmed`."
            )
        else:
            del meds[nombre]
            registrar_todos_los_horarios()
            response = (
                f"🗑️ *{nombre}* ha sido eliminado correctamente.\n\n"
                f"Se han borrado también sus horarios y fechas.\n"
                f"Consulta tu lista actualizada con `/listmed`."
            )

    # ── /settime <nombre> <HH:MM> [...] ──────────────────
    elif cmd == "/settime":
        if len(partes) < 3:
            response = AYUDA["/settime"]
        else:
            nombre = partes[1]
            horas  = partes[2:]

            if nombre not in meds:
                response = (
                    f"❌ El medicamento *{nombre}* no existe.\n\n"
                    f"Agrégalo primero con:\n"
                    f"`/addmed {nombre}`"
                )
            else:
                invalidas = [h for h in horas if not _hora_valida(h)]
                if invalidas:
                    response = (
                        f"❌ *Formato de hora incorrecto:* `{'`, `'.join(invalidas)}`\n\n"
                        f"Usa el formato de 24 horas: `HH:MM`\n\n"
                        f"📋 *Ejemplos válidos:*\n"
                        f"`08:00` · `13:30` · `21:00` · `00:15`"
                    )
                else:
                    meds[nombre]["horarios"] = sorted(list(set(horas)))
                    registrar_todos_los_horarios()
                    lista = "\n".join(f"  • `{h}`" for h in meds[nombre]["horarios"])
                    response = (
                        f"✅ *Horarios configurados para {nombre}:*\n\n"
                        f"{lista}\n\n"
                        f"_El dispensador se activará automáticamente\n"
                        f"en cada uno de estos horarios._"
                    )

    # ── /setdate <nombre> <YYYY-MM-DD> <YYYY-MM-DD> ──────
    elif cmd == "/setdate":
        if len(partes) < 4:
            response = AYUDA["/setdate"]
        else:
            nombre = partes[1]
            f_ini  = partes[2]
            f_fin  = partes[3]

            if nombre not in meds:
                response = (
                    f"❌ El medicamento *{nombre}* no existe.\n\n"
                    f"Agrégalo primero con:\n"
                    f"`/addmed {nombre}`"
                )
            else:
                try:
                    di   = date.fromisoformat(f_ini)
                    df   = date.fromisoformat(f_fin)
                    if df < di:
                        raise ValueError("Fin antes que inicio")
                    dias = (df - di).days + 1
                    meds[nombre]["fecha_inicio"] = f_ini
                    meds[nombre]["fecha_fin"]    = f_fin
                    response = (
                        f"✅ *Duración del tratamiento configurada*\n\n"
                        f"💊 Medicamento : *{nombre}*\n"
                        f"📅 Inicio      : {fmt_fecha(f_ini)}\n"
                        f"📅 Fin         : {fmt_fecha(f_fin)}\n"
                        f"⏳ Duración    : {dias} día{'s' if dias != 1 else ''}\n\n"
                        f"_El dispensador solo se activará dentro\n"
                        f"de este rango de fechas._"
                    )
                except ValueError:
                    response = (
                        f"❌ *Fechas inválidas o en orden incorrecto.*\n\n"
                        f"📌 *Formato requerido:* `YYYY-MM-DD`\n\n"
                        f"📋 *Ejemplo correcto:*\n"
                        f"`/setdate {nombre} 2025-06-01 2025-06-07`\n\n"
                        f"⚠️ La fecha de fin debe ser igual o posterior\n"
                        f"a la fecha de inicio."
                    )

    # ── /reminders ───────────────────────────────────────
    elif cmd == "/reminders":
        activos = {
            n: m for n, m in meds.items()
            if not m["pausado"] and tratamiento_activo(m) and m["horarios"]
        }
        if not activos:
            response = (
                "📭 *No tienes recordatorios activos en este momento.*\n\n"
                "Posibles causas:\n"
                "• No tienes medicamentos registrados\n"
                "• Todos están pausados\n"
                "• Los tratamientos activos no tienen horarios\n"
                "• Algún tratamiento ya finalizó\n\n"
                "Usa `/listmed` para revisar el estado de cada medicamento."
            )
        else:
            response = f"🔔 *Recordatorios activos ({len(activos)}):*\n\n"
            for nombre, med in activos.items():
                horas_str = "  ·  ".join(f"`{h}`" for h in med["horarios"])
                fi        = fmt_fecha(med.get("fecha_inicio"))
                ff        = fmt_fecha(med.get("fecha_fin"))
                response += (
                    f"💊 *{nombre}*\n"
                    f"  ⏰ {horas_str}\n"
                    f"  📅 {fi} → {ff}\n\n"
                )

    # ── /pause <nombre> ──────────────────────────────────
    elif cmd == "/pause":
        nombre = partes[1]
        if nombre not in meds:
            response = (
                f"❌ El medicamento *{nombre}* no existe en tu lista.\n\n"
                f"Consulta tus medicamentos con `/listmed`."
            )
        elif meds[nombre]["pausado"]:
            response = (
                f"⚠️ *{nombre}* ya se encuentra pausado.\n\n"
                f"Para reactivarlo usa:\n"
                f"`/resume {nombre}`"
            )
        else:
            meds[nombre]["pausado"] = True
            registrar_todos_los_horarios()
            response = (
                f"⏸ *{nombre}* ha sido pausado.\n\n"
                f"Los recordatorios y el dispensador no se activarán\n"
                f"para este medicamento hasta que lo reactives con:\n"
                f"`/resume {nombre}`"
            )

    # ── /resume <nombre> ─────────────────────────────────
    elif cmd == "/resume":
        nombre = partes[1]
        if nombre not in meds:
            response = (
                f"❌ El medicamento *{nombre}* no existe en tu lista.\n\n"
                f"Consulta tus medicamentos con `/listmed`."
            )
        elif not meds[nombre]["pausado"]:
            response = (
                f"⚠️ *{nombre}* ya está activo, no estaba pausado.\n\n"
                f"Para pausarlo usa:\n"
                f"`/pause {nombre}`"
            )
        else:
            meds[nombre]["pausado"] = False
            registrar_todos_los_horarios()
            response = (
                f"▶️ *{nombre}* ha sido reactivado.\n\n"
                f"Los recordatorios y el dispensador volverán a\n"
                f"funcionar según sus horarios configurados."
            )

    # ── /status ──────────────────────────────────────────
    elif cmd == "/status":
        total      = len(meds)
        activos    = sum(1 for m in meds.values() if not m["pausado"] and tratamiento_activo(m) and m["horarios"])
        pausados   = sum(1 for m in meds.values() if m["pausado"])
        sin_horas  = sum(1 for m in meds.values() if not m["horarios"] and not m["pausado"])
        terminados = sum(1 for m in meds.values() if not tratamiento_activo(m) and not m["pausado"])
        pendientes = len(senales_pendientes)
        nombre_p   = profile.get("nombre", "—")
        edad_p     = profile.get("edad", "—")
        hora_actual = datetime.now().strftime("%H:%M")
        fecha_actual = datetime.now().strftime("%d/%m/%Y")

        response = (
            f"📊 *Estado del sistema — Bot Medic*\n\n"

            f"👤 *Paciente:* {nombre_p}"
            + (f" ({edad_p} años)" if edad_p != "—" else "") + "\n"
            f"🗓 *Fecha:* {fecha_actual}  🕐 *Hora:* {hora_actual}\n\n"

            f"─────────────────────────\n"
            f"💊 *Medicamentos:* {total}\n"
            f"  ✅ Activos con horario : {activos}\n"
            f"  ⏸ Pausados            : {pausados}\n"
            f"  ⚠️ Sin horarios        : {sin_horas}\n"
            f"  ⛔ Tratamiento terminado: {terminados}\n\n"

            f"─────────────────────────\n"
            f"📡 *ESP32:*\n"
            f"  Señales pendientes: {pendientes}\n\n"

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
        if cmd.startswith("/"):
            response = (
                f"❓ *Comando no reconocido:* `{cmd}`\n\n"
                f"Escribe /help para ver la lista completa\n"
                f"de comandos disponibles."
            )
        else:
            response = (
                "💬 No entiendo ese mensaje.\n\n"
                "Usa los comandos del menú o escribe /help\n"
                "para ver qué puedo hacer."
            )

    await bot.send_message(chat_id=chat_id, text=response, parse_mode="Markdown")
    return {"ok": True}


# ══════════════════════════════════════════════════════════
#  UTILIDADES
# ══════════════════════════════════════════════════════════
def _hora_valida(h: str) -> bool:
    try:
        datetime.strptime(h, "%H:%M")
        return True
    except ValueError:
        return False
