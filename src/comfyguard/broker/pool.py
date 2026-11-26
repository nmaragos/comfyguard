"""Multi-worker ComfyUI queue, backpressure, OOM retry, and cancellation pool."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from comfyguard.broker.client import (
    ComfyUICancelledError,
    ComfyUIClient,
    ComfyUIConnectionError,
    ComfyUIError,
    ComfyUIExecutionError,
    ComfyUIOOMError,
    ComfyUITimeoutError,
    ProgressEvent,
    PromptExecutionResult,
)
from comfyguard.broker.mutator import MutatedFrameGraph


class QueueFullError(ComfyUIError):
    """Raised when inbound job submission exceeds queue capacity and wait timeout."""


class WorkerConfig(BaseModel):
    """Configuration for a single ComfyUI worker instance."""

    name: str
    base_url: str
    max_concurrency: int = Field(default=1, ge=1)


class RetryPolicy(BaseModel):
    """Configurable exponential backoff policy for OOM and transient connection failures."""

    max_oom_retries: int = Field(default=2, ge=0)
    max_transient_retries: int = Field(default=1, ge=0)
    base_backoff_seconds: float = Field(default=1.0, ge=0.0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)
    max_backoff_seconds: float = Field(default=30.0, ge=0.0)

    def delay_for_attempt(self, attempt: int) -> float:
        """Compute bounded exponential backoff delay for retry index (1-based)."""
        if self.base_backoff_seconds <= 0:
            return 0.0
        raw = self.base_backoff_seconds * (self.backoff_multiplier ** max(attempt - 1, 0))
        return min(raw, self.max_backoff_seconds)


class JobStatus(StrEnum):
    """Lifecycle states for a pooled ComfyUI execution task."""

    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class BrokerJobSnapshot(BaseModel):
    """Immutable view of a queued, active, or finished broker task."""

    task_id: str
    frame: int | None = None
    status: JobStatus
    attempts: int = 0
    oom_retries: int = 0
    transient_retries: int = 0
    assigned_worker: str | None = None
    prompt_id: str | None = None
    error: str | None = None
    result: PromptExecutionResult | None = None


@dataclass
class _WorkerSlot:
    worker: WorkerConfig
    slot_index: int
    client: ComfyUIClient


@dataclass
class _PoolItem:
    task_id: str
    graph: dict[str, Any]
    frame: int | None
    timeout: float
    on_progress: Callable[[ProgressEvent], Any] | None
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    oom_retries: int = 0
    transient_retries: int = 0
    assigned_worker: str | None = None
    prompt_id: str | None = None
    error: str | None = None
    result: PromptExecutionResult | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)

    def snapshot(self) -> BrokerJobSnapshot:
        return BrokerJobSnapshot(
            task_id=self.task_id,
            frame=self.frame,
            status=self.status,
            attempts=self.attempts,
            oom_retries=self.oom_retries,
            transient_retries=self.transient_retries,
            assigned_worker=self.assigned_worker,
            prompt_id=self.prompt_id,
            error=self.error,
            result=self.result,
        )


def _normalize_workers(workers: Sequence[WorkerConfig | str]) -> list[WorkerConfig]:
    if not workers:
        raise ValueError("WorkerPool requires at least one worker endpoint")
    normalized: list[WorkerConfig] = []
    for idx, entry in enumerate(workers):
        if isinstance(entry, str):
            normalized.append(WorkerConfig(name=f"worker-{idx}", base_url=entry))
        else:
            normalized.append(entry)
    return normalized


class WorkerPool:
    """Manages 1..N ComfyUI workers with bounded backpressure, OOM retries, and timeouts."""

    def __init__(
        self,
        workers: Sequence[WorkerConfig | str],
        *,
        max_queue_size: int = 32,
        default_timeout: float = 300.0,
        retry_policy: RetryPolicy | None = None,
        client_factory: Callable[[WorkerConfig], ComfyUIClient] | None = None,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError(f"max_queue_size must be >= 1, got {max_queue_size}")
        self._workers = _normalize_workers(workers)
        self.max_queue_size = max_queue_size
        self.default_timeout = default_timeout
        self.retry_policy = retry_policy or RetryPolicy()
        self._client_factory = client_factory or (
            lambda cfg: ComfyUIClient(base_url=cfg.base_url)
        )

        self._slots: list[_WorkerSlot] = []
        self._available_slots: list[_WorkerSlot] = []
        self._pending_deque: deque[str] = deque()
        self._retry_deque: deque[tuple[_PoolItem, str | None]] = deque()
        self._cond = asyncio.Condition()
        self._items: dict[str, _PoolItem] = {}
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._running = False

    async def __aenter__(self) -> WorkerPool:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def start(self) -> None:
        """Initialize worker clients and start the background dispatcher."""
        if self._running:
            return
        self._slots = []
        self._available_slots = []
        self._pending_deque.clear()
        self._retry_deque.clear()
        for worker in self._workers:
            for slot_idx in range(worker.max_concurrency):
                slot = _WorkerSlot(
                    worker=worker,
                    slot_index=slot_idx,
                    client=self._client_factory(worker),
                )
                self._slots.append(slot)
                self._available_slots.append(slot)
        self._running = True
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        """Cancel active jobs, stop the dispatcher, and close all worker clients."""
        async with self._cond:
            self._running = False
            for item in self._items.values():
                if not item.done_event.is_set():
                    item.cancel_event.set()
            self._cond.notify_all()

        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            await asyncio.gather(self._dispatcher_task, return_exceptions=True)
            self._dispatcher_task = None

        if self._active_tasks:
            for task in list(self._active_tasks):
                task.cancel()
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
            self._active_tasks.clear()

        async with self._cond:
            self._pending_deque.clear()
            self._retry_deque.clear()
            for item in self._items.values():
                if not item.done_event.is_set():
                    item.status = JobStatus.CANCELLED
                    item.error = item.error or "Pool stopped"
                    item.done_event.set()

        for slot in self._slots:
            await slot.client.close()
        self._slots.clear()
        self._available_slots.clear()

    async def submit(
        self,
        graph: dict[str, Any],
        *,
        frame: int | None = None,
        timeout: float | None = None,
        on_progress: Callable[[ProgressEvent], Any] | None = None,
        queue_timeout: float | None = None,
    ) -> str:
        """Enqueue a graph for execution, applying backpressure when the queue is full."""
        async with self._cond:
            if not self._running:
                raise RuntimeError("WorkerPool must be started before submitting jobs")

            try:
                wait_coro = self._cond.wait_for(
                    lambda: (
                        not self._running
                        or len(self._pending_deque) < self.max_queue_size
                    )
                )
                if queue_timeout is not None:
                    await asyncio.wait_for(wait_coro, timeout=queue_timeout)
                else:
                    await wait_coro
            except TimeoutError as exc:
                raise QueueFullError(
                    f"WorkerPool queue full (max_queue_size={self.max_queue_size}) "
                    f"after waiting {queue_timeout}s"
                ) from exc

            if not self._running:
                raise RuntimeError("WorkerPool stopped while waiting to enqueue job")

            task_id = f"task_{uuid4().hex[:12]}"
            item = _PoolItem(
                task_id=task_id,
                graph=graph,
                frame=frame,
                timeout=timeout if timeout is not None else self.default_timeout,
                on_progress=on_progress,
            )
            self._items[task_id] = item
            self._pending_deque.append(task_id)
            self._cond.notify_all()
            return task_id

    async def wait(self, task_id: str) -> BrokerJobSnapshot:
        """Wait for a submitted task to reach a terminal state and return its snapshot."""
        item = self._items.get(task_id)
        if item is None:
            raise KeyError(f"Unknown task_id '{task_id}'")
        await item.done_event.wait()
        return item.snapshot()

    async def submit_and_wait(
        self,
        graph: dict[str, Any],
        *,
        frame: int | None = None,
        timeout: float | None = None,
        on_progress: Callable[[ProgressEvent], Any] | None = None,
        queue_timeout: float | None = None,
    ) -> BrokerJobSnapshot:
        """Submit a graph and wait until completion, cancellation, or failure."""
        task_id = await self.submit(
            graph,
            frame=frame,
            timeout=timeout,
            on_progress=on_progress,
            queue_timeout=queue_timeout,
        )
        return await self.wait(task_id)

    async def submit_batch(
        self,
        mutated_frames: Sequence[MutatedFrameGraph],
        *,
        timeout: float | None = None,
        on_progress: Callable[[ProgressEvent], Any] | None = None,
    ) -> list[BrokerJobSnapshot]:
        """Submit a sequence of mutated frame graphs and return ordered results."""
        task_ids: list[str] = []
        for mutated in mutated_frames:
            task_id = await self.submit(
                mutated.graph,
                frame=mutated.frame,
                timeout=timeout,
                on_progress=on_progress,
            )
            task_ids.append(task_id)
        return list(await asyncio.gather(*(self.wait(tid) for tid in task_ids)))

    async def cancel(self, task_id: str) -> bool:
        """Cancel a queued or running task and trigger /interrupt on its active worker."""
        async with self._cond:
            item = self._items.get(task_id)
            if item is None or item.done_event.is_set():
                return False
            item.cancel_event.set()
            if item.status == JobStatus.QUEUED:
                with contextlib.suppress(ValueError):
                    self._pending_deque.remove(task_id)
                item.status = JobStatus.CANCELLED
                item.error = "Cancelled while queued"
                item.done_event.set()
            self._cond.notify_all()
            return True

    def get_status(self, task_id: str) -> BrokerJobSnapshot:
        """Return a point-in-time snapshot for a known task_id."""
        item = self._items.get(task_id)
        if item is None:
            raise KeyError(f"Unknown task_id '{task_id}'")
        return item.snapshot()

    def pool_status(self) -> dict[str, Any]:
        """Return aggregate worker availability and job counts."""
        counts = {status.value: 0 for status in JobStatus}
        for item in self._items.values():
            counts[item.status.value] += 1
        return {
            "running": self._running,
            "workers": len(self._workers),
            "total_slots": len(self._slots),
            "available_slots": len(self._available_slots),
            "queued_depth": len(self._pending_deque),
            "retry_depth": len(self._retry_deque),
            "max_queue_size": self.max_queue_size,
            "jobs": counts,
        }

    def _pop_slot(self, avoid_worker: str | None = None) -> _WorkerSlot:
        if avoid_worker is not None and len(self._workers) > 1:
            for idx, candidate in enumerate(self._available_slots):
                if candidate.worker.name != avoid_worker:
                    return self._available_slots.pop(idx)
        return self._available_slots.pop(0)

    async def _dispatch_loop(self) -> None:
        while True:
            async with self._cond:
                await self._cond.wait_for(
                    lambda: (
                        not self._running
                        or (
                            bool(self._available_slots)
                            and (bool(self._retry_deque) or bool(self._pending_deque))
                        )
                    )
                )
                if not self._running:
                    break

                if self._retry_deque:
                    item, avoid_worker = self._retry_deque.popleft()
                    if item.cancel_event.is_set():
                        if not item.done_event.is_set():
                            item.status = JobStatus.CANCELLED
                            item.error = item.error or "Cancelled before retry"
                            item.done_event.set()
                        continue
                    slot = self._pop_slot(avoid_worker=avoid_worker)
                else:
                    task_id = self._pending_deque.popleft()
                    self._cond.notify_all()
                    item = self._items.get(task_id)
                    if item is None or item.cancel_event.is_set():
                        if item is not None and not item.done_event.is_set():
                            item.status = JobStatus.CANCELLED
                            item.error = item.error or "Cancelled while queued"
                            item.done_event.set()
                        continue
                    slot = self._pop_slot()

            task = asyncio.create_task(self._run_attempt(item, slot))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

    async def _sleep_or_cancel(self, delay: float, cancel_event: asyncio.Event) -> bool:
        if delay <= 0:
            return cancel_event.is_set()
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=delay)
            return True
        except TimeoutError:
            return cancel_event.is_set()

    async def _schedule_retry(
        self,
        item: _PoolItem,
        slot: _WorkerSlot,
        *,
        reason: str,
        attempt_num: int,
        release_slot: Callable[[], Awaitable[None]],
    ) -> bool:
        item.status = JobStatus.RETRYING
        item.error = reason
        await release_slot()
        delay = self.retry_policy.delay_for_attempt(attempt_num)
        if await self._sleep_or_cancel(delay, item.cancel_event):
            item.status = JobStatus.CANCELLED
            item.error = "Cancelled during retry backoff"
            return False
        async with self._cond:
            if self._running:
                self._retry_deque.append((item, slot.worker.name))
                self._cond.notify_all()
                return True
            item.status = JobStatus.CANCELLED
            item.error = "Pool stopped during retry"
            return False

    async def _run_attempt(self, item: _PoolItem, slot: _WorkerSlot) -> None:
        slot_released = False
        should_mark_done = True

        async def release_slot() -> None:
            nonlocal slot_released
            if not slot_released:
                slot_released = True
                async with self._cond:
                    self._available_slots.append(slot)
                    self._cond.notify_all()

        try:
            if item.cancel_event.is_set():
                item.status = JobStatus.CANCELLED
                item.error = item.error or "Cancelled"
                return

            item.attempts += 1
            item.status = JobStatus.RUNNING
            item.assigned_worker = slot.worker.name

            try:
                result = await slot.client.execute_prompt(
                    item.graph,
                    timeout=item.timeout,
                    on_progress=item.on_progress,
                    cancel_event=item.cancel_event,
                )
                item.prompt_id = result.prompt_id
                item.result = result
                item.error = None
                item.status = JobStatus.COMPLETED
            except ComfyUICancelledError as exc:
                item.status = JobStatus.CANCELLED
                item.error = str(exc)
            except ComfyUIOOMError as exc:
                item.prompt_id = exc.prompt_id or item.prompt_id
                if item.oom_retries < self.retry_policy.max_oom_retries:
                    item.oom_retries += 1
                    requeued = await self._schedule_retry(
                        item,
                        slot,
                        reason=f"CUDA OOM on {slot.worker.name}, retrying: {exc}",
                        attempt_num=item.oom_retries,
                        release_slot=release_slot,
                    )
                    if requeued:
                        should_mark_done = False
                else:
                    item.status = JobStatus.FAILED
                    item.error = (
                        f"Exhausted {self.retry_policy.max_oom_retries} OOM retries: {exc}"
                    )
            except ComfyUIConnectionError as exc:
                if item.transient_retries < self.retry_policy.max_transient_retries:
                    item.transient_retries += 1
                    requeued = await self._schedule_retry(
                        item,
                        slot,
                        reason=f"Connection error on {slot.worker.name}, retrying: {exc}",
                        attempt_num=item.transient_retries,
                        release_slot=release_slot,
                    )
                    if requeued:
                        should_mark_done = False
                else:
                    item.status = JobStatus.FAILED
                    item.error = str(exc)
            except (ComfyUITimeoutError, ComfyUIExecutionError) as exc:
                if isinstance(exc, ComfyUIExecutionError) and exc.prompt_id:
                    item.prompt_id = exc.prompt_id
                item.status = JobStatus.FAILED
                item.error = str(exc)
        except asyncio.CancelledError:
            item.status = JobStatus.CANCELLED
            item.error = "Pool shutdown cancelled job"
            raise
        finally:
            await release_slot()
            if should_mark_done and not item.done_event.is_set():
                item.done_event.set()
