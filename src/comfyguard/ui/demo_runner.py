"""Reusable self-contained 3-scenario demo runner powering CLI demo and Web UI."""

from __future__ import annotations

import asyncio
import copy
import io
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
from aiohttp import web
from PIL import Image

from comfyguard.broker.mutator import GraphMutator, graph_diff
from comfyguard.contracts.schema import WorkflowContract
from comfyguard.dcc.submitter import DCCContext, PipelineOrchestrator, build_job_request
from comfyguard.publish.exr_io import read_exr_attributes
from comfyguard.ui.report import (
    GraphDiffRecord,
    JobVisualRecord,
    ReportPayload,
    build_job_record_from_submission,
    export_report,
)
from comfyguard.validation.preflight import inspect_image_header


def resolve_default_workflow_dir() -> Path:
    """Resolve sample workflow directory via COMFYGUARD_WORKFLOW_ROOT or repo checkout."""
    env_root = os.environ.get("COMFYGUARD_WORKFLOW_ROOT", "").strip()
    if env_root:
        candidate = Path(env_root) / "lookdev_relight_v001"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parents[3] / "workflows" / "lookdev_relight_v001"


def render_plate_rgb(
    width: int,
    height: int,
    *,
    shift_x: int = 0,
    brightness_scale: float = 1.0,
    rim_boost: float = 0.0,
) -> np.ndarray:
    """Generate a synthetic CG lookdev silhouette plate with crisp edges."""
    img = np.full((height, width, 3), [0.08, 0.09, 0.12], dtype=np.float32)

    cx = width // 2 + shift_x
    cy = height // 2
    rx = width // 5
    ry = height // 4

    yy, xx = np.ogrid[:height, :width]
    ellipsoid = ((xx - cx) / max(rx, 1)) ** 2 + ((yy - cy) / max(ry, 1)) ** 2 <= 1.0
    img[ellipsoid] = np.array([0.45, 0.42, 0.38], dtype=np.float32) * brightness_scale

    px0, px1 = max(cx - rx, 0), min(cx + rx, width)
    py0, py1 = min(cy + ry // 2, height), min(cy + ry + height // 10, height)
    img[py0:py1, px0:px1] = np.array([0.28, 0.30, 0.34], dtype=np.float32) * brightness_scale

    if rim_boost > 0.0:
        fg_mask = ellipsoid.copy()
        fg_mask[py0:py1, px0:px1] = True
        warm_gain = 1.0 + np.array([0.28, 0.12, -0.06], dtype=np.float32) * rim_boost
        img[fg_mask] = np.clip(img[fg_mask] * warm_gain, 0.0, 1.0)

    return np.clip(img, 0.0, 1.0)


def save_png(path: Path, arr: np.ndarray) -> Path:
    """Write an RGB float32 [0..1] array to a PNG file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    u8 = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    Image.fromarray(u8, mode="RGB").save(path, format="PNG")
    return path


def png_bytes(arr: np.ndarray) -> bytes:
    """Encode an RGB float32 [0..1] array into PNG bytes."""
    buf = io.BytesIO()
    u8 = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    Image.fromarray(u8, mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


class DemoComfyServer:
    """Lightweight self-contained mock ComfyUI HTTP/WS worker for demos."""

    def __init__(
        self,
        *,
        object_info: dict[str, Any],
        render_fn: Callable[[dict[str, Any]], bytes],
    ) -> None:
        self.object_info = object_info
        self.render_fn = render_fn
        self.queued_count = 0
        self._ws_clients: dict[str, web.WebSocketResponse] = {}
        self._history: dict[str, Any] = {}
        self._outputs: dict[str, bytes] = {}
        self._runner: web.AppRunner | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> DemoComfyServer:
        app = web.Application()
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/object_info", self._get_object_info)
        app.router.add_post("/prompt", self._prompt)
        app.router.add_get("/history/{pid}", self._get_history)
        app.router.add_get("/view", self._view)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        self.port = int(site._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *_: object) -> None:
        for ws in list(self._ws_clients.values()):
            if not ws.closed:
                await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        cid = request.query.get("clientId", "")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients[cid] = ws
        try:
            async for _ in ws:
                pass
        finally:
            self._ws_clients.pop(cid, None)
        return ws

    async def _get_object_info(self, _request: web.Request) -> web.Response:
        return web.json_response(self.object_info)

    async def _get_history(self, request: web.Request) -> web.Response:
        pid = request.match_info["pid"]
        return web.json_response({pid: self._history[pid]} if pid in self._history else {})

    async def _view(self, request: web.Request) -> web.Response:
        fn = request.query.get("filename", "")
        return web.Response(body=self._outputs[fn], content_type="image/png")

    async def _prompt(self, request: web.Request) -> web.Response:
        payload = await request.json()
        cid = str(payload.get("client_id", ""))
        graph = payload.get("prompt", {})
        pid = f"demo_{uuid4().hex[:8]}"
        self.queued_count += 1
        asyncio.create_task(self._run_prompt(cid, pid, graph))
        return web.json_response({"prompt_id": pid})

    async def _run_prompt(self, cid: str, pid: str, graph: dict[str, Any]) -> None:
        await asyncio.sleep(0.01)
        ws = self._ws_clients.get(cid)
        prefix = graph["11"]["inputs"]["filename_prefix"]
        filename = f"{prefix}_{pid}.png"
        self._outputs[filename] = self.render_fn(graph)
        node_out = {"images": [{"filename": filename, "subfolder": "", "type": "output"}]}
        self._history[pid] = {"outputs": {"11": node_out}}
        if ws and not ws.closed:
            await ws.send_json({"type": "execution_start", "data": {"prompt_id": pid}})
            await ws.send_json(
                {"type": "progress", "data": {"prompt_id": pid, "node": "9", "value": 1, "max": 1}}
            )
            await ws.send_json(
                {"type": "executed", "data": {"prompt_id": pid, "node": "11", "output": node_out}}
            )
            await ws.send_json({"type": "executing", "data": {"prompt_id": pid, "node": None}})


def demo_object_info() -> dict[str, Any]:
    """Minimal ComfyUI /object_info matching workflows/lookdev_relight_v001."""
    return {
        "CheckpointLoaderSimple": {
            "input": {"required": {"ckpt_name": [["sdxl_base_1.0.safetensors"]]}}
        },
        "ControlNetLoader": {
            "input": {
                "required": {"control_net_name": [["diffusers_xl_depth_full.safetensors"]]}
            }
        },
    }


async def run_portfolio_scenarios(
    output_dir: Path | None = None,
    *,
    scenarios: Sequence[str] = ("1", "2", "3"),
    report_path: Path | None = None,
    workflow_dir: Path | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Run selected portfolio scenarios and build visual records + optional HTML report."""
    wf_dir = workflow_dir or resolve_default_workflow_dir()
    tmp_ctx: tempfile.TemporaryDirectory[str] | None = None
    if output_dir is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="comfyguard_demo_")
        workspace = Path(tmp_ctx.name)
    else:
        workspace = output_dir
        workspace.mkdir(parents=True, exist_ok=True)

    try:
        contract = WorkflowContract.from_yaml(wf_dir / "contract.yaml")
        model_hashes = {
            m.filename: m.sha256 for m in contract.required_models if m.sha256 is not None
        }
        obj_info = demo_object_info()
        orchestrator = PipelineOrchestrator()
        dcc_ctx = DCCContext(
            dcc_app="houdini",
            scene_file="sq010_sh0020_lookdev_v003.hip",
            artist="td_demo",
            rez_packages=["comfyguard-0.1.0", "ocio_configs-2.3.0"],
        )

        results: dict[str, Any] = {}
        visual_jobs: list[JobVisualRecord] = []
        contract_label = f"{contract.name}:{contract.version}"

        if "1" in scenarios:
            s1_plates = workspace / "plates_1080"
            save_png(s1_plates / "beauty.1001.png", render_plate_rgb(1920, 1080))
            save_png(s1_plates / "depth.1001.png", render_plate_rgb(1920, 1080))
            job1 = build_job_request(
                show="NOVA",
                sequence="sq010",
                shot="sh0010",
                pass_name="relight",
                version=1,
                workflow_dir=wf_dir,
                start_frame=1001,
                end_frame=1001,
                input_sequences={
                    "beauty": str(s1_plates / "beauty.####.png"),
                    "depth": str(s1_plates / "depth.####.png"),
                },
                output_dir=workspace / "publish",
                dcc_context=dcc_ctx,
            )
            async with DemoComfyServer(
                object_info=obj_info,
                render_fn=lambda _g: png_bytes(render_plate_rgb(1920, 1080)),
            ) as srv1:
                res1 = await orchestrator.run_job(
                    job1,
                    endpoints=[srv1.base_url],
                    object_info=obj_info,
                    model_hashes=model_hashes,
                )
                assert res1.passed is False
                assert res1.preflight_report.passed is False
                assert srv1.queued_count == 0, "Pre-flight failure must prevent GPU dispatch"
                dispatched_1 = srv1.queued_count

            results["scenario_1"] = res1
            visual_jobs.append(
                build_job_record_from_submission(
                    res1,
                    job1,
                    title="Unaligned 1920x1080 Pre-Flight Block",
                    scenario_tag="SCENARIO 1",
                    gpu_prompts_dispatched=dispatched_1,
                    contract_label=contract_label,
                )
            )
            if verbose:
                print("[Scenario 1] Pre-Flight Rejected 1920x1080 (0 GPU prompts dispatched):")
                print(f"  {res1.preflight_report.summary()}")
                for chk in res1.preflight_report.failed_checks:
                    print(f"  - {chk.check_name}: {chk.message}")

        if "2" in scenarios:
            s2_plates = workspace / "plates_drift"
            save_png(s2_plates / "beauty.1001.png", render_plate_rgb(1920, 1088))
            save_png(s2_plates / "depth.1001.png", render_plate_rgb(1920, 1088))
            job2 = build_job_request(
                show="NOVA",
                sequence="sq010",
                shot="sh0020",
                pass_name="relight",
                version=1,
                workflow_dir=wf_dir,
                start_frame=1001,
                end_frame=1001,
                input_sequences={
                    "beauty": str(s2_plates / "beauty.####.png"),
                    "depth": str(s2_plates / "depth.####.png"),
                },
                output_dir=workspace / "publish",
                dcc_context=dcc_ctx,
            )
            drifted_bytes = png_bytes(render_plate_rgb(1920, 1088, shift_x=180, rim_boost=1.0))
            async with DemoComfyServer(
                object_info=obj_info, render_fn=lambda _g: drifted_bytes
            ) as srv2:
                res2 = await orchestrator.run_job(
                    job2,
                    endpoints=[srv2.base_url],
                    object_info=obj_info,
                    model_hashes=model_hashes,
                )
                assert res2.passed is False
                assert res2.preflight_report.passed is True
                assert res2.postflight_report is not None and res2.postflight_report.passed is False
                assert res2.publish_result is not None
                assert len(res2.publish_result.published_files) == 0
                assert 1001 in res2.publish_result.quarantined_files
                dispatched_2 = srv2.queued_count

            results["scenario_2"] = res2
            visual_jobs.append(
                build_job_record_from_submission(
                    res2,
                    job2,
                    title="High Edge-Drift Frame Quarantine",
                    scenario_tag="SCENARIO 2",
                    gpu_prompts_dispatched=dispatched_2,
                    contract_label=contract_label,
                )
            )
            if verbose:
                print("\n[Scenario 2] Post-Flight Quarantined High Edge-Drift Frame:")
                print(f"  {res2.postflight_report.summary()}")
                print(f"  Quarantine Dir: {res2.publish_result.quarantine_dir}")

        if "3" in scenarios:
            s3_plates = workspace / "plates_clean"
            for idx, frame in enumerate((1001, 1002, 1003)):
                scale = 1.0 + idx * 0.02
                save_png(
                    s3_plates / f"beauty.{frame:04d}.png",
                    render_plate_rgb(1920, 1088, brightness_scale=scale),
                )
                save_png(
                    s3_plates / f"depth.{frame:04d}.png",
                    render_plate_rgb(1920, 1088, brightness_scale=scale),
                )

            job3 = build_job_request(
                show="NOVA",
                sequence="sq010",
                shot="sh0030",
                pass_name="relight",
                version=1,
                workflow_dir=wf_dir,
                start_frame=1001,
                end_frame=1003,
                base_seed=1000,
                params={"steps": 28, "cfg": 7.0, "denoise": 0.45},
                input_sequences={
                    "beauty": str(s3_plates / "beauty.####.png"),
                    "depth": str(s3_plates / "depth.####.png"),
                },
                output_dir=workspace / "publish",
                dcc_context=dcc_ctx,
            )

            def render_clean_relight(graph: dict[str, Any]) -> bytes:
                seed = int(graph["9"]["inputs"]["seed"])
                frame_offset = (seed - 1000) - 1001
                scale = 1.0 + max(frame_offset, 0) * 0.02
                return png_bytes(
                    render_plate_rgb(1920, 1088, brightness_scale=scale, rim_boost=0.8)
                )

            async with (
                DemoComfyServer(object_info=obj_info, render_fn=render_clean_relight) as w0,
                DemoComfyServer(object_info=obj_info, render_fn=render_clean_relight) as w1,
            ):
                res3 = await orchestrator.run_job(
                    job3,
                    endpoints=[w0.base_url, w1.base_url],
                    object_info=obj_info,
                    model_hashes=model_hashes,
                    dcc_source=dcc_ctx.format_source(),
                )
                dispatched_3 = w0.queued_count + w1.queued_count

            assert res3.passed is True
            assert res3.publish_result is not None
            assert len(res3.publish_result.published_files) == 3
            assert len(res3.publish_result.quarantined_files) == 0

            sample_exr = res3.publish_result.published_files[1001]
            exr_info = inspect_image_header(sample_exr)
            exr_attrs = read_exr_attributes(sample_exr)
            assert (exr_info.width, exr_info.height) == (1920, 1088)
            assert exr_attrs["comfyguard:seed"] == "2001"
            assert exr_attrs["comfyguard:colorspace"] == "ACEScg"
            assert len(exr_attrs["comfyguard:graph_hash"]) == 64

            results["scenario_3"] = res3
            visual_jobs.append(
                build_job_record_from_submission(
                    res3,
                    job3,
                    title="Clean 1920x1088 Multi-Worker Publish",
                    scenario_tag="SCENARIO 3",
                    gpu_prompts_dispatched=dispatched_3,
                    contract_label=contract_label,
                )
            )
            if verbose:
                print("\n[Scenario 3] Clean 1920x1088 Multi-Worker Publish + EXR Provenance:")
                print(f"  Published Dir: {res3.publish_result.publish_dir}")
                print(f"  Manifest:      {res3.publish_result.manifest_path}")
                print(f"  Sample EXR:    {sample_exr.name} ({exr_info.width}x{exr_info.height})")
                print(
                    f"  EXR Stamps:    seed={exr_attrs['comfyguard:seed']} "
                    f"colorspace={exr_attrs['comfyguard:colorspace']} "
                    f"graph_hash={exr_attrs['comfyguard:graph_hash'][:12]}..."
                )

        mutator = GraphMutator.from_workflow_dir(wf_dir)
        v1_graph = mutator.template_graph
        v2_graph = copy.deepcopy(v1_graph)
        v2_graph["9"]["inputs"]["steps"] = 28
        v2_graph["9"]["inputs"]["cfg"] = 7.0
        diff = graph_diff(v1_graph, v2_graph)
        diff_record = GraphDiffRecord.from_graph_diff(diff, v1_label="v001", v2_label="v002")

        if verbose:
            print(f"\n{diff.format_human()}")

        if report_path is not None:
            payload = ReportPayload(jobs=visual_jobs, graph_diff=diff_record)
            written = export_report(payload, report_path)
            results["report_path"] = written
            if verbose:
                print(f"\n[Visual Report] Wrote static HTML report -> {written}")

        results["diff"] = diff
        results["diff_record"] = diff_record
        results["visual_jobs"] = visual_jobs
        return results
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()
