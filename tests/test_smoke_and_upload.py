"""Offline tests for /upload/image input staging, worker probing, and smoke-test."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from comfyguard.broker.smoke import discover_live_endpoint, probe_worker, run_smoke_test
from comfyguard.cli import app
from comfyguard.contracts.schema import WorkflowContract
from comfyguard.dcc.submitter import PipelineOrchestrator, build_job_request
from comfyguard.publish.exr_io import read_exr_attributes
from comfyguard.ui.demo_runner import png_bytes, render_plate_rgb, save_png
from tests.fixtures.mock_comfy_server import MockComfyUIServer

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / "workflows" / "lookdev_relight_v001"
MOCK_OBJECT_INFO_PATH = Path(__file__).resolve().parent / "fixtures" / "mock_object_info.json"

runner = CliRunner()


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _smoke_object_info(*, checkpoints: list[str] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(MOCK_OBJECT_INFO_PATH.read_text(encoding="utf-8"))
    if checkpoints is not None:
        data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"] = [checkpoints]
    data.setdefault("CLIPTextEncode", {"input": {"required": {"text": ["STRING"]}}})
    data.setdefault("VAEEncode", {"input": {"required": {"pixels": ["IMAGE"], "vae": ["VAE"]}}})
    data.setdefault("VAEDecode", {"input": {"required": {"samples": ["LATENT"], "vae": ["VAE"]}}})
    return data


async def test_orchestrator_upload_inputs_multi_worker_staging(tmp_path: Path) -> None:
    save_png(tmp_path / "plates" / "beauty.1001.png", render_plate_rgb(512, 512))
    save_png(tmp_path / "plates" / "depth.1001.png", render_plate_rgb(512, 512))
    save_png(tmp_path / "plates" / "beauty.1002.png", render_plate_rgb(512, 512))
    save_png(tmp_path / "plates" / "depth.1002.png", render_plate_rgb(512, 512))

    contract = WorkflowContract.from_yaml(WORKFLOW_DIR / "contract.yaml")
    model_hashes = {m.filename: m.sha256 for m in contract.required_models if m.sha256}
    relit_bytes = png_bytes(render_plate_rgb(512, 512, rim_boost=0.8))

    job = build_job_request(
        show="NOVA",
        sequence="sq010",
        shot="sh0050",
        pass_name="relight",
        version=1,
        workflow_dir=WORKFLOW_DIR,
        start_frame=1001,
        end_frame=1002,
        input_sequences={
            "beauty": str(tmp_path / "plates" / "beauty.####.png"),
            "depth": str(tmp_path / "plates" / "depth.####.png"),
        },
        output_dir=tmp_path / "publish",
    )

    async with (
        MockComfyUIServer(render_fn=lambda _g: relit_bytes) as w0,
        MockComfyUIServer(render_fn=lambda _g: relit_bytes) as w1,
    ):
        res = await PipelineOrchestrator().run_job(
            job,
            endpoints=[w0.base_url, w1.base_url],
            model_hashes=model_hashes,
            upload_inputs=True,
            upload_subfolder="nova_sq010_sh0050",
        )

    assert res.passed is True
    assert len(w0.uploaded_files) == 4
    assert len(w1.uploaded_files) == 4
    assert {item["subfolder"] for item in w0.uploaded_files} == {
        "nova_sq010_sh0050_beauty",
        "nova_sq010_sh0050_depth",
    }
    assert res.staged_inputs[1001]["beauty"] == "nova_sq010_sh0050_beauty/beauty.1001.png"
    assert res.staged_inputs[1002]["depth"] == "nova_sq010_sh0050_depth/depth.1002.png"

    queued = w0.queued_prompts + w1.queued_prompts
    assert len(queued) == 2
    beauty_refs = {entry["graph"]["3"]["inputs"]["image"] for entry in queued}
    assert beauty_refs == {
        "nova_sq010_sh0050_beauty/beauty.1001.png",
        "nova_sq010_sh0050_beauty/beauty.1002.png",
    }

    assert res.publish_result is not None
    assert res.publish_result.manifest.frames[0].input_files["beauty"].endswith("beauty.1001.png")
    assert Path(res.publish_result.manifest.frames[0].input_files["beauty"]).is_file()


async def test_preflight_failure_skips_upload_inputs(tmp_path: Path) -> None:
    save_png(tmp_path / "plates" / "beauty.1001.png", render_plate_rgb(1920, 1080))
    save_png(tmp_path / "plates" / "depth.1001.png", render_plate_rgb(1920, 1080))

    contract = WorkflowContract.from_yaml(WORKFLOW_DIR / "contract.yaml")
    model_hashes = {m.filename: m.sha256 for m in contract.required_models if m.sha256}

    job = build_job_request(
        show="NOVA",
        sequence="sq010",
        shot="sh0060",
        pass_name="relight",
        version=1,
        workflow_dir=WORKFLOW_DIR,
        start_frame=1001,
        end_frame=1001,
        input_sequences={
            "beauty": str(tmp_path / "plates" / "beauty.####.png"),
            "depth": str(tmp_path / "plates" / "depth.####.png"),
        },
        output_dir=tmp_path / "publish",
    )

    async with MockComfyUIServer() as srv:
        res = await PipelineOrchestrator().run_job(
            job,
            endpoints=[srv.base_url],
            model_hashes=model_hashes,
            upload_inputs=True,
        )

    assert res.passed is False
    assert res.preflight_report.passed is False
    assert res.staged_inputs == {}
    assert len(srv.uploaded_files) == 0
    assert len(srv.queued_prompts) == 0


async def test_probe_worker_discover_and_smoke_test_end_to_end(tmp_path: Path) -> None:
    smoke_render = png_bytes(render_plate_rgb(64, 64, rim_boost=0.6))
    obj_info = _smoke_object_info()
    async with MockComfyUIServer(
        render_fn=lambda _g: smoke_render,
        object_info=obj_info,
    ) as srv:
        probe = await probe_worker(srv.base_url)
        assert probe.online is True
        assert "sdxl_base_1.0.safetensors" in probe.checkpoints
        assert probe.missing_core_nodes == []

        discovered = await discover_live_endpoint(
            ("http://127.0.0.1:1", srv.base_url),
            request_timeout=1.0,
        )
        assert discovered is not None
        assert discovered.endpoint == srv.base_url

        check_only = await run_smoke_test(
            endpoint=srv.base_url,
            check_only=True,
        )
        assert check_only.passed is True
        assert check_only.checkpoint == "sdxl_base_1.0.safetensors"
        assert len(srv.queued_prompts) == 0

        full_run = await run_smoke_test(
            endpoint=srv.base_url,
            width=64,
            height=64,
            steps=2,
            denoise=0.2,
            output_dir=tmp_path / "smoke_out",
            upload_inputs=True,
        )
        assert full_run.passed is True
        assert full_run.published_exr is not None and full_run.published_exr.is_file()
        assert full_run.manifest_path is not None and full_run.manifest_path.is_file()
        assert full_run.exr_attributes["comfyguard:colorspace"] == "ACEScg"
        assert len(srv.uploaded_files) == 1
        assert len(srv.queued_prompts) == 1
        assert srv.queued_prompts[0]["graph"]["3"]["inputs"]["image"].endswith("beauty.1001.png")


async def test_smoke_test_fails_gracefully_without_checkpoints() -> None:
    empty_ckpt_obj_info = _smoke_object_info(checkpoints=[])

    async with MockComfyUIServer(object_info=empty_ckpt_obj_info) as srv:
        res = await run_smoke_test(endpoint=srv.base_url, check_only=True)

    assert res.passed is False
    assert "no checkpoints" in res.message.lower()


def test_cli_worker_status_smoke_test_and_submit_upload_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    help_smoke = runner.invoke(app, ["smoke-test", "--help"], env={"NO_COLOR": "1"})
    assert help_smoke.exit_code == 0
    clean_help = _strip_ansi(help_smoke.stdout)
    assert "--check-only" in clean_help
    assert "--checkpoint" in clean_help
    assert "--upload-inputs" in clean_help

    help_submit = runner.invoke(app, ["submit", "--help"], env={"NO_COLOR": "1"})
    assert help_submit.exit_code == 0
    assert "--upload-inputs" in _strip_ansi(help_submit.stdout)

    # Verify offline worker-status / smoke-test error exits cleanly with code 1.
    monkeypatch.setattr(
        "comfyguard.cli.DEFAULT_LOCAL_ENDPOINTS",
        ("http://127.0.0.1:1",),
    )
    status_res = runner.invoke(app, ["worker-status"])
    assert status_res.exit_code == 1
    assert "unreachable" in status_res.stderr.lower()

    smoke_offline = runner.invoke(
        app,
        ["smoke-test", "--endpoint", "http://127.0.0.1:1", "--check-only"],
    )
    assert smoke_offline.exit_code == 1
    assert "offline" in smoke_offline.stdout.lower()

    # Verify CLI submit --upload-inputs dry-run still works cleanly without live worker
    save_png(tmp_path / "plates" / "beauty.1001.png", render_plate_rgb(512, 512))
    save_png(tmp_path / "plates" / "depth.1001.png", render_plate_rgb(512, 512))
    job = build_job_request(
        show="DEMO",
        sequence="sq01",
        shot="sh01",
        pass_name="relight",
        version=1,
        workflow_dir=WORKFLOW_DIR,
        start_frame=1001,
        end_frame=1001,
        input_sequences={
            "beauty": str(tmp_path / "plates" / "beauty.####.png"),
            "depth": str(tmp_path / "plates" / "depth.####.png"),
        },
        output_dir=tmp_path / "out",
    )
    job_file = tmp_path / "job.json"
    job_file.write_text(job.model_dump_json(indent=2), encoding="utf-8")

    dry_res = runner.invoke(
        app,
        ["submit", "--job", str(job_file), "--dry-run", "--skip-model-check", "--upload-inputs"],
    )
    assert dry_res.exit_code == 0
    assert read_exr_attributes is not None
