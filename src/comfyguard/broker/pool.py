"""Multi-worker ComfyUI queue, OOM retry, and timeout pool (Phase 2)."""

from __future__ import annotations


class WorkerPool:
    """Manages 1..N ComfyUI worker endpoints with backpressure and OOM retry."""

    def __init__(self, endpoints: list[str] | None = None) -> None:
        self.endpoints = endpoints or []

    async def submit_and_wait(self) -> None:
        raise NotImplementedError("Implemented in Phase 2")
