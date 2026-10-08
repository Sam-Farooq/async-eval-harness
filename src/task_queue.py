from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class QueueFull(Exception):
    pass


@dataclass
class Task:
    id: str
    fn: Callable[[], Awaitable[Any]]
    max_retries: int = 3
    attempts: int = 0
    result: Any = None
    error: BaseException | None = None
    enqueued_at: float = field(default_factory=time.monotonic)

    @property
    def age(self) -> float:
        return time.monotonic() - self.enqueued_at


class TaskQueue:
    """Worker pool over an asyncio.Queue with bounded admission and retries.

    Admission is bounded by hand rather than Queue(maxsize=...) because a full
    queue should shed the submission, not block the producer behind it.
    """

    def __init__(
        self,
        workers: int = 8,
        max_buffer: int = 1000,
        max_results: int = 10_000,
        retry_backoff: float = 0.001,
    ) -> None:
        self.workers = workers
        self.max_buffer = max_buffer
        self.max_results = max_results
        self.retry_backoff = retry_backoff

        self._queue: asyncio.Queue[Task] = asyncio.Queue()
        self._done: dict[str, Task] = {}
        self._inflight: set[str] = set()
        self._pool: list[asyncio.Task[None]] = []
        self._closed = False
        self.shed = 0

    async def submit(self, task: Task) -> None:
        if self._closed:
            raise RuntimeError("queue is closed")
        if self._queue.qsize() >= self.max_buffer:
            self.shed += 1
            raise QueueFull(task.id)
        self._queue.put_nowait(task)

    async def _worker(self, name: str) -> None:
        while True:
            task = await self._queue.get()
            await self._execute(task)
            self._queue.task_done()

    async def _execute(self, task: Task) -> None:
        self._inflight.add(task.id)
        try:
            task.attempts += 1
            task.result = await task.fn()
            self._done[task.id] = task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 retry is the whole point
            task.error = exc
            if task.attempts <= task.max_retries:
                await asyncio.sleep(self.retry_backoff * task.attempts)
                await self._queue.put(task)
            else:
                log.warning("task %s exhausted retries: %r", task.id, exc)
                self._done[task.id] = task
        finally:
            self._inflight.discard(task.id)

    def start(self) -> None:
        if self._pool:
            raise RuntimeError("already started")
        self._pool = [
            asyncio.create_task(self._worker(f"w{i}"), name=f"worker-{i}")
            for i in range(self.workers)
        ]

    async def drain(self) -> None:
        await self._queue.join()

    async def close(self) -> None:
        self._closed = True
        for w in self._pool:
            w.cancel()
        await asyncio.gather(*self._pool, return_exceptions=True)
        self._pool.clear()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def completed(self) -> int:
        return len(self._done)

    def result(self, task_id: str) -> Task | None:
        return self._done.get(task_id)
