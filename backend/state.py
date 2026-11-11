from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import time
from typing import Dict, List, Optional

TERMINAL_STATUSES = {"delivered", "failed", "cancelled"}
READY_STATUSES = {"queued", "retry"}


class DailyLimitError(RuntimeError):
    pass


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class Job:
    id: int
    topic: str
    status: str = "queued"
    stage: str = "queued"
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)
    error: Optional[str] = None
    script_json: Optional[str] = None
    html: Optional[str] = None
    counted_date: Optional[str] = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class AppState:
    """In-memory queue and job state. No database, no disk persistence."""

    def __init__(self, daily_limit: int = 3) -> None:
        self.jobs: Dict[int, Job] = {}
        self.queue: List[Job] = []
        self.next_id = 1
        self.paused = False
        self.daily_limit = max(1, int(daily_limit))
        self.daily_date = utc_day()
        self.daily_count = 0

    def _rollover(self) -> None:
        today = utc_day()
        if today != self.daily_date:
            self.daily_date = today
            self.daily_count = 0

    @property
    def daily_remaining(self) -> int:
        self._rollover()
        return max(0, self.daily_limit - self.daily_count)

    def take_slot(self, job: Optional[Job] = None) -> None:
        self._rollover()
        if self.daily_count >= self.daily_limit:
            raise DailyLimitError(
                f"Daily limit reached ({self.daily_limit} runs/day, resets 00:00 UTC)."
            )
        self.daily_count += 1
        if job is not None:
            job.counted_date = self.daily_date

    def add(self, topic: str, *, front: bool = False) -> Job:
        self._rollover()
        job = Job(id=self.next_id, topic=topic)
        self.take_slot(job)
        self.next_id += 1
        self.jobs[job.id] = job
        if front:
            self.queue.insert(0, job)
        else:
            self.queue.append(job)
        return job

    def find(self, job_id: int) -> Optional[Job]:
        return self.jobs.get(job_id)

    def remove(self, job_id: int) -> Optional[Job]:
        self._rollover()
        for i, job in enumerate(self.queue):
            if job.id == job_id:
                self.queue.pop(i)
                self.jobs.pop(job_id, None)
                if job.counted_date == self.daily_date:
                    self.daily_count = max(0, self.daily_count - 1)
                job.counted_date = None
                return job
        return None

    def move(self, job_id: int, delta: int) -> bool:
        for i, job in enumerate(self.queue):
            if job.id != job_id:
                continue
            target = i + delta
            if target < 0 or target >= len(self.queue):
                return False
            self.queue[i], self.queue[target] = self.queue[target], self.queue[i]
            return True
        return False

    def claim(self, job: Job) -> None:
        try:
            self.queue.remove(job)
        except ValueError:
            pass

    def next_ready(self) -> Optional[Job]:
        if self.paused:
            return None
        for job in self.queue:
            if job.status in READY_STATUSES:
                return job
        return None

    def active(self) -> List[Job]:
        return [job for job in self.jobs.values() if not job.terminal]

    def history(self, limit: int = 10) -> List[Job]:
        done = [job for job in self.jobs.values() if job.terminal]
        return sorted(done, key=lambda item: item.updated_at, reverse=True)[:limit]

    def touch(self, job: Job, *, status: Optional[str] = None, stage: Optional[str] = None) -> None:
        if status is not None:
            job.status = status
        if stage is not None:
            job.stage = stage
        job.updated_at = time()
