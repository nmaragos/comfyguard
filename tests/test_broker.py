"""Async integration tests for ComfyUIClient and WorkerPool (Verification Gate 2)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from comfyguard.broker.client import (
    ComfyUICancelledError,
    ComfyUIClient,
    ComfyUIExecutionError,
    ComfyUIOOMError,
    ComfyUITimeoutError,
    ProgressEvent,
    detect_cuda_oom,
)
from comfyguard.broker.mutator import GraphMutator
from comfyguard.broker.pool import (
    JobStatus,
    QueueFullError,
    RetryPolicy,
    WorkerConfig,
    WorkerPool,
)
from tests.fixtures.image_factory import write_png
from tests.fixtures.mock_comfy_server import MockComfyUIServer

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "workflows" / "lookdev_relight_v001"


def _sample_graph() -> dict[str, Any]:
    return json.loads((WORKFLOW_DIR / "workflow_api.json").read_text(encoding="utf-8"))


def test_detect_cuda_oom_patterns() -> None:
    assert detect_cuda_oom("torch.cuda.OutOfMemoryError", "CUDA out of memory") is True
    assert detect_cuda_oom("RuntimeError", "CUDNN_STATUS_ALLOC_FAILED on device") is True
    assert detect_cuda_oom("ValueError", "Invalid tensor shape") is False


async def test_client_object_info_and_upload_image(tmp_path: Path) -> None:
    img_path = write_png(tmp_path / "plate.1001.png", 64, 64)
    async with MockComfyUIServer() as server, ComfyUIClient(server.base_url) as client:
        info = await client.get_object_info()
        assert "CheckpointLoaderSimple" in info

        uploaded = await client.upload_image(img_path, subfolder="shots")
        assert uploaded["name"] == "plate.1001.png"
        assert uploaded["subfolder"] == "shots"
        assert server.uploaded_files[0]["size"] > 0


async def test_client_execute_prompt_success_and_fetch_output() -> None:
    events: list[ProgressEvent] = []
    async with MockComfyUIServer() as server, ComfyUIClient(server.base_url) as client:
        result = await client.execute_prompt(_sample_graph(), on_progress=events.append)

        assert result.status == "completed"
        assert len(result.outputs) == 1
        artifact = result.outputs[0]
        assert artifact.filename.startswith("comfyguard_relight_")

        raw_png = await client.fetch_output_bytes(artifact)
        assert raw_png.startswith(b"\x89PNG\r\n\x1a\n")
        assert any(e.event_type == "progress" and e.value == 2 for e in events)
        assert any(e.event_type == "executed" for e in events)


async def test_client_mid_run_cancellation_triggers_interrupt() -> None:
    progress_seen = asyncio.Event()
    cancel_event = asyncio.Event()

    def on_progress(evt: ProgressEvent) -> None:
        if evt.event_type == "progress":
            progress_seen.set()

    async with (
        MockComfyUIServer(default_behavior="slow") as server,
        ComfyUIClient(server.base_url) as client,
    ):
        task = asyncio.create_task(
            client.execute_prompt(
                _sample_graph(),
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
        )
        await asyncio.wait_for(progress_seen.wait(), timeout=2.0)
        cancel_event.set()

        with pytest.raises(ComfyUICancelledError):
            await task
        assert len(server.interrupt_calls) >= 1


async def test_client_timeout_triggers_interrupt() -> None:
    async with (
        MockComfyUIServer(default_behavior="slow") as server,
        ComfyUIClient(server.base_url) as client,
    ):
        with pytest.raises(ComfyUITimeoutError):
            await client.execute_prompt(_sample_graph(), timeout=0.12)
        assert len(server.interrupt_calls) >= 1


async def test_client_captures_oom_and_fatal_errors() -> None:
    async with (
        MockComfyUIServer(behaviors=["oom", "fatal", "http_400"]) as server,
        ComfyUIClient(server.base_url) as client,
    ):
        with pytest.raises(ComfyUIOOMError) as oom_exc:
            await client.execute_prompt(_sample_graph())
        assert oom_exc.value.is_oom is True
        assert oom_exc.value.node_id == "9"

        with pytest.raises(ComfyUIExecutionError) as fatal_exc:
            await client.execute_prompt(_sample_graph())
        assert fatal_exc.value.is_oom is False
        assert fatal_exc.value.node_type == "VAEEncode"

        with pytest.raises(ComfyUIExecutionError, match="HTTP 400"):
            await client.execute_prompt(_sample_graph())


async def test_pool_multi_worker_batch_execution() -> None:
    mutator = GraphMutator.from_workflow_dir(WORKFLOW_DIR)
    mutated_frames = [
        mutator.mutate_for_frame(
            frame=frame,
            base_seed=100,
            input_files={"beauty": f"beauty.{frame}.png", "depth": f"depth.{frame}.png"},
            output_prefix=f"sh010_relight_v001_{frame}",
        )
        for frame in (1001, 1002, 1003, 1004)
    ]

    async with MockComfyUIServer(step_delay=0.02) as s1, MockComfyUIServer(step_delay=0.02) as s2:
        workers = [
            WorkerConfig(name="gpu-0", base_url=s1.base_url),
            WorkerConfig(name="gpu-1", base_url=s2.base_url),
        ]
        async with WorkerPool(workers, max_queue_size=8) as pool:
            snapshots = await pool.submit_batch(mutated_frames)

        assert [snap.frame for snap in snapshots] == [1001, 1002, 1003, 1004]
        assert all(snap.status == JobStatus.COMPLETED for snap in snapshots)
        assert {snap.assigned_worker for snap in snapshots} == {"gpu-0", "gpu-1"}
        assert len(s1.queued_prompts) + len(s2.queued_prompts) == 4


async def test_pool_oom_retry_and_recovery() -> None:
    policy = RetryPolicy(max_oom_retries=2, base_backoff_seconds=0.01)
    async with (
        MockComfyUIServer(behaviors=["oom", "success"]) as server,
        WorkerPool([server.base_url], retry_policy=policy) as pool,
    ):
        snap = await pool.submit_and_wait(_sample_graph(), frame=1001)
        assert snap.status == JobStatus.COMPLETED
        assert snap.attempts == 2
        assert snap.oom_retries == 1
        assert snap.result is not None
        assert len(snap.result.outputs) == 1


async def test_pool_oom_exhaustion_fails() -> None:
    policy = RetryPolicy(max_oom_retries=2, base_backoff_seconds=0.01)
    async with (
        MockComfyUIServer(default_behavior="oom") as server,
        WorkerPool([server.base_url], retry_policy=policy) as pool,
    ):
        snap = await pool.submit_and_wait(_sample_graph(), frame=1001)
        assert snap.status == JobStatus.FAILED
        assert snap.attempts == 3
        assert snap.oom_retries == 2
        assert snap.error is not None and "Exhausted 2 OOM retries" in snap.error


async def test_pool_oom_failover_across_workers() -> None:
    policy = RetryPolicy(max_oom_retries=2, base_backoff_seconds=0.01)
    async with (
        MockComfyUIServer(default_behavior="oom") as bad_gpu,
        MockComfyUIServer(default_behavior="success") as good_gpu,
    ):
        workers = [
            WorkerConfig(name="gpu-bad", base_url=bad_gpu.base_url),
            WorkerConfig(name="gpu-good", base_url=good_gpu.base_url),
        ]
        async with WorkerPool(workers, retry_policy=policy) as pool:
            snap = await pool.submit_and_wait(_sample_graph(), frame=1001)

        assert snap.status == JobStatus.COMPLETED
        assert snap.assigned_worker == "gpu-good"
        assert snap.oom_retries == 1
        assert snap.attempts == 2


async def test_pool_fatal_error_not_retried() -> None:
    policy = RetryPolicy(max_oom_retries=2, base_backoff_seconds=0.01)
    async with (
        MockComfyUIServer(default_behavior="fatal") as server,
        WorkerPool([server.base_url], retry_policy=policy) as pool,
    ):
        snap = await pool.submit_and_wait(_sample_graph(), frame=1001)
        assert snap.status == JobStatus.FAILED
        assert snap.attempts == 1
        assert snap.oom_retries == 0


async def test_pool_mid_run_and_queued_cancellation() -> None:
    running_progress = asyncio.Event()

    def on_progress(evt: ProgressEvent) -> None:
        if evt.event_type == "progress":
            running_progress.set()

    async with (
        MockComfyUIServer(default_behavior="slow") as server,
        WorkerPool([server.base_url], max_queue_size=4) as pool,
    ):
        active_id = await pool.submit(_sample_graph(), frame=1001, on_progress=on_progress)
        await asyncio.wait_for(running_progress.wait(), timeout=2.0)

        queued_id = await pool.submit(_sample_graph(), frame=1002)
        assert await pool.cancel(queued_id) is True
        assert await pool.cancel(active_id) is True

        active_snap = await pool.wait(active_id)
        queued_snap = await pool.wait(queued_id)
        assert active_snap.status == JobStatus.CANCELLED
        assert queued_snap.status == JobStatus.CANCELLED
        assert len(server.interrupt_calls) >= 1


async def test_pool_timeout_enforced() -> None:
    async with (
        MockComfyUIServer(default_behavior="slow") as server,
        WorkerPool([server.base_url], default_timeout=0.12) as pool,
    ):
        snap = await pool.submit_and_wait(_sample_graph(), frame=1001)
        assert snap.status == JobStatus.FAILED
        assert snap.error is not None and "timed out" in snap.error.lower()
        assert len(server.interrupt_calls) >= 1


async def test_pool_backpressure_raises_queue_full() -> None:
    first_started = asyncio.Event()

    def on_progress(evt: ProgressEvent) -> None:
        if evt.event_type == "progress":
            first_started.set()

    async with (
        MockComfyUIServer(default_behavior="slow") as server,
        WorkerPool([server.base_url], max_queue_size=1) as pool,
    ):
        # Task 1 enters the worker slot, freeing the single pending queue slot.
        t1 = await pool.submit(_sample_graph(), frame=1001, on_progress=on_progress)
        await asyncio.wait_for(first_started.wait(), timeout=2.0)

        # Task 2 fills max_queue_size=1 because the worker is busy on Task 1.
        t2 = await pool.submit(_sample_graph(), frame=1002)
        status = pool.pool_status()
        assert status["queued_depth"] == 1

        # Task 3 must hit backpressure and raise QueueFullError.
        with pytest.raises(QueueFullError):
            await pool.submit(_sample_graph(), frame=1003, queue_timeout=0.05)

        await pool.cancel(t2)
        await pool.cancel(t1)
        await pool.wait(t1)
        await pool.wait(t2)
