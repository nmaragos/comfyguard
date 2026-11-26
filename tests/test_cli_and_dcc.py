"""Tests for Typer CLI, DCC/Deadline submitter, PipelineOrchestrator, and portfolio demo."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from demo.run_portfolio_demo import DemoComfyServer, _png_bytes, _render_plate_rgb, run_demo
from PIL import Image
from typer.testing import CliRunner

from comfyguard import __version__
from comfyguard.cli import app
from comfyguard.contracts.schema import WorkflowContract
from comfyguard.dcc.submitter import (
    DCCContext,
    PipelineOrchestrator,
    build_job_request,
    generate_deadline_job_info,
)
from comfyguard.publish.exr_io import read_exr_attributes, write_exr

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / "workflows" / "lookdev_relight_v001"
MOCK_OBJECT_INFO_PATH = Path(__file__).resolve().parent / "fixtures" / "mock_object_info.json"

runner = CliRunner()


def _save_png(path: Path, width: int, height: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = _render_plate_rgb(width, height)
    u8 = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    Image.fromarray(u8, mode="RGB").save(path, format="PNG")
    return path


def test_cli_version_and_diff(tmp_path: Path) -> None:
    res = runner.invoke(app, ["version"])
    assert res.exit_code == 0
    assert __version__ in res.stdout

    v1_path = WORKFLOW_DIR / "workflow_api.json"
    v2_data = json.loads(v1_path.read_text(encoding="utf-8"))
    v2_data["9"]["inputs"]["steps"] = 36
    v2_path = tmp_path / "workflow_v002.json"
    v2_path.write_text(json.dumps(v2_data), encoding="utf-8")

    diff_res = runner.invoke(app, ["diff", str(v1_path), str(v2_path)])
    assert diff_res.exit_code == 0
    assert "node 9 (KSampler).steps: 24 -> 36" in diff_res.stdout

    diff_json = runner.invoke(app, ["diff", str(v1_path), str(v2_path), "--json"])
    assert diff_json.exit_code == 0
    parsed = json.loads(diff_json.stdout)
    assert parsed["modified_inputs"][0]["input_name"] == "steps"


def test_cli_validate_dry_run_submit_and_inspect(tmp_path: Path) -> None:
    _save_png(tmp_path / "plates" / "beauty.1001.png", 512, 512)
    _save_png(tmp_path / "plates" / "depth.1001.png", 512, 512)

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

    val_ok = runner.invoke(app, ["validate", "--job", str(job_file), "--skip-model-check"])
    assert val_ok.exit_code == 0
    assert "[PREFLIGHT] PASSED" in val_ok.stdout

    dry_res = runner.invoke(
        app, ["submit", "--job", str(job_file), "--dry-run", "--skip-model-check"]
    )
    assert dry_res.exit_code == 0
    assert "Dry-run OK: prepared 1 deterministic frame graph(s)" in dry_res.stdout

    # Now verify unaligned 1920x1080 fails validate with exit code 1
    _save_png(tmp_path / "bad_plates" / "beauty.1001.png", 1920, 1080)
    _save_png(tmp_path / "bad_plates" / "depth.1001.png", 1920, 1080)
    bad_job = job.model_copy(
        update={
            "input_sequences": {
                "beauty": str(tmp_path / "bad_plates" / "beauty.####.png"),
                "depth": str(tmp_path / "bad_plates" / "depth.####.png"),
            }
        }
    )
    bad_job_file = tmp_path / "bad_job.json"
    bad_job_file.write_text(bad_job.model_dump_json(indent=2), encoding="utf-8")

    val_bad = runner.invoke(app, ["validate", "--job", str(bad_job_file), "--skip-model-check"])
    assert val_bad.exit_code == 1
    assert "[PREFLIGHT] FAILED" in val_bad.stdout

    # Inspect EXR provenance attributes via CLI
    exr_file = tmp_path / "sample.1001.exr"
    write_exr(
        exr_file,
        _render_plate_rgb(64, 64),
        attributes={"comfyguard:seed": "1043", "comfyguard:colorspace": "ACEScg"},
    )
    insp_exr = runner.invoke(app, ["inspect", str(exr_file)])
    assert insp_exr.exit_code == 0
    assert "comfyguard:seed: 1043" in insp_exr.stdout


def test_dcc_context_and_deadline_job_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REZ_USED_RESOLVE", "comfyguard-0.1.0 python-3.13")
    monkeypatch.setenv("USERNAME", "lookdev_td")

    ctx = DCCContext.from_environment("houdini", scene_file="sh010_lookdev.hip")
    assert ctx.rez_packages == ["comfyguard-0.1.0", "python-3.13"]
    assert ctx.artist == "lookdev_td"
    assert ctx.format_source() == "houdini:sh010_lookdev.hip"

    job = build_job_request(
        show="NOVA",
        sequence="sq010",
        shot="sh0010",
        pass_name="relight",
        version=3,
        workflow_dir=WORKFLOW_DIR,
        start_frame=1001,
        end_frame=1005,
        input_sequences={"beauty": "beauty.####.exr", "depth": "depth.####.exr"},
        output_dir=tmp_path / "publish",
        dcc_context=ctx,
    )
    assert job.rez_packages == ["comfyguard-0.1.0", "python-3.13"]

    dl = generate_deadline_job_info(job, job_file_path=tmp_path / "job.json")
    assert dl["Plugin"] == "CommandLine"
    assert dl["Frames"] == "1001-1005"
    assert dl["Name"] == "NOVA_sq010_sh0010_relight_v003"


async def test_orchestrator_end_to_end_with_mock_server(tmp_path: Path) -> None:
    _save_png(tmp_path / "plates" / "beauty.1001.png", 512, 512)
    _save_png(tmp_path / "plates" / "depth.1001.png", 512, 512)
    contract = WorkflowContract.from_yaml(WORKFLOW_DIR / "contract.yaml")
    model_hashes = {m.filename: m.sha256 for m in contract.required_models if m.sha256}
    obj_info: dict[str, Any] = json.loads(MOCK_OBJECT_INFO_PATH.read_text(encoding="utf-8"))

    job = build_job_request(
        show="NOVA",
        sequence="sq010",
        shot="sh0010",
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

    relit_bytes = _png_bytes(_render_plate_rgb(512, 512, rim_boost=0.9))
    async with DemoComfyServer(object_info=obj_info, render_fn=lambda _g: relit_bytes) as srv:
        res = await PipelineOrchestrator().run_job(
            job,
            endpoints=[srv.base_url],
            model_hashes=model_hashes,
        )

    assert res.passed is True
    assert res.publish_result is not None
    exr_path = res.publish_result.published_files[1001]
    assert read_exr_attributes(exr_path)["comfyguard:colorspace"] == "ACEScg"

    # Also test CLI inspect on the published manifest
    insp = runner.invoke(app, ["inspect", str(res.publish_result.manifest_path)])
    assert insp.exit_code == 0
    assert "NOVA/sq010/sh0010 relight v001" in insp.stdout


async def test_portfolio_demo_runs_cleanly(tmp_path: Path) -> None:
    results = await run_demo(output_dir=copy.deepcopy(tmp_path / "demo_run"))
    assert results["scenario_1"].passed is False
    assert results["scenario_2"].passed is False
    assert results["scenario_3"].passed is True
    assert not results["diff"].is_identical
