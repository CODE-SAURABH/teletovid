from __future__ import annotations

import logging
import sys
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

if __package__:
    from .config import ConfigError, Settings, load_settings
    from .health import HealthServer, KeepAlive
    from .pipeline import PipelineWorker
    from .state import AppState, DailyLimitError, Job
else:
    from config import ConfigError, Settings, load_settings
    from health import HealthServer, KeepAlive
    from pipeline import PipelineWorker
    from state import AppState, DailyLimitError, Job

logger = logging.getLogger("backend.bot")


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _state(context: ContextTypes.DEFAULT_TYPE) -> AppState:
    return context.application.bot_data["state"]


def _worker(context: ContextTypes.DEFAULT_TYPE) -> PipelineWorker:
    return context.application.bot_data["worker"]


def _is_owner(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    return bool(user and settings.owner_id is not None and user.id == settings.owner_id)


def _owner_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return _is_owner(update, _settings(context))


async def _reply(update: Update, text: str, **kwargs) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text, **kwargs)


def _parse_id(value: str) -> Optional[int]:
    try:
        job_id = int(value)
    except (TypeError, ValueError):
        return None
    return job_id if job_id > 0 else None


def _job_label(job: Job) -> str:
    return f"#{job.id} [{job.status}] {job.topic}"


def _queue_text(state: AppState) -> str:
    if not state.queue:
        return "Queue is empty."
    lines = [
        f"Queue ({len(state.queue)})"
        + (" - paused" if state.paused else "")
        + f" | daily {state.daily_remaining}/{state.daily_limit}"
    ]
    for index, job in enumerate(state.queue[:10], start=1):
        lines.append(f"{index}. {_job_label(job)}")
    if len(state.queue) > 10:
        lines.append(f"... and {len(state.queue) - 10} more")
    return "\n".join(lines)


