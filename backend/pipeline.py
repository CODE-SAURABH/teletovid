from __future__ import annotations

import asyncio
import io
import logging
import re
from time import time
from typing import Optional

from telegram import InputFile
from telegram.ext import Application

if __package__:
    from .agent import AgentResult, DeepAgentRunner
    from .config import Settings
    from .state import AppState, Job
else:
    from agent import AgentResult, DeepAgentRunner
    from config import Settings
    from state import AppState, Job

logger = logging.getLogger(__name__)

MAX_TELEGRAM_UPLOAD = 50 * 1024 * 1024


class PipelineError(RuntimeError):
    pass


def topic_slug(topic: str) -> str:
    value = re.sub(r"[^\w\s-]", "", (topic or "").lower(), flags=re.UNICODE)
    value = re.sub(r"[\s_-]+", "-", value).strip("-")
    return (value[:60].strip("-") or "explainer")


class PipelineWorker:
    def __init__(self, app: Application, settings: Settings, state: AppState) -> None:
        self.app = app
        self.settings = settings
        self.state = state
        self._task: Optional[asyncio.Task] = None
        self._job_task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self.current_job: Optional[Job] = None
        self._runner: Optional[DeepAgentRunner] = None
        self._init_error: Optional[str] = None
        try:
            self._runner = DeepAgentRunner(settings)
        except Exception as exc:
            self._init_error = f"Agent initialization failed: {exc}"
            logger.exception("DeepAgent initialization failed")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="pipeline-worker")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._job_task is not None and not self._job_task.done():
            self._job_task.cancel()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Pipeline worker stopped with an error")

    def wake(self) -> None:
        self._wake.set()

    @property
    def init_error(self) -> Optional[str]:
        return self._init_error

    @property
    def ready(self) -> bool:
        return self._runner is not None and self._init_error is None

    def requeue(self, job: Job) -> None:
        self.state.take_slot(job)
        job.status = "queued"
        job.stage = "queued"
        job.error = None
        job.updated_at = time()
        if job not in self.state.queue:
            self.state.queue.append(job)
        self.wake()

    def cancel(self, job_id: int) -> bool:
        job = self.state.find(job_id)
        if job is None:
            return False
        if self.current_job is not None and self.current_job.id == job_id:
            job.status = "cancelled"
            job.stage = "cancelled"
            job.updated_at = time()
            if self._job_task is not None and not self._job_task.done():
                self._job_task.cancel()
            return True
        removed = self.state.remove(job_id)
        return removed is not None

    async def _run(self) -> None:
        logger.info("Pipeline worker started.")
        while not self._stop.is_set():
            self._wake.clear()
            job = self.state.next_ready()
            if job is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue

            self.state.claim(job)
            self.current_job = job
            self._job_task = asyncio.create_task(self._process(job))
            try:
                await self._job_task
            except asyncio.CancelledError:
                if job.status != "cancelled":
                    job.status = "cancelled"
                    job.stage = "cancelled"
                    job.updated_at = time()
                logger.info("Job #%s cancelled", job.id)
            except Exception as exc:
                logger.exception("Job #%s failed", job.id)
                if job.status != "cancelled":
                    job.status = "failed"
                    job.stage = "failed"
                    job.error = str(exc)[:1000]
                    job.updated_at = time()
                    await self._notify(
                        job,
                        f"Job #{job.id} failed.\n\n{job.error}\n\nUse /retry {job.id}.",
                    )
            finally:
                self.current_job = None
                self._job_task = None

    async def _process(self, job: Job) -> None:
        if self._init_error is not None:
            raise PipelineError(self._init_error)
        if self._runner is None:
            raise PipelineError("DeepAgent is not initialized.")

        job.status = "researching"
        job.stage = "research"
        job.updated_at = time()
        await self._notify(job, f"Job #{job.id} started: {job.topic}")

        try:
            result = await asyncio.wait_for(
                self._runner.run(job.topic),
                timeout=self.settings.agent_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise PipelineError(
                f"Agent timed out after {self.settings.agent_timeout:.0f}s."
            ) from exc

        job.script_json = result.script_json
        job.html = result.html
        job.status = "delivering"
        job.stage = "deliver"
        job.updated_at = time()
        await self._deliver(job, result)

        job.status = "delivered"
        job.stage = "delivered"
        job.updated_at = time()
        duration = _duration(result)
        title = str(result.script.get("title") or job.topic)
        await self._notify(
            job,
            f"Delivered #{job.id}: {title}\n"
            f"Duration: {duration:.1f}s\n"
            f"Files: {topic_slug(job.topic)}.html, {topic_slug(job.topic)}-script.json",
        )

    async def _deliver(self, job: Job, result: AgentResult) -> None:
        html_bytes = result.html.encode("utf-8")
        script_bytes = result.script_json.encode("utf-8")
        if len(html_bytes) > MAX_TELEGRAM_UPLOAD:
            raise PipelineError("HTML exceeds Telegram's 50 MB limit.")
        if len(script_bytes) > MAX_TELEGRAM_UPLOAD:
            raise PipelineError("Script exceeds Telegram's 50 MB limit.")

        slug = topic_slug(job.topic)
        await self._send_bytes(html_bytes, f"{slug}.html")
        await self._send_bytes(script_bytes, f"{slug}-script.json")

    async def _send_bytes(self, data: bytes, filename: str) -> None:
        if self.settings.owner_id is None:
            raise PipelineError("OWNER_TELEGRAM_ID is not configured.")
        document = InputFile(io.BytesIO(data), filename=filename)
        await self.app.bot.send_document(
            chat_id=self.settings.owner_id,
            document=document,
            filename=filename,
        )

    async def _notify(self, job: Job, text: str) -> None:
        if self.settings.owner_id is None:
            return
        try:
            await self.app.bot.send_message(chat_id=self.settings.owner_id, text=text)
        except Exception:
            logger.exception("Could not send notification for #%s", job.id)


def _duration(result: AgentResult) -> float:
    total = 0.0
    for scene in result.script.get("scenes") or []:
        if isinstance(scene, dict):
            try:
                total += float(scene.get("duration") or 0)
            except (TypeError, ValueError):
                pass
    return total
