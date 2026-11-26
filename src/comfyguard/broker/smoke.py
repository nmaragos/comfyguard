"""Live ComfyUI worker probing and end-to-end img2img smoke-test runner."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from comfyguard.broker.client import ComfyUIClient, ComfyUIConnectionError
from comfyguard.contracts.schema import (
    AOVRequirement,
    ExposedParam,
    ParamType,
    PostflightThresholds,
    RequiredModel,
    ResolutionRule,
    WorkflowContract,
)

DEFAULT_LOCAL_ENDPOINTS: tuple[str, ...] = (
    "http://127.0.0.1:8188",
    "http://127.0.0.1:8000",
)

CORE_SMOKE_NODES: tuple[str, ...] = (
    "CheckpointLoaderSimple",
    "LoadImage",
    "CLIPTextEncode",
    "VAEEncode",
    "KSampler",
    "VAEDecode",
    "SaveImage",
)


class WorkerProbeResult(BaseModel):
    """Health, queue, node, and checkpoint inventory snapshot for a ComfyUI worker."""

    endpoint: str
    online: bool
    queue_running: int = 0
    queue_pending: int = 0
    registered_nodes: int = 0
    checkpoints: list[str] = Field(default_factory=list)
    samplers: list[str] = Field(default_factory=list)
    schedulers: list[str] = Field(default_factory=list)
    missing_core_nodes: list[str] = Field(default_factory=list)
    error: str | None = None


class SmokeTestResult(BaseModel):
    """Outcome of probing or running a live end-to-end smoke test against ComfyUI."""

    endpoint: str
    probe: WorkerProbeResult
    checkpoint: str | None = None
    check_only: bool = False
    passed: bool
    message: str
    submission: Any = None
    published_exr: Path | None = None
    manifest_path: Path | None = None
    exr_attributes: dict[str, str] = Field(default_factory=dict)


def _extract_combo_choices(node_meta: Any, input_name: str) -> list[str]:
    if not isinstance(node_meta, dict):
        return []
    input_section = node_meta.get("input", {})
    if not isinstance(input_section, dict):
        return []
    for group in ("required", "optional"):
        group_inputs = input_section.get(group, {})
        if not isinstance(group_inputs, dict) or input_name not in group_inputs:
            continue
        spec = group_inputs[input_name]
        if not isinstance(spec, list) or not spec:
            continue
        first = spec[0]
        if isinstance(first, list):
            return [str(item) for item in first]
        if first == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            options = spec[1].get("options")
            if isinstance(options, list):
                return [str(item) for item in options]
    return []


async def probe_worker(
    endpoint: str,
    *,
    request_timeout: float = 5.0,
) -> WorkerProbeResult:
    """Query /queue and /object_info to verify worker readiness and installed checkpoints."""
    clean_url = endpoint.rstrip("/")
    try:
        async with ComfyUIClient(clean_url, request_timeout=request_timeout) as client:
            queue = await client.get_queue_status()
            obj_info = await client.get_object_info()
    except ComfyUIConnectionError as exc:
        return WorkerProbeResult(endpoint=clean_url, online=False, error=str(exc))

    running = queue.get("queue_running", [])
    pending = queue.get("queue_pending", [])
    checkpoints = _extract_combo_choices(obj_info.get("CheckpointLoaderSimple"), "ckpt_name")
    samplers = _extract_combo_choices(obj_info.get("KSampler"), "sampler_name")
    schedulers = _extract_combo_choices(obj_info.get("KSampler"), "scheduler")
    missing_nodes = [node for node in CORE_SMOKE_NODES if node not in obj_info]

    return WorkerProbeResult(
        endpoint=clean_url,
        online=True,
        queue_running=len(running) if isinstance(running, list) else 0,
        queue_pending=len(pending) if isinstance(pending, list) else 0,
        registered_nodes=len(obj_info),
        checkpoints=checkpoints,
        samplers=samplers,
        schedulers=schedulers,
        missing_core_nodes=missing_nodes,
    )


async def discover_live_endpoint(
    endpoints: Sequence[str] = DEFAULT_LOCAL_ENDPOINTS,
    *,
    request_timeout: float = 3.0,
) -> WorkerProbeResult | None:
    """Probe candidate endpoints in order and return the first online worker."""
    for ep in endpoints:
        probe = await probe_worker(ep, request_timeout=request_timeout)
        if probe.online:
            return probe
    return None


def _pick_sampler(available: Sequence[str]) -> str:
    for preferred in ("dpmpp_2m", "euler", "euler_ancestral"):
        if preferred in available:
            return preferred
    return str(available[0]) if available else "euler"


def _pick_scheduler(available: Sequence[str]) -> str:
    for preferred in ("karras", "normal", "simple"):
        if preferred in available:
            return preferred
    return str(available[0]) if available else "normal"


def build_smoke_workflow(
    workflow_dir: Path,
    *,
    checkpoint: str,
    width: int = 512,
    height: int = 512,
    steps: int = 4,
    denoise: float = 0.15,
    sampler_name: str = "euler",
    scheduler: str = "normal",
    min_edge_iou: float = 0.25,
) -> tuple[Path, WorkflowContract]:
    """Write a self-contained single-checkpoint img2img workflow + contract for smoke tests."""
    workflow_dir.mkdir(parents=True, exist_ok=True)
    graph: dict[str, Any] = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "3": {
            "class_type": "LoadImage",
            "inputs": {"image": "beauty.1001.png"},
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["1", 1],
                "text": "studio lookdev relight, crisp geometry, neutral exposure",
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["1", 1],
                "text": "watermark, text, blurry, artifacts",
            },
        },
        "8": {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["3", 0], "vae": ["1", 2]},
        },
        "9": {
            "class_type": "KSampler",
            "inputs": {
                "cfg": 5.0,
                "denoise": float(denoise),
                "latent_image": ["8", 0],
                "model": ["1", 0],
                "negative": ["6", 0],
                "positive": ["5", 0],
                "sampler_name": sampler_name,
                "scheduler": scheduler,
                "seed": 42,
                "steps": int(steps),
            },
        },
        "10": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["9", 0], "vae": ["1", 2]},
        },
        "11": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "comfyguard_smoke", "images": ["10", 0]},
        },
    }
    workflow_path = workflow_dir / "workflow_api.json"
    workflow_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")

    contract = WorkflowContract(
        name="smoke_test",
        version="v001",
        description="Self-contained ComfyGuard live worker smoke test",
        workflow_file="workflow_api.json",
        colorspace="ACEScg",
        resolution=ResolutionRule(
            multiple_of=32,
            min_width=min(64, width),
            max_width=max(4096, width),
            min_height=min(64, height),
            max_height=max(4096, height),
        ),
        required_models=[
            RequiredModel(
                node_id="1",
                node_class="CheckpointLoaderSimple",
                input_name="ckpt_name",
                filename=checkpoint,
                sha256=None,
            )
        ],
        required_aovs=[
            AOVRequirement(
                name="beauty",
                param_name="input_beauty",
                required=True,
                required_channels=["R", "G", "B"],
            )
        ],
        seed_param="seed",
        postflight=PostflightThresholds(
            min_edge_iou=min_edge_iou,
            max_temporal_delta=0.25,
            min_pixel_std=1e-4,
            allow_nan_or_inf=False,
            expected_channels=3,
        ),
        exposed_params={
            "seed": ExposedParam(
                node_id="9",
                input_name="seed",
                param_type=ParamType.INT,
                required=True,
                default=42,
                min_value=0,
                max_value=2147483647,
                per_frame=True,
                is_seed=True,
            ),
            "steps": ExposedParam(
                node_id="9",
                input_name="steps",
                param_type=ParamType.INT,
                required=True,
                default=int(steps),
                min_value=1,
                max_value=100,
            ),
            "cfg": ExposedParam(
                node_id="9",
                input_name="cfg",
                param_type=ParamType.FLOAT,
                required=True,
                default=5.0,
                min_value=1.0,
                max_value=20.0,
            ),
            "denoise": ExposedParam(
                node_id="9",
                input_name="denoise",
                param_type=ParamType.FLOAT,
                required=True,
                default=float(denoise),
                min_value=0.01,
                max_value=1.0,
            ),
            "sampler_name": ExposedParam(
                node_id="9",
                input_name="sampler_name",
                param_type=ParamType.STRING,
                required=True,
                default=sampler_name,
            ),
            "scheduler": ExposedParam(
                node_id="9",
                input_name="scheduler",
                param_type=ParamType.STRING,
                required=True,
                default=scheduler,
            ),
            "input_beauty": ExposedParam(
                node_id="3",
                input_name="image",
                param_type=ParamType.FILE_PATH,
                required=True,
                per_frame=True,
            ),
            "output_prefix": ExposedParam(
                node_id="11",
                input_name="filename_prefix",
                param_type=ParamType.STRING,
                required=True,
                default="comfyguard_smoke",
                per_frame=True,
                is_output_prefix=True,
            ),
        },
    )
    contract_path = workflow_dir / "contract.yaml"
    contract_path.write_text(
        yaml.safe_dump(contract.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    return workflow_dir, contract


async def run_smoke_test(
    *,
    endpoint: str | None = None,
    checkpoint: str | None = None,
    width: int = 512,
    height: int = 512,
    steps: int = 4,
    denoise: float = 0.15,
    min_edge_iou: float = 0.25,
    output_dir: Path | None = None,
    timeout: float = 180.0,
    check_only: bool = False,
    upload_inputs: bool = True,
    candidate_endpoints: Sequence[str] = DEFAULT_LOCAL_ENDPOINTS,
) -> SmokeTestResult:
    """Probe a ComfyUI worker (or auto-discover local Desktop/CLI) and run a smoke job."""
    if width < 64 or height < 64 or width % 32 != 0 or height % 32 != 0:
        raise ValueError(
            f"Smoke-test dimensions must be >= 64 and multiples of 32, got {width}x{height}"
        )

    if endpoint is not None:
        probe = await probe_worker(endpoint)
        target_endpoint = probe.endpoint
    else:
        discovered = await discover_live_endpoint(candidate_endpoints)
        if discovered is None:
            checked = ", ".join(candidate_endpoints)
            offline_probe = WorkerProbeResult(
                endpoint=candidate_endpoints[0] if candidate_endpoints else "http://127.0.0.1:8188",
                online=False,
                error=f"No reachable ComfyUI worker at {checked}",
            )
            return SmokeTestResult(
                endpoint=offline_probe.endpoint,
                probe=offline_probe,
                check_only=check_only,
                passed=False,
                message=(
                    f"No running ComfyUI server found on {checked}. "
                    "Launch ComfyUI Desktop (default http://127.0.0.1:8000) or standard "
                    "ComfyUI (http://127.0.0.1:8188), or pass --endpoint <url>."
                ),
            )
        probe = discovered
        target_endpoint = probe.endpoint

    if not probe.online:
        return SmokeTestResult(
            endpoint=target_endpoint,
            probe=probe,
            check_only=check_only,
            passed=False,
            message=f"ComfyUI worker at {target_endpoint} is offline: {probe.error}",
        )

    if probe.missing_core_nodes:
        missing = ", ".join(probe.missing_core_nodes)
        return SmokeTestResult(
            endpoint=target_endpoint,
            probe=probe,
            check_only=check_only,
            passed=False,
            message=f"Worker at {target_endpoint} is missing core ComfyUI node(s): {missing}",
        )

    if not probe.checkpoints:
        return SmokeTestResult(
            endpoint=target_endpoint,
            probe=probe,
            check_only=check_only,
            passed=False,
            message=(
                f"Worker at {target_endpoint} is online ({probe.registered_nodes} nodes), "
                "but no checkpoints were found in CheckpointLoaderSimple.ckpt_name. "
                "Install a .safetensors checkpoint in ComfyUI's models/checkpoints folder."
            ),
        )

    if checkpoint is not None:
        if checkpoint not in probe.checkpoints:
            available = ", ".join(probe.checkpoints[:5])
            return SmokeTestResult(
                endpoint=target_endpoint,
                probe=probe,
                checkpoint=checkpoint,
                check_only=check_only,
                passed=False,
                message=(
                    f"Checkpoint '{checkpoint}' not found on {target_endpoint}. "
                    f"Available: {available}"
                ),
            )
        chosen_ckpt = checkpoint
    else:
        chosen_ckpt = probe.checkpoints[0]

    if check_only:
        return SmokeTestResult(
            endpoint=target_endpoint,
            probe=probe,
            checkpoint=chosen_ckpt,
            check_only=True,
            passed=True,
            message=(
                f"Worker {target_endpoint} is READY: {probe.registered_nodes} nodes, "
                f"{len(probe.checkpoints)} checkpoint(s), selected '{chosen_ckpt}'."
            ),
        )

    from comfyguard.dcc.submitter import PipelineOrchestrator, build_job_request
    from comfyguard.publish.exr_io import read_exr_attributes
    from comfyguard.ui.demo_runner import render_plate_rgb, save_png

    tmp_ctx: tempfile.TemporaryDirectory[str] | None = None
    if output_dir is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="comfyguard_smoke_")
        workspace = Path(tmp_ctx.name)
    else:
        workspace = Path(output_dir)
        workspace.mkdir(parents=True, exist_ok=True)

    try:
        wf_dir = workspace / "workflow_smoke"
        sampler_name = _pick_sampler(probe.samplers)
        scheduler = _pick_scheduler(probe.schedulers)
        build_smoke_workflow(
            wf_dir,
            checkpoint=chosen_ckpt,
            width=width,
            height=height,
            steps=steps,
            denoise=denoise,
            sampler_name=sampler_name,
            scheduler=scheduler,
            min_edge_iou=min_edge_iou,
        )

        plates_dir = workspace / "plates"
        save_png(plates_dir / "beauty.1001.png", render_plate_rgb(width, height))

        job = build_job_request(
            show="SMOKE",
            sequence="sq000",
            shot="sh0010",
            pass_name="smoke",
            version=1,
            workflow_dir=wf_dir,
            start_frame=1001,
            end_frame=1001,
            input_sequences={"beauty": str(plates_dir / "beauty.####.png")},
            output_dir=workspace / "publish",
        )

        submission = await PipelineOrchestrator().run_job(
            job,
            endpoints=[target_endpoint],
            staging_dir=workspace / "staging",
            upload_inputs=upload_inputs,
            reference_aov="beauty",
            timeout=timeout,
            dcc_source="cli:smoke-test",
        )

        published_exr: Path | None = None
        manifest_path: Path | None = None
        exr_attrs: dict[str, str] = {}
        if submission.publish_result is not None:
            manifest_path = submission.publish_result.manifest_path
            if 1001 in submission.publish_result.published_files:
                published_exr = submission.publish_result.published_files[1001]
                exr_attrs = read_exr_attributes(published_exr)

        status_label = "PASSED" if submission.passed else "FAILED"
        return SmokeTestResult(
            endpoint=target_endpoint,
            probe=probe,
            checkpoint=chosen_ckpt,
            check_only=False,
            passed=submission.passed,
            message=(
                f"Smoke test {status_label} on {target_endpoint} using '{chosen_ckpt}' "
                f"({width}x{height}, steps={steps}, denoise={denoise})"
            ),
            submission=submission,
            published_exr=published_exr,
            manifest_path=manifest_path,
            exr_attributes=exr_attrs,
        )
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()
