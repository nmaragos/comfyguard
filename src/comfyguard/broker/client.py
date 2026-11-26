"""Async HTTP and WebSocket client for ComfyUI."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import aiohttp
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

_OOM_MARKERS = (
    "out of memory",
    "outofmemoryerror",
    "cudnn_status_alloc_failed",
    "allocationondevice",
    "cuda error: out of memory",
)


class ComfyUIError(Exception):
    """Base exception for ComfyUI communication and execution errors."""


class ComfyUIConnectionError(ComfyUIError):
    """Raised when HTTP or WebSocket communication with ComfyUI fails."""


class ComfyUICancelledError(ComfyUIError):
    """Raised when a ComfyUI prompt execution is cancelled or interrupted."""


class ComfyUITimeoutError(ComfyUIError):
    """Raised when a ComfyUI prompt execution exceeds its configured timeout."""


class ComfyUIExecutionError(ComfyUIError):
    """Raised when ComfyUI rejects or fails during graph execution."""

    def __init__(
        self,
        message: str,
        *,
        prompt_id: str = "",
        node_id: str | None = None,
        node_type: str | None = None,
        exception_type: str | None = None,
        exception_message: str = "",
        traceback: list[str] | None = None,
        is_oom: bool = False,
    ) -> None:
        super().__init__(message)
        self.prompt_id = prompt_id
        self.node_id = node_id
        self.node_type = node_type
        self.exception_type = exception_type
        self.exception_message = exception_message
        self.traceback = traceback or []
        self.is_oom = is_oom


class ComfyUIOOMError(ComfyUIExecutionError):
    """Raised when a ComfyUI worker encounters a GPU out-of-memory error."""

    def __init__(
        self,
        message: str,
        *,
        prompt_id: str = "",
        node_id: str | None = None,
        node_type: str | None = None,
        exception_type: str | None = None,
        exception_message: str = "",
        traceback: list[str] | None = None,
    ) -> None:
        super().__init__(
            message,
            prompt_id=prompt_id,
            node_id=node_id,
            node_type=node_type,
            exception_type=exception_type,
            exception_message=exception_message,
            traceback=traceback,
            is_oom=True,
        )


def detect_cuda_oom(
    exception_type: str | None,
    exception_message: str,
    traceback: list[str] | None = None,
) -> bool:
    """Return True if exception metadata indicates a GPU/CUDA out-of-memory failure."""
    haystack = " ".join(
        [
            exception_type or "",
            exception_message or "",
            *(traceback or []),
        ]
    ).lower()
    return any(marker in haystack for marker in _OOM_MARKERS)


class ProgressEvent(BaseModel):
    """Structured WebSocket progress or execution lifecycle event from ComfyUI."""

    prompt_id: str
    event_type: Literal[
        "execution_start",
        "executing",
        "progress",
        "executed",
        "execution_cached",
        "execution_error",
        "execution_interrupted",
    ]
    node_id: str | None = None
    value: int | None = None
    max_value: int | None = None
    output: dict[str, Any] | None = None
    error_message: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class OutputArtifact(BaseModel):
    """Reference to an output file produced by a ComfyUI SaveImage/output node."""

    node_id: str
    filename: str
    subfolder: str = ""
    artifact_type: str = "output"


class PromptExecutionResult(BaseModel):
    """Final result returned after executing a ComfyUI prompt over WebSocket + HTTP."""

    prompt_id: str
    endpoint: str
    status: Literal["completed", "cancelled", "failed"]
    outputs: list[OutputArtifact] = Field(default_factory=list)
    history: dict[str, Any] = Field(default_factory=dict)
    events: list[ProgressEvent] = Field(default_factory=list)
    error_message: str | None = None
    elapsed_seconds: float = 0.0


def _build_ws_url(base_url: str, client_id: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    return urlunparse(
        (ws_scheme, parsed.netloc, f"{base_path}/ws", "", f"clientId={client_id}", "")
    )


def _extract_outputs_from_node_map(outputs_map: dict[str, Any]) -> list[OutputArtifact]:
    artifacts: list[OutputArtifact] = []
    for node_id, node_output in outputs_map.items():
        if not isinstance(node_output, dict):
            continue
        for key in ("images", "gifs", "videos", "files"):
            entries = node_output.get(key)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if isinstance(item, dict) and "filename" in item:
                    artifacts.append(
                        OutputArtifact(
                            node_id=str(node_id),
                            filename=str(item["filename"]),
                            subfolder=str(item.get("subfolder", "")),
                            artifact_type=str(item.get("type", "output")),
                        )
                    )
    return artifacts


class ComfyUIClient:
    """Async client for ComfyUI /prompt, /history, /object_info, /view, and /ws."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8188",
        *,
        client_id: str | None = None,
        request_timeout: float = 30.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id or uuid4().hex
        self.request_timeout = request_timeout
        self._external_session = session
        self._owned_session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> ComfyUIClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def ws_url(self) -> str:
        """Return the WebSocket endpoint URL for this client's clientId."""
        return _build_ws_url(self.base_url, self.client_id)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._external_session is not None:
            return self._external_session
        if self._owned_session is None or self._owned_session.closed:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            self._owned_session = aiohttp.ClientSession(timeout=timeout)
        return self._owned_session

    async def close(self) -> None:
        """Close the internally managed HTTP session if open."""
        if self._owned_session is not None and not self._owned_session.closed:
            await self._owned_session.close()
        self._owned_session = None

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"
        try:
            async with session.request(method, url, json=json_body) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise ComfyUIConnectionError(
                        f"HTTP {resp.status} from {method} {url}: {text[:500]}"
                    )
                if not text:
                    return {}
                return json.loads(text)
        except ComfyUIError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise ComfyUIConnectionError(f"Request {method} {url} failed: {exc}") from exc

    async def get_object_info(self) -> dict[str, Any]:
        """Fetch ComfyUI node schemas and available model lists from /object_info."""
        data = await self._request_json("GET", "/object_info")
        if not isinstance(data, dict):
            raise ComfyUIConnectionError("Expected JSON object from /object_info")
        return data

    async def get_queue_status(self) -> dict[str, Any]:
        """Fetch running and pending queue state from /queue."""
        data = await self._request_json("GET", "/queue")
        if not isinstance(data, dict):
            raise ComfyUIConnectionError("Expected JSON object from /queue")
        return data

    async def upload_image(
        self,
        image_path: str | Path,
        *,
        subfolder: str = "",
        overwrite: bool = True,
        image_type: str = "input",
    ) -> dict[str, Any]:
        """Upload a local image to ComfyUI's input directory via POST /upload/image."""
        src = Path(image_path)
        if not src.is_file():
            raise FileNotFoundError(f"Cannot upload missing image: {src}")

        session = await self._ensure_session()
        url = f"{self.base_url}/upload/image"
        form = aiohttp.FormData()
        form.add_field("image", src.read_bytes(), filename=src.name)
        form.add_field("overwrite", "true" if overwrite else "false")
        form.add_field("type", image_type)
        if subfolder:
            form.add_field("subfolder", subfolder)

        try:
            async with session.post(url, data=form) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise ComfyUIConnectionError(
                        f"HTTP {resp.status} uploading '{src.name}': {text[:500]}"
                    )
                return json.loads(text) if text else {}
        except ComfyUIError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise ComfyUIConnectionError(f"Upload to {url} failed: {exc}") from exc

    async def queue_prompt(
        self,
        graph: dict[str, Any],
        *,
        extra_data: dict[str, Any] | None = None,
    ) -> str:
        """Dispatch a mutated workflow graph to POST /prompt and return its prompt_id."""
        session = await self._ensure_session()
        url = f"{self.base_url}/prompt"
        payload: dict[str, Any] = {
            "prompt": graph,
            "client_id": self.client_id,
        }
        if extra_data:
            payload["extra_data"] = extra_data

        try:
            async with session.post(url, json=payload) as resp:
                text = await resp.text()
                data = json.loads(text) if text else {}
                if resp.status == 400:
                    err_info = data.get("error", {}) if isinstance(data, dict) else {}
                    err_msg = (
                        err_info.get("message")
                        if isinstance(err_info, dict)
                        else None
                    ) or text[:500]
                    raise ComfyUIExecutionError(
                        f"Prompt validation rejected (HTTP 400): {err_msg}",
                        exception_type=(
                            err_info.get("type") if isinstance(err_info, dict) else None
                        ),
                        exception_message=str(err_msg),
                    )
                if resp.status >= 400:
                    raise ComfyUIConnectionError(
                        f"HTTP {resp.status} from POST {url}: {text[:500]}"
                    )
                if not isinstance(data, dict) or "prompt_id" not in data:
                    raise ComfyUIConnectionError(f"Malformed /prompt response: {data!r}")
                return str(data["prompt_id"])
        except ComfyUIError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise ComfyUIConnectionError(f"POST {url} failed: {exc}") from exc

    async def get_history(self, prompt_id: str) -> dict[str, Any]:
        """Fetch execution history for a prompt_id from /history/<prompt_id>."""
        data = await self._request_json("GET", f"/history/{prompt_id}")
        if not isinstance(data, dict):
            raise ComfyUIConnectionError(f"Expected JSON object from /history/{prompt_id}")
        return data

    async def fetch_output_bytes(self, artifact: OutputArtifact) -> bytes:
        """Download raw bytes for a generated output artifact via GET /view."""
        session = await self._ensure_session()
        url = f"{self.base_url}/view"
        params = {
            "filename": artifact.filename,
            "subfolder": artifact.subfolder,
            "type": artifact.artifact_type,
        }
        try:
            async with session.get(url, params=params) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise ComfyUIConnectionError(
                        f"HTTP {resp.status} fetching '{artifact.filename}': {text[:300]}"
                    )
                return await resp.read()
        except ComfyUIError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            raise ComfyUIConnectionError(f"GET {url} failed: {exc}") from exc

    async def interrupt(self, prompt_id: str | None = None) -> None:
        """Request immediate cancellation of the active prompt via POST /interrupt."""
        body: dict[str, Any] = {"prompt_id": prompt_id} if prompt_id else {}
        await self._request_json("POST", "/interrupt", json_body=body)

    async def _safe_interrupt(self, prompt_id: str | None) -> None:
        with contextlib.suppress(ComfyUIError):
            await self.interrupt(prompt_id)

    async def execute_prompt(
        self,
        graph: dict[str, Any],
        *,
        timeout: float | None = None,
        on_progress: Callable[[ProgressEvent], Any] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> PromptExecutionResult:
        """Queue a workflow graph, stream WebSocket progress, and return /history outputs."""
        if cancel_event is not None and cancel_event.is_set():
            raise ComfyUICancelledError("Prompt execution cancelled before dispatch")

        started = time.monotonic()
        deadline = started + timeout if timeout is not None else None
        events: list[ProgressEvent] = []
        ws_outputs: dict[str, Any] = {}
        prompt_id: str | None = None

        try:
            async with ws_connect(self.ws_url) as ws:
                prompt_id = await self.queue_prompt(graph)

                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        await self._safe_interrupt(prompt_id)
                        raise ComfyUICancelledError(f"Prompt '{prompt_id}' cancelled")

                    remaining: float | None = None
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            await self._safe_interrupt(prompt_id)
                            raise ComfyUITimeoutError(
                                f"Prompt '{prompt_id}' timed out after {timeout}s"
                            )

                    raw_msg = await self._recv_ws_message(
                        ws,
                        prompt_id=prompt_id,
                        timeout=timeout,
                        remaining=remaining,
                        cancel_event=cancel_event,
                    )
                    if isinstance(raw_msg, bytes):
                        continue

                    done = await self._handle_ws_message(
                        raw_msg=raw_msg,
                        prompt_id=prompt_id,
                        events=events,
                        ws_outputs=ws_outputs,
                        on_progress=on_progress,
                    )
                    if done:
                        break
        except asyncio.CancelledError:
            if prompt_id is not None:
                await self._safe_interrupt(prompt_id)
            raise
        except ConnectionClosed as exc:
            raise ComfyUIConnectionError(
                f"WebSocket closed while monitoring prompt '{prompt_id}': {exc}"
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise ComfyUIConnectionError(
                f"WebSocket connection to {self.ws_url} failed: {exc}"
            ) from exc

        history = await self.get_history(prompt_id)
        entry = history.get(prompt_id, {})
        hist_outputs = entry.get("outputs", {}) if isinstance(entry, dict) else {}
        artifacts = _extract_outputs_from_node_map(
            hist_outputs if isinstance(hist_outputs, dict) and hist_outputs else ws_outputs
        )

        return PromptExecutionResult(
            prompt_id=prompt_id,
            endpoint=self.base_url,
            status="completed",
            outputs=artifacts,
            history=history,
            events=events,
            elapsed_seconds=round(time.monotonic() - started, 4),
        )

    async def _recv_ws_message(
        self,
        ws: Any,
        *,
        prompt_id: str,
        timeout: float | None,
        remaining: float | None,
        cancel_event: asyncio.Event | None,
    ) -> str | bytes:
        recv_task: asyncio.Task[str | bytes] = asyncio.create_task(ws.recv())
        wait_tasks: set[asyncio.Task[Any]] = {recv_task}
        cancel_task: asyncio.Task[bool] | None = None
        if cancel_event is not None:
            cancel_task = asyncio.create_task(cancel_event.wait())
            wait_tasks.add(cancel_task)

        try:
            done_tasks, pending_tasks = await asyncio.wait(
                wait_tasks,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for pending in pending_tasks:
                pending.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

            if not done_tasks:
                await self._safe_interrupt(prompt_id)
                raise ComfyUITimeoutError(
                    f"Prompt '{prompt_id}' timed out after {timeout}s"
                )

            if cancel_task is not None and cancel_task in done_tasks:
                await self._safe_interrupt(prompt_id)
                raise ComfyUICancelledError(f"Prompt '{prompt_id}' cancelled")

            return recv_task.result()
        except BaseException:
            for task in wait_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*wait_tasks, return_exceptions=True)
            raise

    async def _handle_ws_message(
        self,
        *,
        raw_msg: str,
        prompt_id: str,
        events: list[ProgressEvent],
        ws_outputs: dict[str, Any],
        on_progress: Callable[[ProgressEvent], Any] | None,
    ) -> bool:
        try:
            payload = json.loads(raw_msg)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False

        msg_type = payload.get("type")
        data = payload.get("data")
        if not isinstance(msg_type, str) or not isinstance(data, dict):
            return False

        msg_prompt_id = data.get("prompt_id")
        if msg_prompt_id is not None and str(msg_prompt_id) != prompt_id:
            return False

        supported_types = {
            "execution_start",
            "executing",
            "progress",
            "executed",
            "execution_cached",
            "execution_error",
            "execution_interrupted",
        }
        if msg_type not in supported_types:
            return False

        event = ProgressEvent(
            prompt_id=prompt_id,
            event_type=msg_type,  # type: ignore[arg-type]
            node_id=str(data["node"]) if data.get("node") is not None else None,
            value=data.get("value"),
            max_value=data.get("max"),
            output=data.get("output") if isinstance(data.get("output"), dict) else None,
            error_message=(
                str(data["exception_message"])
                if data.get("exception_message") is not None
                else None
            ),
            raw=payload,
        )
        events.append(event)
        if on_progress is not None:
            res = on_progress(event)
            if inspect.isawaitable(res):
                await res

        if msg_type == "executed" and event.node_id and event.output is not None:
            ws_outputs[event.node_id] = event.output
            return False

        if msg_type == "executing" and data.get("node") is None:
            return True

        if msg_type == "execution_interrupted":
            raise ComfyUICancelledError(f"Prompt '{prompt_id}' interrupted on worker")

        if msg_type == "execution_error":
            exc_type = (
                str(data["exception_type"]) if data.get("exception_type") is not None else None
            )
            exc_msg = str(data.get("exception_message", "ComfyUI execution error"))
            tb_raw = data.get("traceback", [])
            tb = [str(line) for line in tb_raw] if isinstance(tb_raw, list) else []
            node_id = str(data["node_id"]) if data.get("node_id") is not None else None
            node_type = str(data["node_type"]) if data.get("node_type") is not None else None
            formatted = (
                f"Prompt '{prompt_id}' failed at node {node_id or '?'} "
                f"({node_type or 'unknown'}): {exc_type or 'Error'}: {exc_msg}"
            )
            if detect_cuda_oom(exc_type, exc_msg, tb):
                raise ComfyUIOOMError(
                    formatted,
                    prompt_id=prompt_id,
                    node_id=node_id,
                    node_type=node_type,
                    exception_type=exc_type,
                    exception_message=exc_msg,
                    traceback=tb,
                )
            raise ComfyUIExecutionError(
                formatted,
                prompt_id=prompt_id,
                node_id=node_id,
                node_type=node_type,
                exception_type=exc_type,
                exception_message=exc_msg,
                traceback=tb,
                is_oom=False,
            )

        return False
