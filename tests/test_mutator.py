"""Tests for contracts, GraphMutator, canonical hashing, and graph_diff."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from comfyguard.broker.mutator import (
    GraphMutator,
    NodeReferenceError,
    ParamValidationError,
    compute_graph_sha256,
    graph_diff,
)
from comfyguard.contracts.manifest import (
    CheckResult,
    CheckStatus,
    FrameLineage,
    LineageManifest,
    ValidationReport,
)
from comfyguard.contracts.schema import (
    ExposedParam,
    FrameRange,
    JobRequest,
    ParamType,
    WorkflowContract,
    expand_frame_pattern,
)

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "workflows" / "lookdev_relight_v001"


def test_load_sample_workflow_and_contract() -> None:
    mutator = GraphMutator.from_workflow_dir(WORKFLOW_DIR)
    assert mutator.contract.name == "lookdev_relight"
    assert mutator.contract.version == "v001"
    assert len(mutator.template_sha256) == 64


def test_mutate_for_frame_determinism_and_template_immutability() -> None:
    mutator = GraphMutator.from_workflow_dir(WORKFLOW_DIR)
    initial_sha = mutator.template_sha256

    frame_1001 = mutator.mutate_for_frame(
        frame=1001,
        base_seed=100,
        params={"steps": 30, "cfg": 7, "denoise": 0.5, "sampler_name": "euler"},
        input_files={
            "beauty": "/show/sh010/beauty.1001.exr",
            "depth": "/show/sh010/depth.1001.exr",
        },
        output_prefix="sh010_relight_v001_1001",
    )
    frame_1002 = mutator.mutate_for_frame(
        frame=1002,
        base_seed=100,
        params={"steps": 30, "cfg": 7, "denoise": 0.5, "sampler_name": "euler"},
        input_files={
            "beauty": "/show/sh010/beauty.1002.exr",
            "depth": "/show/sh010/depth.1002.exr",
        },
        output_prefix="sh010_relight_v001_1002",
    )

    assert mutator.template_sha256 == initial_sha
    assert frame_1001.seed == 1101
    assert frame_1002.seed == 1102
    assert frame_1001.graph["9"]["inputs"]["seed"] == 1101
    assert frame_1001.graph["9"]["inputs"]["cfg"] == 7.0
    assert isinstance(frame_1001.graph["9"]["inputs"]["cfg"], float)
    assert frame_1001.graph["3"]["inputs"]["image"] == "/show/sh010/beauty.1001.exr"
    assert frame_1001.graph["4"]["inputs"]["image"] == "/show/sh010/depth.1001.exr"
    assert frame_1001.graph["11"]["inputs"]["filename_prefix"] == "sh010_relight_v001_1001"
    assert frame_1001.graph_sha256 != frame_1002.graph_sha256


def test_canonical_sha256_invariant_to_key_ordering() -> None:
    g1 = {
        "2": {"class_type": "B", "inputs": {"z": 1, "a": 2}},
        "1": {"class_type": "A", "inputs": {}},
    }
    g2 = {
        "1": {"inputs": {}, "class_type": "A"},
        "2": {"inputs": {"a": 2, "z": 1}, "class_type": "B"},
    }
    assert compute_graph_sha256(g1) == compute_graph_sha256(g2)


@pytest.mark.parametrize(
    "bad_params",
    [
        {"steps": True},
        {"steps": 24.5},
        {"steps": 2},
        {"steps": 999},
        {"cfg": True},
        {"denoise": 0.99},
        {"denoise": float("nan")},
        {"sampler_name": "unapproved_sampler"},
        {"unknown_knob": 123},
        {"positive_prompt": "   "},
    ],
)
def test_mutate_rejects_invalid_params(bad_params: dict[str, object]) -> None:
    mutator = GraphMutator.from_workflow_dir(WORKFLOW_DIR)
    with pytest.raises(ParamValidationError):
        mutator.mutate_for_frame(
            frame=1001,
            base_seed=42,
            params=bad_params,
            input_files={"beauty": "beauty.1001.exr", "depth": "depth.1001.exr"},
        )


def test_contract_rejects_missing_node_reference() -> None:
    contract = WorkflowContract.from_yaml(WORKFLOW_DIR / "contract.yaml")
    template = json.loads((WORKFLOW_DIR / "workflow_api.json").read_text(encoding="utf-8"))
    del template["7"]
    with pytest.raises(NodeReferenceError):
        GraphMutator(contract=contract, template_graph=template)


def test_exposed_param_rejects_bad_definition() -> None:
    with pytest.raises(ValueError):
        ExposedParam(
            node_id="9",
            input_name="steps",
            param_type=ParamType.INT,
            min_value=10,
            max_value=5,
        )
    with pytest.raises(ValueError):
        ExposedParam(
            node_id="9",
            input_name="sampler_name",
            param_type=ParamType.ENUM,
            choices=[],
        )


def test_mutate_for_job_expands_patterns_and_versions(tmp_path: Path) -> None:
    mutator = GraphMutator.from_workflow_dir(WORKFLOW_DIR)
    job = JobRequest(
        show="DEMO",
        sequence="sq010",
        shot="sh0020",
        pass_name="relight",
        version=3,
        workflow_dir=WORKFLOW_DIR,
        frame_range=FrameRange(start=1001, end=1003),
        base_seed=500,
        input_sequences={
            "beauty": str(tmp_path / "beauty.####.exr"),
            "depth": str(tmp_path / "depth.%04d.exr"),
        },
        params={"steps": 20, "denoise": 0.4},
        output_dir=tmp_path / "publish",
    )

    mutated_frames = mutator.mutate_for_job(job)
    assert [m.frame for m in mutated_frames] == [1001, 1002, 1003]
    assert [m.seed for m in mutated_frames] == [1501, 1502, 1503]
    assert mutated_frames[1].graph["3"]["inputs"]["image"].endswith("beauty.1002.exr")
    assert mutated_frames[1].graph["4"]["inputs"]["image"].endswith("depth.1002.exr")
    assert (
        mutated_frames[1].graph["11"]["inputs"]["filename_prefix"]
        == "DEMO_sq010_sh0020_relight_v003_1002"
    )
    assert expand_frame_pattern("shot.{frame:04d}.exr", 42) == Path("shot.0042.exr")


def test_graph_diff_detects_structural_and_param_changes() -> None:
    v1 = json.loads((WORKFLOW_DIR / "workflow_api.json").read_text(encoding="utf-8"))
    assert graph_diff(v1, copy.deepcopy(v1)).is_identical

    v2 = copy.deepcopy(v1)
    v2["9"]["inputs"]["denoise"] = 0.65
    v2["9"]["inputs"]["positive"] = ["12", 0]
    v2["10"]["class_type"] = "VAEDecodeTiled"
    v2["12"] = {"class_type": "ConditioningCombine", "inputs": {}}
    del v2["6"]

    diff = graph_diff(v1, v2)
    assert not diff.is_identical
    assert diff.added_nodes == {"12": "ConditioningCombine"}
    assert diff.removed_nodes == {"6": "CLIPTextEncode"}
    assert len(diff.class_changes) == 1
    assert diff.class_changes[0].node_id == "10"
    assert {(c.node_id, c.input_name) for c in diff.modified_inputs} == {
        ("9", "denoise"),
        ("9", "positive"),
    }

    text = diff.format_human()
    assert "+ node 12: ConditioningCombine" in text
    assert "- node 6: CLIPTextEncode" in text
    assert "node 9 (KSampler).denoise: 0.45 -> 0.65" in text


def test_lineage_manifest_json_roundtrip(tmp_path: Path) -> None:
    report = ValidationReport(stage="preflight")
    report.add_check(
        CheckResult(check_name="resolution_alignment", status=CheckStatus.PASS, message="OK")
    )
    manifest = LineageManifest(
        job_id="cg_test123",
        show="DEMO",
        sequence="sq010",
        shot="sh0020",
        pass_name="relight",
        version=1,
        contract_name="lookdev_relight",
        contract_version="v001",
        workflow_sha256="a" * 64,
        base_seed=42,
        colorspace="ACEScg",
        rez_packages=["comfyguard-0.1.0"],
        preflight_report=report,
        frames=[
            FrameLineage(
                frame=1001,
                seed=1043,
                graph_sha256="b" * 64,
                input_files={"beauty": "beauty.1001.exr"},
                output_file="out.1001.exr",
            )
        ],
    )
    out_file = tmp_path / "manifest.json"
    manifest.to_json_file(out_file)
    loaded = LineageManifest.from_json_file(out_file)
    assert loaded.job_id == "cg_test123"
    assert loaded.frames[0].seed == 1043
    assert loaded.preflight_report is not None
    assert loaded.preflight_report.passed is True
