"""Tests for EXR I/O, PostflightValidator, and quarantine Publisher (Verification Gate 3)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from comfyguard.contracts.manifest import CheckStatus, LineageManifest
from comfyguard.contracts.schema import FrameRange, JobRequest, WorkflowContract
from comfyguard.publish.exr_io import read_exr, read_exr_attributes, write_exr
from comfyguard.publish.publisher import FramePublishInput, Publisher
from comfyguard.validation.postflight import (
    PostflightFrameInput,
    PostflightValidator,
    compute_edge_iou,
    compute_temporal_delta,
)
from comfyguard.validation.preflight import inspect_image_header

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "workflows" / "lookdev_relight_v001"


@pytest.fixture
def contract() -> WorkflowContract:
    return WorkflowContract.from_yaml(WORKFLOW_DIR / "contract.yaml")


def _render_box(
    width: int = 512,
    height: int = 512,
    *,
    shift_x: int = 0,
    brightness: float = 0.7,
) -> np.ndarray:
    arr = np.full((height, width, 3), 0.1, dtype=np.float32)
    x0 = max(128 + shift_x, 0)
    x1 = min(384 + shift_x, width)
    color = np.array([brightness, brightness * 0.8, brightness * 0.6], dtype=np.float32)
    arr[128:384, x0:x1] = color
    return arr


def _save_png(path: Path, arr: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    u8 = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    Image.fromarray(u8, mode="RGB").save(path, format="PNG")
    return path


def _make_job(tmp_path: Path, start: int = 1001, end: int = 1002) -> JobRequest:
    return JobRequest(
        job_id="cg_gate3test",
        show="NOVA",
        sequence="sq010",
        shot="sh0020",
        pass_name="relight",
        version=2,
        workflow_dir=WORKFLOW_DIR,
        frame_range=FrameRange(start=start, end=end),
        base_seed=100,
        input_sequences={
            "beauty": str(tmp_path / "plates" / "beauty.####.png"),
            "depth": str(tmp_path / "plates" / "depth.####.png"),
        },
        output_dir=tmp_path / "publish",
        rez_packages=["comfyguard-0.1.0"],
    )


def test_exr_write_read_roundtrip_with_attributes_and_nan(tmp_path: Path) -> None:
    pixels = _render_box(64, 64)
    pixels[5, 5, 0] = float("nan")
    pixels[6, 6, 1] = float("inf")
    exr_path = tmp_path / "stamped.1001.exr"

    write_exr(
        exr_path,
        pixels,
        channels=["R", "G", "B"],
        attributes={
            "comfyguard:graph_hash": "a" * 64,
            "comfyguard:seed": "1101",
            "comfyguard:colorspace": "ACEScg",
        },
    )

    info = inspect_image_header(exr_path)
    assert (info.width, info.height) == (64, 64)
    assert set(info.channels) == {"R", "G", "B"}

    loaded, channels, attrs = read_exr(exr_path)
    assert channels == ["R", "G", "B"]
    assert loaded.shape == (64, 64, 3)
    assert np.isnan(loaded[5, 5, 0])
    assert np.isinf(loaded[6, 6, 1])
    assert np.allclose(loaded[20, 20], pixels[20, 20], atol=1e-5)
    assert attrs["comfyguard:seed"] == "1101"
    assert attrs["comfyguard:colorspace"] == "ACEScg"
    assert read_exr_attributes(exr_path)["comfyguard:graph_hash"] == "a" * 64


def test_postflight_passes_clean_relit_sequence(
    tmp_path: Path, contract: WorkflowContract
) -> None:
    frames: list[PostflightFrameInput] = []
    for idx, frame in enumerate((1001, 1002, 1003)):
        ref_path = _save_png(
            tmp_path / f"ref.{frame}.png", _render_box(512, 512, brightness=0.5)
        )
        out_path = _save_png(
            tmp_path / f"out.{frame}.png",
            _render_box(512, 512, brightness=0.72 + idx * 0.03),
        )
        frames.append(
            PostflightFrameInput(frame=frame, output_path=out_path, reference_path=ref_path)
        )

    res = PostflightValidator().validate_sequence(
        frames, contract, expected_resolution=(512, 512)
    )
    assert res.report.passed is True
    assert res.passed_frames == [1001, 1002, 1003]
    assert not res.failed_frames
    assert res.frame_metrics[1001].edge_iou is not None
    assert res.frame_metrics[1001].edge_iou >= 0.85
    assert res.frame_metrics[1002].temporal_delta is not None


def test_postflight_catches_nan_and_inf_buffers(
    tmp_path: Path, contract: WorkflowContract
) -> None:
    bad = _render_box(512, 512)
    bad[10, 10, 0] = float("nan")
    bad_exr = write_exr(tmp_path / "nan.1001.exr", bad)

    res = PostflightValidator().validate_sequence(
        [PostflightFrameInput(frame=1001, output_path=bad_exr)],
        contract,
        expected_resolution=(512, 512),
    )
    assert res.report.passed is False
    assert 1001 in res.failed_frames
    assert any(c.check_name == "buffer_integrity" for c in res.report.failed_checks)


def test_postflight_catches_flat_frame(tmp_path: Path, contract: WorkflowContract) -> None:
    flat = np.full((512, 512, 3), 0.5, dtype=np.float32)
    flat_path = _save_png(tmp_path / "flat.1001.png", flat)

    res = PostflightValidator().validate_sequence(
        [PostflightFrameInput(frame=1001, output_path=flat_path)],
        contract,
        expected_resolution=(512, 512),
    )
    assert res.report.passed is False
    assert any(c.check_name == "flat_frame" for c in res.report.failed_checks)


def test_postflight_catches_resolution_and_channel_mismatch(
    tmp_path: Path, contract: WorkflowContract
) -> None:
    rgba = np.full((512, 512, 4), 0.4, dtype=np.float32)
    rgba[100:200, 100:200] = 0.9
    rgba_exr = write_exr(tmp_path / "rgba.1001.exr", rgba, channels=["R", "G", "B", "A"])

    res = PostflightValidator().validate_sequence(
        [PostflightFrameInput(frame=1001, output_path=rgba_exr)],
        contract,
        expected_resolution=(1024, 1024),
    )
    failed_checks = {c.check_name for c in res.report.failed_checks}
    assert {"output_resolution", "output_channels"} <= failed_checks


def test_postflight_catches_edge_drift(tmp_path: Path, contract: WorkflowContract) -> None:
    ref = _render_box(512, 512, shift_x=0)
    drifted = _render_box(512, 512, shift_x=110)
    assert compute_edge_iou(drifted, ref) < 0.25

    ref_path = _save_png(tmp_path / "ref.1001.png", ref)
    out_path = _save_png(tmp_path / "drifted.1001.png", drifted)

    res = PostflightValidator().validate_sequence(
        [PostflightFrameInput(frame=1001, output_path=out_path, reference_path=ref_path)],
        contract,
        expected_resolution=(512, 512),
    )
    assert res.report.passed is False
    assert 1001 in res.failed_frames
    assert any(c.check_name == "edge_drift" for c in res.report.failed_checks)


def test_postflight_catches_temporal_flicker(
    tmp_path: Path, contract: WorkflowContract
) -> None:
    f1 = _render_box(512, 512, brightness=0.2)
    f2 = _render_box(512, 512, brightness=0.95)
    # Bright sky flash across frame 1002 so mean absolute delta exceeds 0.20
    f2_flash = np.clip(f2 + 0.4, 0.0, 1.0)
    assert compute_temporal_delta(f1, f2_flash) > 0.20

    p1 = _save_png(tmp_path / "out.1001.png", f1)
    p2 = _save_png(tmp_path / "out.1002.png", f2_flash)

    res = PostflightValidator().validate_sequence(
        [
            PostflightFrameInput(frame=1001, output_path=p1),
            PostflightFrameInput(frame=1002, output_path=p2),
        ],
        contract,
        expected_resolution=(512, 512),
    )
    assert res.report.passed is False
    assert 1001 in res.passed_frames
    assert 1002 in res.failed_frames
    assert any(c.check_name == "temporal_delta" for c in res.report.failed_checks)


def test_publisher_stamps_exr_and_quarantines_failed_frames(
    tmp_path: Path, contract: WorkflowContract
) -> None:
    ref = _render_box(512, 512, shift_x=0)
    ref_1001 = _save_png(tmp_path / "plates" / "depth.1001.png", ref)
    ref_1002 = _save_png(tmp_path / "plates" / "depth.1002.png", ref)

    good_out = _save_png(tmp_path / "raw" / "out.1001.png", _render_box(512, 512, brightness=0.85))
    drifted_out = _save_png(tmp_path / "raw" / "out.1002.png", _render_box(512, 512, shift_x=110))

    post_res = PostflightValidator().validate_sequence(
        [
            PostflightFrameInput(frame=1001, output_path=good_out, reference_path=ref_1001),
            PostflightFrameInput(frame=1002, output_path=drifted_out, reference_path=ref_1002),
        ],
        contract,
        expected_resolution=(512, 512),
    )
    job = _make_job(tmp_path, start=1001, end=1002)
    pub_res = Publisher().publish_job(
        job=job,
        contract=contract,
        workflow_sha256="c" * 64,
        frames=[
            FramePublishInput(
                frame=1001,
                seed=1101,
                graph_sha256="d" * 64,
                source_path=good_out,
                input_files={"depth": str(ref_1001)},
                prompt_id="p_1001",
            ),
            FramePublishInput(
                frame=1002,
                seed=1102,
                graph_sha256="e" * 64,
                source_path=drifted_out,
                input_files={"depth": str(ref_1002)},
                prompt_id="p_1002",
            ),
        ],
        postflight_result=post_res,
        dcc_source="nuke:shot020.nk",
    )

    assert 1001 in pub_res.published_files
    assert 1002 not in pub_res.published_files
    assert 1002 in pub_res.quarantined_files
    assert pub_res.published_files[1001].is_file()
    assert pub_res.quarantined_files[1002].is_file()

    attrs = read_exr_attributes(pub_res.published_files[1001])
    assert attrs["comfyguard:graph_hash"] == "d" * 64
    assert attrs["comfyguard:seed"] == "1101"
    assert attrs["comfyguard:colorspace"] == "ACEScg"

    assert pub_res.manifest_path is not None and pub_res.manifest_path.is_file()
    assert pub_res.quarantine_manifest_path is not None
    loaded_manifest = LineageManifest.from_json_file(pub_res.manifest_path)
    assert loaded_manifest.dcc_source == "nuke:shot020.nk"
    assert loaded_manifest.frames[0].quarantined is False
    assert loaded_manifest.frames[1].quarantined is True
    assert loaded_manifest.frames[1].quarantine_reason is not None
    assert loaded_manifest.postflight_report is not None
    assert loaded_manifest.postflight_report.checks[-1].status == CheckStatus.FAIL


def test_publisher_atomic_quarantine_blocks_entire_job(
    tmp_path: Path, contract: WorkflowContract
) -> None:
    ref = _render_box(512, 512, shift_x=0)
    ref_1001 = _save_png(tmp_path / "plates" / "depth.1001.png", ref)
    ref_1002 = _save_png(tmp_path / "plates" / "depth.1002.png", ref)

    good_out = _save_png(tmp_path / "raw" / "out.1001.png", _render_box(512, 512))
    bad_out = _save_png(tmp_path / "raw" / "out.1002.png", _render_box(512, 512, shift_x=110))

    post_res = PostflightValidator().validate_sequence(
        [
            PostflightFrameInput(frame=1001, output_path=good_out, reference_path=ref_1001),
            PostflightFrameInput(frame=1002, output_path=bad_out, reference_path=ref_1002),
        ],
        contract,
        expected_resolution=(512, 512),
    )
    job = _make_job(tmp_path, start=1001, end=1002)
    pub_res = Publisher().publish_job(
        job=job,
        contract=contract,
        workflow_sha256="c" * 64,
        frames=[
            FramePublishInput(frame=1001, seed=1101, graph_sha256="1" * 64, source_path=good_out),
            FramePublishInput(frame=1002, seed=1102, graph_sha256="2" * 64, source_path=bad_out),
        ],
        postflight_result=post_res,
        atomic_job_quarantine=True,
    )

    assert len(pub_res.published_files) == 0
    assert pub_res.manifest_path is None
    assert set(pub_res.quarantined_files) == {1001, 1002}
    assert not pub_res.publish_dir.exists()
