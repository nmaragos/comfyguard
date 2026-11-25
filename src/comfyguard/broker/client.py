"""Async ComfyUI WebSocket and HTTP client (Phase 2)."""

from __future__ import annotations


class ComfyUIClient:
    """Async client for ComfyUI /prompt, /history, /object_info, and WebSocket progress."""

    def __init__(self, base_url: str = "http://127.0.0.1:8188") -> None:
        self.base_url = base_url.rstrip("/")

    async def get_object_info(self) -> dict[str, object]:
        raise NotImplementedError("Implemented in Phase 2")
