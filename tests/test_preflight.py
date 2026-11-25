"""Tests for PreflightValidator, image header inspection, and Rez package metadata."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from comfyguard.contracts.manifest import CheckStatus
from comfyguard.contracts.schema import FrameRange, JobRequest, WorkflowContract
from comfyguard.validation.preflight import (
    ImageInspectionError,
    PreflightValidator,
    inspect_image_header,
)
from tests.fixtures.image_factory import write_exr_header, write_png

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / "workflows" / "lookdev_relight_v001"
MOCK_OBJECT_INFO_PATH = Path(__file__).resolve().parent / "fixtures" / "mock_object_info.json"


@pytest.fixture
def contract() -> WorkflowContract:
    return WorkflowContract.from_yaml(WORKFLOW_DIR / "contract.yaml")


@pytest.fixture
def template_graph() -> dict[str, Any]:
    return json.loads((WORKFLOW_DIR / "workflow_api.json").read_text(encoding="utf-8"))


@pytest.fixture
def mock_object_info() -> dict[str, Any]:
    return json.loads(MOCK_OBJECT_INFO_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def model_hashes(contract: WorkflowContract) -> dict[str, str]:
    return {m.filename: m.sha256 for m in contract.required_models if m.sha256 is not None}


def _make_sequence(
    base_dir: Path,
    frames: list[int],
    width: int = 1920,
    height: int = 1088,
    *,
    fmt: str = "png",
    beauty_mode: str = "RGB",
    exr_channels: list[str] | None = None,
) -> dict[str, str]:
    for frame in frames:
        beauty_path = base_dir / f"beauty.{frame:04d}.{fmt}"
        depth_path = base_dir / f"depth.{frame:04d}.{fmt}"
        if fmt == "exr":
            write_exr_header(beauty_path, width, height, channels=exr_channels or ["B", "G", "R"])
            write_exr_header(depth_path, width, height, channels=["Z"])
        else:
            write_png(beauty_path, width, height, mode=beauty_mode)
            write_png(depth_path, width, height, mode="L")
    return {
        "beauty": str(base_dir / f"beauty.####.{fmt}"),
        "depth": str(base_dir / f"depth.####.{fmt}"),
    }


def _make_job(
    tmp_path: Path, sequences: dict[str, str], start: int = 1001, end: int = 1002
) -> JobRequest:
    return JobRequest(
        show="DEMO",
        sequence="sq010",
        shot="sh0010",
        pass_name="relight",
        version=1,
        workflow_dir=WORKFLOW_DIR,
        frame_range=FrameRange(start=start, end=end),
        base_seed=42,
        input_sequences=sequences,
        params={"steps": 24, "cfg": 6.5, "denoise": 0.45},
        output_dir=tmp_path / "out",
    )


def test_preflight_passes_aligned_png_sequence(
    tmp_path: Path,
    contract: WorkflowContract,
    template_graph: dict[str, Any],
    mock_object_info: dict[str, Any],
    model_hashes: dict[str, str],
) -> None:
    sequences = _make_sequence(tmp_path / "plates", [1001, 1002], width=1920, height=1088)
    job = _make_job(tmp_path, sequences)

    report = PreflightValidator().validate(
        job=job,
        contract=contract,
        template_graph=template_graph,
        object_info=mock_object_info,
        model_hashes=model_hashes,
    )
    assert report.passed is True
    assert not report.failed_checks


def test_preflight_rejects_unaligned_1920x1080(
    tmp_path: Path,
    contract: WorkflowContract,
    template_graph: dict[str, Any],
    mock_object_info: dict[str, Any],
    model_hashes: dict[str, str],
) -> None:
    sequences = _make_sequence(tmp_path / "plates", [1001], width=1920, height=1080)
    job = _make_job(tmp_path, sequences, start=1001, end=1001)

    report = PreflightValidator().validate(
        job=job,
        contract=contract,
        template_graph=template_graph,
        object_info=mock_object_info,
        model_hashes=model_hashes,
    )
    assert report.passed is False
    failed_names = {c.check_name for c in report.failed_checks}
    assert "resolution_alignment" in failed_names
    assert "1920x1080" in report.failed_checks[0].message


def test_preflight_rejects_out_of_bounds_resolution_and_mismatch(
    tmp_path: Path,
    contract: WorkflowContract,
    template_graph: dict[str, Any],
    mock_object_info: dict[str, Any],
    model_hashes: dict[str, str],
) -> None:
    # Small 256x256 (multiple of 32, but below min_width/min_height 512) + mismatched depth frame
    write_png(tmp_path / "plates" / "beauty.1001.png", 256, 256, mode="RGB")
    write_png(tmp_path / "plates" / "depth.1001.png", 512, 512, mode="L")
    job = _make_job(
        tmp_path,
        {
            "beauty": str(tmp_path / "plates" / "beauty.####.png"),
            "depth": str(tmp_path / "plates" / "depth.####.png"),
        },
        start=1001,
        end=1001,
    )

    report = PreflightValidator().validate(
        job=job,
        contract=contract,
        template_graph=template_graph,
        object_info=mock_object_info,
        model_hashes=model_hashes,
    )
    failed_names = {c.check_name for c in report.failed_checks}
    assert {"resolution_alignment", "resolution_consistency"} <= failed_names


def test_preflight_exr_header_inspection_and_channel_checks(
    tmp_path: Path,
    contract: WorkflowContract,
    template_graph: dict[str, Any],
    mock_object_info: dict[str, Any],
    model_hashes: dict[str, str],
) -> None:
    good_seq = _make_sequence(
        tmp_path / "exr_ok",
        [1001],
        width=1920,
        height=1088,
        fmt="exr",
        exr_channels=["B", "G", "R"],
    )
    info = inspect_image_header(tmp_path / "exr_ok" / "beauty.1001.exr")
    assert (info.width, info.height) == (1920, 1088)
    assert set(info.channels) == {"R", "G", "B"}

    ok_report = PreflightValidator().validate(
        job=_make_job(tmp_path, good_seq, start=1001, end=1001),
        contract=contract,
        template_graph=template_graph,
        object_info=mock_object_info,
        model_hashes=model_hashes,
    )
    assert ok_report.passed is True

    bad_seq = _make_sequence(
        tmp_path / "exr_bad_ch", [1001], width=1920, height=1088, fmt="exr", exr_channels=["R", "G"]
    )
    bad_report = PreflightValidator().validate(
        job=_make_job(tmp_path, bad_seq, start=1001, end=1001),
        contract=contract,
        template_graph=template_graph,
        object_info=mock_object_info,
        model_hashes=model_hashes,
    )
    assert bad_report.passed is False
    assert any(c.check_name == "aov_channels" for c in bad_report.failed_checks)


def test_preflight_catches_missing_and_corrupt_frames(
    tmp_path: Path,
    contract: WorkflowContract,
    template_graph: dict[str, Any],
    mock_object_info: dict[str, Any],
    model_hashes: dict[str, str],
) -> None:
    sequences = _make_sequence(tmp_path / "plates", [1001, 1003], width=1024, height=1024)
    # Create empty 0-byte frame 1002 for beauty; missing completely for depth
    (tmp_path / "plates" / "beauty.1002.png").write_bytes(b"")
    job = _make_job(tmp_path, sequences, start=1001, end=1003)

    report = PreflightValidator().validate(
        job=job,
        contract=contract,
        template_graph=template_graph,
        object_info=mock_object_info,
        model_hashes=model_hashes,
    )
    assert report.passed is False
    continuity_fails = [c for c in report.failed_checks if c.check_name == "frame_continuity"]
    assert len(continuity_fails) == 1
    assert len(continuity_fails[0].details["empty"]) == 1
    assert len(continuity_fails[0].details["missing"]) == 1


def test_preflight_catches_missing_required_aov(
    tmp_path: Path,
    contract: WorkflowContract,
    template_graph: dict[str, Any],
    mock_object_info: dict[str, Any],
    model_hashes: dict[str, str],
) -> None:
    write_png(tmp_path / "plates" / "beauty.1001.png", 1024, 1024)
    job = _make_job(
        tmp_path,
        {"beauty": str(tmp_path / "plates" / "beauty.####.png")},
        start=1001,
        end=1001,
    )
    report = PreflightValidator().validate(
        job=job,
        contract=contract,
        template_graph=template_graph,
        object_info=mock_object_info,
        model_hashes=model_hashes,
    )
    assert report.passed is False
    assert any(
        c.check_name == "frame_continuity" and "depth" in c.message
        for c in report.failed_checks
    )


def test_preflight_model_availability_and_hash_checks(
    tmp_path: Path,
    contract: WorkflowContract,
    template_graph: dict[str, Any],
    mock_object_info: dict[str, Any],
    model_hashes: dict[str, str],
) -> None:
    sequences = _make_sequence(tmp_path / "plates", [1001], width=1024, height=1024)
    job = _make_job(tmp_path, sequences, start=1001, end=1001)

    # 1. Missing object_info fails closed
    no_info = PreflightValidator().validate(
        job=job,
        contract=contract,
        template_graph=template_graph,
        object_info=None,
        model_hashes=model_hashes,
    )
    assert any(c.check_name == "model_availability" for c in no_info.failed_checks)

    # 2. Missing model in object_info
    bad_info = json.loads(MOCK_OBJECT_INFO_PATH.read_text(encoding="utf-8"))
    bad_info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"] = [
        ["other_model.safetensors"]
    ]
    missing_ckpt = PreflightValidator().validate(
        job=job,
        contract=contract,
        template_graph=template_graph,
        object_info=bad_info,
        model_hashes=model_hashes,
    )
    assert any(
        c.check_name == "model_availability" and "sdxl_base_1.0.safetensors" in c.message
        for c in missing_ckpt.failed_checks
    )

    # 3. Missing hash source fails closed
    no_hashes = PreflightValidator().validate(
        job=job,
        contract=contract,
        template_graph=template_graph,
        object_info=mock_object_info,
        model_hashes=None,
    )
    assert any(c.check_name == "model_hash" for c in no_hashes.failed_checks)

    # 4. On-disk models_dir hash verification (pass, then tamper fail)
    models_dir = tmp_path / "models"
    (models_dir / "checkpoints").mkdir(parents=True)
    (models_dir / "controlnet").mkdir(parents=True)
    ckpt_bytes = b"dummy-sdxl-weights"
    cnet_bytes = b"dummy-depth-cnet"
    (models_dir / "checkpoints" / "sdxl_base_1.0.safetensors").write_bytes(ckpt_bytes)
    (models_dir / "controlnet" / "diffusers_xl_depth_full.safetensors").write_bytes(cnet_bytes)

    local_contract = contract.model_copy(deep=True)
    local_contract.required_models[0].sha256 = hashlib.sha256(ckpt_bytes).hexdigest()
    local_contract.required_models[1].sha256 = hashlib.sha256(cnet_bytes).hexdigest()

    disk_ok = PreflightValidator().validate(
        job=job,
        contract=local_contract,
        template_graph=template_graph,
        object_info=mock_object_info,
        models_dir=models_dir,
    )
    assert disk_ok.passed is True

    (models_dir / "checkpoints" / "sdxl_base_1.0.safetensors").write_bytes(b"corrupted-weights")
    disk_bad = PreflightValidator().validate(
        job=job,
        contract=local_contract,
        template_graph=template_graph,
        object_info=mock_object_info,
        models_dir=models_dir,
    )
    assert disk_bad.passed is False
    assert any(c.check_name == "model_hash" for c in disk_bad.failed_checks)


def test_preflight_aggregates_multiple_failures(
    tmp_path: Path,
    contract: WorkflowContract,
    template_graph: dict[str, Any],
) -> None:
    sequences = _make_sequence(tmp_path / "plates", [1001], width=1920, height=1080)
    job = _make_job(tmp_path, sequences, start=1001, end=1001)
    job.params["denoise"] = 0.99  # exceeds contract max 0.85

    report = PreflightValidator().validate(
        job=job,
        contract=contract,
        template_graph=template_graph,
        object_info=None,
        model_hashes=None,
    )
    failed_names = {c.check_name for c in report.failed_checks}
    expected_failures = {
        "contract_params",
        "resolution_alignment",
        "model_availability",
        "model_hash",
    }
    assert expected_failures <= failed_names


def test_preflight_skip_model_check(
    tmp_path: Path,
    contract: WorkflowContract,
    template_graph: dict[str, Any],
) -> None:
    sequences = _make_sequence(tmp_path / "plates", [1001], width=1024, height=1024)
    job = _make_job(tmp_path, sequences, start=1001, end=1001)
    report = PreflightValidator().validate(
        job=job,
        contract=contract,
        template_graph=template_graph,
        skip_model_check=True,
    )
    assert report.passed is True
    assert any(
        c.check_name == "model_availability" and c.status == CheckStatus.SKIP
        for c in report.checks
    )


def test_inspect_image_header_rejects_corrupt_file(tmp_path: Path) -> None:
    bad_file = tmp_path / "corrupt.exr"
    bad_file.write_bytes(b"not-an-exr")
    with pytest.raises(ImageInspectionError):
        inspect_image_header(bad_file)


def test_rez_package_py_metadata() -> None:
    namespace: dict[str, Any] = {}
    exec((REPO_ROOT / "package.py").read_text(encoding="utf-8"), namespace)
    assert namespace["name"] == "comfyguard"
    assert namespace["version"] == "0.1.0"
    assert "comfyguard" in namespace["tools"]
    assert callable(namespace["commands"])
