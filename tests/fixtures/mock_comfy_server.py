"""In-process mock HTTP + WebSocket ComfyUI server for Phase 2 integration tests."""

from __future__ import annotations

import asyncio
import io
import json
from collections import deque
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import web
from PIL import Image

MOCK_OBJECT_INFO_PATH = Path(__file__).resolve().parent / "mock_object_info.json"


def _make_png_bytes(width: int = 64, height: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(32, 128, 224)).save(buf, format="PNG")
    return buf.getvalue()


class MockComfyUIServer:
    """Scriptable aiohttp HTTP/WebSocket mock implementing ComfyUI's execution API."""

    def __init__(
        self,
        *,
        behaviors: list[str] | None = None,
        default_behavior: str = "success",
        step_delay: float = 0.005,
    ) -> None:
        self.behaviors: deque[str] = deque(behaviors or [])
        self.default_behavior = default_behavior
        self.step_delay = step_delay

        self.queued_prompts: list[dict[str, Any]] = []
        self.interrupt_calls: list[dict[str, Any]] = []
        self.uploaded_files: list[dict[str, Any]] = []
        self.history: dict[str, Any] = {}
        self.outputs_data: dict[str, bytes] = {}

        self._ws_clients: dict[str, web.WebSocketResponse] = {}
        self._interrupt_events: dict[str, asyncio.Event] = {}
        self._active_prompt_id: str | None = None
        self._sim_tasks: set[asyncio.Task[None]] = set()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port: int = 0

    @property
    def base_url(self) -> str:
        """Return the base HTTP URL for this running mock server."""
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> MockComfyUIServer:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start listening on an ephemeral loopback port."""
        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/object_info", self._handle_object_info)
        app.router.add_get("/queue", self._handle_queue)
        app.router.add_post("/prompt", self._handle_prompt)
        app.router.add_get("/history/{prompt_id}", self._handle_history)
        app.router.add_get("/view", self._handle_view)
        app.router.add_post("/interrupt", self._handle_interrupt)
        app.router.add_post("/upload/image", self._handle_upload_image)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        assert self._site._server is not None
        sockets = self._site._server.sockets
        self.port = int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        """Cancel running prompt simulations and shut down the mock server."""
        for task in list(self._sim_tasks):
            task.cancel()
        if self._sim_tasks:
            await asyncio.gather(*self._sim_tasks, return_exceptions=True)
            self._sim_tasks.clear()

        for ws in list(self._ws_clients.values()):
            if not ws.closed:
                await ws.close()
        self._ws_clients.clear()

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    def _next_behavior(self) -> str:
        if self.behaviors:
            return self.behaviors.popleft()
        return self.default_behavior

    async def _send_ws(self, client_id: str, msg_type: str, data: dict[str, Any]) -> None:
        ws = self._ws_clients.get(client_id)
        if ws is None or ws.closed:
            return
        await ws.send_json({"type": msg_type, "data": data})

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        client_id = request.query.get("clientId", "")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients[client_id] = ws
        try:
            async for _ in ws:
                pass
        finally:
            if self._ws_clients.get(client_id) is ws:
                self._ws_clients.pop(client_id, None)
        return ws

    async def _handle_object_info(self, _request: web.Request) -> web.Response:
        return web.json_response(json.loads(MOCK_OBJECT_INFO_PATH.read_text(encoding="utf-8")))

    async def _handle_queue(self, _request: web.Request) -> web.Response:
        running = [[0, self._active_prompt_id]] if self._active_prompt_id else []
        return web.json_response({"queue_running": running, "queue_pending": []})

    async def _handle_history(self, request: web.Request) -> web.Response:
        prompt_id = request.match_info["prompt_id"]
        if prompt_id in self.history:
            return web.json_response({prompt_id: self.history[prompt_id]})
        return web.json_response({})

    async def _handle_view(self, request: web.Request) -> web.Response:
        filename = request.query.get("filename", "")
        if filename not in self.outputs_data:
            return web.Response(status=404, text=f"Not found: {filename}")
        return web.Response(body=self.outputs_data[filename], content_type="image/png")

    async def _handle_interrupt(self, request: web.Request) -> web.Response:
        body: dict[str, Any] = {}
        if request.can_read_body:
            raw = await request.text()
            if raw:
                body = json.loads(raw)
        self.interrupt_calls.append(body)
        target_id = body.get("prompt_id") or self._active_prompt_id
        if target_id and target_id in self._interrupt_events:
            self._interrupt_events[target_id].set()
        return web.json_response({"ok": True})

    async def _handle_upload_image(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        filename = ""
        size = 0
        image_type = "input"
        subfolder = ""
        while part := await reader.next():
            if part.name == "image":
                filename = part.filename or "upload.png"
                payload = await part.read()
                size = len(payload)
            elif part.name == "type":
                image_type = await part.text()
            elif part.name == "subfolder":
                subfolder = await part.text()
        record = {
            "name": filename,
            "subfolder": subfolder,
            "type": image_type,
            "size": size,
        }
        self.uploaded_files.append(record)
        return web.json_response(
            {"name": filename, "subfolder": subfolder, "type": image_type}
        )

    async def _handle_prompt(self, request: web.Request) -> web.Response:
        payload = await request.json()
        client_id = str(payload.get("client_id", ""))
        graph = payload.get("prompt", {})
        behavior = self._next_behavior()

        if behavior == "http_400":
            return web.json_response(
                {
                    "error": {
                        "type": "prompt_outputs_failed_validation",
                        "message": "Required input missing on node 9",
                    }
                },
                status=400,
            )

        prompt_id = f"p_{uuid4().hex[:10]}"
        self.queued_prompts.append(
            {"prompt_id": prompt_id, "client_id": client_id, "graph": graph, "behavior": behavior}
        )
        interrupt_event = asyncio.Event()
        self._interrupt_events[prompt_id] = interrupt_event

        task = asyncio.create_task(
            self._simulate_prompt(client_id, prompt_id, graph, behavior, interrupt_event)
        )
        self._sim_tasks.add(task)
        task.add_done_callback(self._sim_tasks.discard)

        return web.json_response({"prompt_id": prompt_id, "number": len(self.queued_prompts)})

    async def _simulate_prompt(
        self,
        client_id: str,
        prompt_id: str,
        graph: dict[str, Any],
        behavior: str,
        interrupt_event: asyncio.Event,
    ) -> None:
        self._active_prompt_id = prompt_id
        try:
            await asyncio.sleep(0.005)
            await self._send_ws(client_id, "execution_start", {"prompt_id": prompt_id})
            await self._send_ws(
                client_id, "executing", {"prompt_id": prompt_id, "node": "9"}
            )

            if behavior == "oom":
                await self._send_ws(
                    client_id,
                    "execution_error",
                    {
                        "prompt_id": prompt_id,
                        "node_id": "9",
                        "node_type": "KSampler",
                        "exception_type": "torch.cuda.OutOfMemoryError",
                        "exception_message": "CUDA out of memory. Tried to allocate 2.00 GiB",
                        "traceback": ["File 'nodes.py', line 100, in sample\n"],
                    },
                )
                return

            if behavior == "fatal":
                await self._send_ws(
                    client_id,
                    "execution_error",
                    {
                        "prompt_id": prompt_id,
                        "node_id": "8",
                        "node_type": "VAEEncode",
                        "exception_type": "ValueError",
                        "exception_message": "Invalid latent tensor dimensions",
                        "traceback": ["File 'vae.py', line 42, in encode\n"],
                    },
                )
                return

            steps = 40 if behavior == "slow" else 2
            step_sleep = 0.05 if behavior == "slow" else self.step_delay
            for step in range(1, steps + 1):
                if interrupt_event.is_set():
                    await self._send_ws(
                        client_id,
                        "execution_interrupted",
                        {"prompt_id": prompt_id, "node_id": "9", "node_type": "KSampler"},
                    )
                    return
                await self._send_ws(
                    client_id,
                    "progress",
                    {"prompt_id": prompt_id, "node": "9", "value": step, "max": steps},
                )
                await asyncio.sleep(step_sleep)

            if interrupt_event.is_set():
                await self._send_ws(
                    client_id,
                    "execution_interrupted",
                    {"prompt_id": prompt_id, "node_id": "9", "node_type": "KSampler"},
                )
                return

            prefix = "comfyguard"
            if isinstance(graph, dict) and "11" in graph:
                inputs = graph["11"].get("inputs", {})
                if isinstance(inputs, dict) and "filename_prefix" in inputs:
                    prefix = str(inputs["filename_prefix"])
            filename = f"{prefix}_{prompt_id}.png"
            self.outputs_data[filename] = _make_png_bytes()

            node_output = {
                "images": [{"filename": filename, "subfolder": "", "type": "output"}]
            }
            self.history[prompt_id] = {
                "outputs": {"11": node_output},
                "status": {"completed": True},
            }
            await self._send_ws(
                client_id,
                "executed",
                {"prompt_id": prompt_id, "node": "11", "output": node_output},
            )
            await self._send_ws(
                client_id, "executing", {"prompt_id": prompt_id, "node": None}
            )
        finally:
            if self._active_prompt_id == prompt_id:
                self._active_prompt_id = None