def _queue_keyboard(state: AppState) -> Optional[InlineKeyboardMarkup]:
    rows = []
    for job in state.queue[:10]:
        rows.append(
            [
                InlineKeyboardButton("Up", callback_data=f"q:up:{job.id}"),
                InlineKeyboardButton("Down", callback_data=f"q:down:{job.id}"),
                InlineKeyboardButton("Remove", callback_data=f"q:rm:{job.id}"),
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else None


def _jobs_text(state: AppState, worker: PipelineWorker) -> str:
    lines = ["Queued:"]
    if state.queue:
        lines.extend(f"  {_job_label(job)}" for job in state.queue[:10])
    else:
        lines.append("  none")

    current = worker.current_job
    lines.append("Current:")
    lines.append(f"  {_job_label(current)}" if current else "  none")

    history = state.history(5)
    lines.append("Recent:")
    if history:
        lines.extend(
            f"  #{job.id} [{job.status}] {job.topic}" for job in history
        )
    else:
        lines.append("  none")
    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    worker = _worker(context)
    state = _state(context)
    await _reply(
        update,
        "Bot connected.\n\n"
        f"Agent: {'ready' if worker.ready else 'not ready'}\n"
        f"Queue: {len(state.queue)}\n"
        f"Daily: {state.daily_remaining}/{state.daily_limit} remaining\n"
        "Use /add <topic> to make a video, or /help for commands.",
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    await _reply(update, "pong")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    state = _state(context)
    await _reply(
        update,
        "Commands:\n"
        "  /add <topic> - queue a 9:16 explainer\n"
        "  /now <topic> - jump the queue\n"
        "  /queue - show the queue with buttons\n"
        "  /remove <id> - remove a queued job\n"
        "  /skip - remove the first queued job\n"
        "  /cancel <id> - cancel a job\n"
        "  /retry <id> - requeue a failed job\n"
        "  /pause /resume - stop or start the worker\n"
        "  /status /jobs /profile\n"
        "  /start /ping /help\n\n"
        f"Daily limit: {state.daily_remaining}/{state.daily_limit} remaining (UTC).",
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    topic = " ".join(context.args or []).strip()
    if not topic:
        await _reply(update, "Usage: /add <topic>")
        return
    worker = _worker(context)
    if not worker.ready:
        await _reply(update, f"Agent is not ready: {worker.init_error or 'initializing'}")
        return
    state = _state(context)
    try:
        job = state.add(topic)
    except DailyLimitError as exc:
        await _reply(update, str(exc))
        return
    worker.wake()
    await _reply(
        update,
        f"Added #{job.id}: {job.topic}\n"
        f"Queue position: {len(state.queue)}\n"
        f"Daily left: {state.daily_remaining}/{state.daily_limit}",
    )


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    topic = " ".join(context.args or []).strip()
    if not topic:
        await _reply(update, "Usage: /now <topic>")
        return
    worker = _worker(context)
    if not worker.ready:
        await _reply(update, f"Agent is not ready: {worker.init_error or 'initializing'}")
        return
    state = _state(context)
    try:
        job = state.add(topic, front=True)
    except DailyLimitError as exc:
        await _reply(update, str(exc))
        return
    worker.wake()
    await _reply(
        update,
        f"Priority job #{job.id} added at the front: {job.topic}\n"
        f"Daily left: {state.daily_remaining}/{state.daily_limit}",
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    state = _state(context)
    await _reply(update, _queue_text(state), reply_markup=_queue_keyboard(state))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    if not context.args:
        await _reply(update, "Usage: /remove <id>")
        return
    job_id = _parse_id(context.args[0])
    if job_id is None:
        await _reply(update, "Usage: /remove <id>")
        return
    job = _state(context).remove(job_id)
    if job is None:
        await _reply(update, f"Job #{job_id} is not in the queue.")
        return
    await _reply(update, f"Removed #{job.id}: {job.topic}")


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    state = _state(context)
    if not state.queue:
        await _reply(update, "Queue is empty.")
        return
    queued = state.queue[0]
    state.remove(queued.id)
    await _reply(update, f"Skipped #{queued.id}: {queued.topic}")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    state = _state(context)
    state.paused = True
    await _reply(update, "Paused. The current job can finish; new jobs will wait.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    state = _state(context)
    state.paused = False
    _worker(context).wake()
    await _reply(update, "Resumed.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    state = _state(context)
    worker = _worker(context)
    current = f"{_job_label(worker.current_job)}" if worker.current_job else "none"
    await _reply(
        update,
        f"Agent: {'ready' if worker.ready else 'not ready'}\n"
        f"Model: {_settings(context).agent_model}\n"
        f"Paused: {'yes' if state.paused else 'no'}\n"
        f"Queue: {len(state.queue)}\n"
        f"Daily: {state.daily_remaining}/{state.daily_limit} remaining (UTC)\n"
        f"Current: {current}"
        + (f"\nError: {worker.init_error}" if worker.init_error else ""),
    )


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    await _reply(update, _jobs_text(_state(context), _worker(context)))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    if not context.args:
        await _reply(update, "Usage: /cancel <id>")
        return
    job_id = _parse_id(context.args[0])
    if job_id is None:
        await _reply(update, "Usage: /cancel <id>")
        return
    job = _state(context).find(job_id)
    if job is None:
        await _reply(update, f"Job #{job_id} not found.")
        return
    if not _worker(context).cancel(job_id):
        await _reply(update, f"Job #{job_id} is already {job.status}.")
        return
    await _reply(update, f"Cancelled #{job_id}: {job.topic}")


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    if not context.args:
        await _reply(update, "Usage: /retry <id>")
        return
    job_id = _parse_id(context.args[0])
    if job_id is None:
        await _reply(update, "Usage: /retry <id>")
        return
    job = _state(context).find(job_id)
    if job is None:
        await _reply(update, f"Job #{job_id} not found.")
        return
    if not job.terminal:
        await _reply(update, f"Job #{job_id} is still {job.status}.")
        return
    try:
        _worker(context).requeue(job)
    except DailyLimitError as exc:
        await _reply(update, str(exc))
        return
    state = _state(context)
    await _reply(
        update,
        f"Requeued #{job.id}: {job.topic}\n"
        f"Daily left: {state.daily_remaining}/{state.daily_limit}",
    )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_ok(update, context):
        return
    settings = _settings(context)
    state = _state(context)
    await _reply(
        update,
        f"Owner ID: {settings.owner_id}\n"
        f"Model: {settings.agent_model}\n"
        f"Agent timeout: {settings.agent_timeout:.0f}s\n"
        f"Queue: {len(state.queue)}\n"
        f"Daily: {state.daily_remaining}/{state.daily_limit} remaining (UTC)\n"
        f"Paused: {'yes' if state.paused else 'no'}",
    )


async def queue_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings = _settings(context)
    if query is None:
        return
    if settings.owner_id is None or query.from_user.id != settings.owner_id:
        await query.answer("Not allowed.")
        return

    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "q":
        await query.answer()
        return
    action = parts[1]
    job_id = _parse_id(parts[2])
    if job_id is None:
        await query.answer("Invalid job.")
        return

    state = _state(context)
    message = ""
    if action == "rm":
        job = state.remove(job_id)
        message = f"Removed #{job_id}" if job else f"Job #{job_id} not found"
    elif action in ("up", "down"):
        moved = state.move(job_id, -1 if action == "up" else 1)
        message = "Moved" if moved else "Already at the edge"
    else:
        message = "Unknown action"

    await query.answer(message)
    try:
        await query.edit_message_text(
            _queue_text(state),
            reply_markup=_queue_keyboard(state),
        )
    except BadRequest:
        pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram update error", exc_info=context.error)


async def post_init(app: Application) -> None:
    me = await app.bot.get_me()
    logger.info("Connected as @%s (id=%s)", me.username, me.id)
    worker: PipelineWorker = app.bot_data["worker"]
    state: AppState = app.bot_data["state"]
    settings: Settings = app.bot_data["settings"]
    worker.start()
    if settings.port is not None:
        health = HealthServer(settings.port, state, worker)
        health.start()
        app.bot_data["health"] = health
    else:
        logger.info("PORT is not set; health server disabled.")
    if settings.self_ping_url:
        keepalive = KeepAlive(settings.self_ping_url, settings.self_ping_seconds)
        keepalive.start()
        app.bot_data["keepalive"] = keepalive
    else:
        logger.info("SELF_PING_URL / RENDER_EXTERNAL_URL is not set; keepalive disabled.")
    logger.info("Worker ready=%s", worker.ready)


async def post_shutdown(app: Application) -> None:
    keepalive = app.bot_data.get("keepalive")
    if keepalive is not None:
        keepalive.stop()
    health = app.bot_data.get("health")
    if health is not None:
        health.stop()
    worker: PipelineWorker = app.bot_data["worker"]
    await worker.stop()


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, settings.log_level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if settings.owner_id is None:
        logger.warning("OWNER_TELEGRAM_ID is not set; every command will be ignored.")

    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    state = AppState(settings.daily_job_limit)
    worker = PipelineWorker(app, settings, state)
    app.bot_data["settings"] = settings
    app.bot_data["state"] = state
    app.bot_data["worker"] = worker

    commands = {
        "start": cmd_start,
        "ping": cmd_ping,
        "help": cmd_help,
        "add": cmd_add,
        "now": cmd_now,
        "queue": cmd_queue,
        "remove": cmd_remove,
        "skip": cmd_skip,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "status": cmd_status,
        "jobs": cmd_jobs,
        "cancel": cmd_cancel,
        "retry": cmd_retry,
        "profile": cmd_profile,
    }
    for name, callback in commands.items():
        app.add_handler(CommandHandler(name, callback))

    app.add_handler(CallbackQueryHandler(queue_button, pattern=r"^q:(up|down|rm):\d+$"))
    app.add_error_handler(on_error)

    logger.info("Starting Telegram polling")
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
