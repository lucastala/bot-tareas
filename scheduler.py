import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot

from database import get_active_users
from google_services import get_today_events, get_pending_tasks

logger = logging.getLogger(__name__)

ARGENTINA_TZ = "America/Argentina/Buenos_Aires"


async def send_daily_summary(bot: Bot) -> None:
    users = await get_active_users()
    logger.info(f"Sending daily summary to {len(users)} users")

    for user in users:
        if not user.get("access_token"):
            continue
        try:
            events = await get_today_events(user)
            tasks = await get_pending_tasks(user)

            if events:
                lines = ["📅 Eventos de hoy:"]
                for ev in events:
                    inicio = ev["inicio"]
                    if "T" in inicio:
                        hora = inicio.split("T")[1][:5]
                        lines.append(f"- {hora} {ev['nombre']}")
                    else:
                        lines.append(f"- Todo el día: {ev['nombre']}")
                events_block = "\n".join(lines)
            else:
                events_block = "📅 No tenés eventos para hoy."

            if tasks:
                task_lines = ["📋 Tareas pendientes:"]
                for i, t in enumerate(tasks, 1):
                    task_lines.append(f"{i}. {t['tarea']}")
                tasks_block = "\n".join(task_lines)
            else:
                tasks_block = "No tenés tareas pendientes."

            text = (
                f"☀️ Buenos días! Acá tu resumen de hoy:\n\n"
                f"{events_block}\n\n"
                f"{tasks_block}\n\n"
                f"Usá .texto para agregar tarea. Usá .número para eliminar."
            )

            await bot.send_message(chat_id=user["chat_id"], text=text)

        except Exception as e:
            logger.error(f"Error sending summary to user {user['chat_id']}: {e}")


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=ARGENTINA_TZ)
    scheduler.add_job(
        send_daily_summary,
        CronTrigger(hour=8, minute=0, timezone=ARGENTINA_TZ),
        args=[bot],
        id="daily_summary",
        name="Daily Morning Summary",
        replace_existing=True,
    )
    return scheduler
