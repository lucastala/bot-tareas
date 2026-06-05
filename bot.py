import io
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import create_user, get_user
from google_services import (
    add_task,
    create_event,
    delete_task_by_position,
    get_events_by_date,
    get_pending_tasks,
    get_today_events,
    search_event,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PAYMENT_LINK = os.getenv("PAYMENT_LINK", "https://tu-link-de-pago.com")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
ARGENTINA_TZ = timezone(timedelta(hours=-3))

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_today_events",
            "description": "Obtiene los eventos de hoy del Google Calendar del usuario",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_events_by_date",
            "description": "Obtiene los eventos de una fecha específica del Google Calendar",
            "parameters": {
                "type": "object",
                "properties": {
                    "fecha": {
                        "type": "string",
                        "description": "Fecha en formato YYYY-MM-DD",
                    }
                },
                "required": ["fecha"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_event",
            "description": "Busca un evento por nombre o descripción en Google Calendar",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Término de búsqueda del evento",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "Crea un nuevo evento en Google Calendar",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string", "description": "Título del evento"},
                    "fecha": {
                        "type": "string",
                        "description": "Fecha en formato YYYY-MM-DD",
                    },
                    "hora": {
                        "type": "string",
                        "description": (
                            "Hora en formato HH:MM (opcional). "
                            "Si no se provee se crea como evento de todo el día."
                        ),
                    },
                },
                "required": ["nombre", "fecha"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pending_tasks",
            "description": "Obtiene las tareas pendientes del usuario desde Google Sheets",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


async def build_tasks_footer(user: dict) -> str:
    try:
        tasks = await get_pending_tasks(user)
    except Exception as e:
        logger.error(f"Error fetching tasks for user {user.get('chat_id')}: {e}")
        return "⚠️ No se pudieron cargar las tareas pendientes."

    if not tasks:
        return "No tenés tareas pendientes.\n\nUsá .texto para agregar tarea."

    lines = ["📋 Tareas pendientes:"]
    for i, task in enumerate(tasks, 1):
        lines.append(f"{i}. {task['tarea']}")
    lines.append("\nUsá .texto para agregar tarea. Usá .número para eliminar.")
    return "\n".join(lines)


async def _execute_tool(func_name: str, func_args: dict, user: dict):
    if func_name == "get_today_events":
        return await get_today_events(user)
    if func_name == "get_events_by_date":
        return await get_events_by_date(user, func_args["fecha"])
    if func_name == "search_event":
        return await search_event(user, func_args["query"])
    if func_name == "create_event":
        return await create_event(
            user, func_args["nombre"], func_args["fecha"], func_args.get("hora")
        )
    if func_name == "get_pending_tasks":
        return await get_pending_tasks(user)
    return {"error": f"Función desconocida: {func_name}"}


async def _call_openai(user: dict, text: str) -> str:
    today = datetime.now(ARGENTINA_TZ).strftime("%Y-%m-%d")
    messages = [
        {
            "role": "system",
            "content": (
                "Sos un asistente personal de productividad. "
                "Ayudás a gestionar tareas y eventos de Google Calendar. "
                "Respondé en español rioplatense, de forma concisa y amigable. "
                f"La fecha de hoy es {today}. "
                "Si el usuario menciona días relativos (mañana, el lunes, etc.), "
                "calculá la fecha correcta a partir de hoy. "
                "Cuando el usuario pide una hora en punto ('a las 4', 'a las 10'), "
                "usá siempre HH:00 como minutos."
            ),
        },
        {"role": "user", "content": text},
    ]

    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tools=OPENAI_TOOLS,
        tool_choice="auto",
    )

    msg = response.choices[0].message

    if not msg.tool_calls:
        return msg.content or "No pude procesar tu mensaje."

    messages.append(msg)

    for tc in msg.tool_calls:
        func_name = tc.function.name
        func_args = json.loads(tc.function.arguments)
        logger.info(f"Tool call: {func_name}({func_args}) for user {user['chat_id']}")
        result = await _execute_tool(func_name, func_args, user)
        messages.append(
            {
                "tool_call_id": tc.id,
                "role": "tool",
                "name": func_name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            }
        )

    final = await openai_client.chat.completions.create(
        model="gpt-4o-mini", messages=messages
    )
    return final.choices[0].message.content or "Listo."


async def _transcribe_voice(voice_bytes: bytes) -> str:
    buf = io.BytesIO(voice_bytes)
    buf.name = "audio.ogg"
    result = await openai_client.audio.transcriptions.create(
        model="whisper-1", file=buf, language="es"
    )
    return result.text


async def _route_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict, text: str
) -> None:
    message = update.message

    # Dot commands ───────────────────────────────────────────────────────────
    if text.startswith("."):
        content = text[1:].strip()

        if re.match(r"^\d+$", content):
            pos = int(content)
            success = await delete_task_by_position(user, pos)
            prefix = (
                f"✅ Tarea #{pos} eliminada.\n\n"
                if success
                else f"⚠️ No encontré la tarea #{pos}.\n\n"
            )
        elif content:
            await add_task(user, content)
            prefix = f"✅ Tarea agregada: {content}\n\n"
        else:
            prefix = "⚠️ Usá .texto para agregar o .número para eliminar.\n\n"

        footer = await build_tasks_footer(user)
        await message.reply_text(prefix + footer)
        return

    # OpenAI function calling ────────────────────────────────────────────────
    try:
        reply = await _call_openai(user, text)
    except Exception as e:
        logger.error(f"OpenAI error for user {user['chat_id']}: {e}")
        reply = "⚠️ Tuve un error procesando tu mensaje. Intentá de nuevo."

    footer = await build_tasks_footer(user)
    await message.reply_text(reply + "\n\n" + footer)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return

    chat_id = update.effective_chat.id
    user = await get_user(chat_id)

    # New user — start onboarding
    if user is None:
        await _start_onboarding(update)
        return

    # OAuth not completed yet
    if not user.get("access_token"):
        oauth_url = f"{BASE_URL}/oauth/start?chat_id={chat_id}"
        await message.reply_text(
            f"⚠️ Todavía no conectaste tu cuenta de Google.\n\n"
            f"Completá la configuración aquí:\n{oauth_url}"
        )
        return

    # Subscription check
    if user.get("estado_suscripcion") not in ("activo", "trial"):
        await message.reply_text(
            f"⚠️ Tu suscripción no está activa.\n\nActivá tu plan aquí:\n{PAYMENT_LINK}"
        )
        return

    await message.reply_chat_action("typing")

    # Voice → transcribe → re-route
    if message.voice:
        try:
            voice_file = await message.voice.get_file()
            raw = await voice_file.download_as_bytearray()
            text = await _transcribe_voice(bytes(raw))
            await message.reply_text(f"🗣️ Transcripción: {text}")
        except Exception as e:
            logger.error(f"Voice transcription error for user {chat_id}: {e}")
            await message.reply_text(
                "⚠️ No pude transcribir el audio. Intentá de nuevo."
            )
            return
    else:
        text = message.text or ""

    if not text.strip():
        return

    await _route_text(update, context, user, text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = await get_user(chat_id)

    if user and user.get("access_token"):
        await update.message.reply_text(
            "👋 ¡Hola! Ya estás configurado y listo.\n\n"
            "Podés decirme:\n"
            "• .texto → agregar tarea\n"
            "• .número → eliminar tarea por número\n"
            "• 'qué tengo hoy' → ver eventos del día\n"
            "• 'crear reunión el viernes a las 10' → agregar evento\n"
            "• Audio de voz 🎤 → lo transcribo y proceso"
        )
    else:
        await _start_onboarding(update)


async def _start_onboarding(update: Update) -> None:
    chat_id = update.effective_chat.id
    nombre = update.effective_user.first_name or "Usuario"

    if not await get_user(chat_id):
        await create_user(chat_id, nombre)

    oauth_url = f"{BASE_URL}/oauth/start?chat_id={chat_id}"
    await update.message.reply_text(
        f"👋 ¡Hola {nombre}! Bienvenido a tu asistente personal.\n\n"
        f"Para empezar, necesito conectar tu cuenta de Google. "
        f"Esto me da acceso a tu Calendar y crea tu hoja de tareas en Google Sheets.\n\n"
        f"👉 Hacé clic aquí para autorizar:\n{oauth_url}\n\n"
        f"Una vez que completes la autorización, ¡ya podés usar el bot!"
    )


async def _post_init(application: Application) -> None:
    from scheduler import setup_scheduler

    scheduler = setup_scheduler(application.bot)
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    logger.info("Scheduler started — daily summary at 08:00 Argentina time")


async def _post_shutdown(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_TOKEN not set in .env")

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))

    logger.info("Bot starting — polling for updates")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
