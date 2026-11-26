"""Offline mock-DCC unit tests for Nuke and Houdini submission hooks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from comfyguard.contracts.schema import WorkflowContract
from comfyguard.dcc.houdini_hook import (
    HoudiniUnavailableError,
    build_job_from_houdini,
    build_job_from_pdg_work_item,
    capture_houdini_context,
    extract_houdini_frame_range,
    normalize_houdini_frame_pattern,
    submit_from_houdini,
    submit_from_houdini_async,
)
from comfyguard.dcc.nuke_hook import (
    NukeUnavailableError,
    build_job_from_nuke,
    capture_nuke_context,
    create_nuke_submit_node,
    extract_nuke_inputs,
    register_nuke_menu,
    submit_from_nuke,
    submit_from_nuke_async,
)
from comfyguard.publish.exr_io import read_exr_attributes
from comfyguard.ui.demo_runner import DemoComfyServer, png_bytes, render_plate_rgb, save_png

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / "workflows" / "lookdev_relight_v001"
MOCK_OBJECT_INFO_PATH = Path(__file__).resolve().parent / "fixtures" / "mock_object_info.json"


# ---------------------------------------------------------------------------
# In-memory Mock Foundry Nuke API
# ---------------------------------------------------------------------------


class MockKnob:
    def __init__(self, name: str, value: Any = "", label: str = "") -> None:
        self._name = name
        self._value = value
        self._label = label

    def value(self) -> Any:
        return self._value

    def setValue(self, val: Any) -> None:
        self._value = val


class MockNukeNode:
    def __init__(self, node_class: str, name: str, knobs: dict[str, Any] | None = None) -> None:
        self._class = node_class
        self._name = name
        self._knobs: dict[str, MockKnob] = {
            k: MockKnob(k, v) for k, v in (knobs or {}).items()
        }

    def name(self) -> str:
        return self._name

    def Class(self) -> str:
        return self._class

    def knob(self, name: str) -> MockKnob | None:
        return self._knobs.get(name)

    def addKnob(self, knob: MockKnob) -> None:
        self._knobs[knob._name] = knob

    def __getitem__(self, name: str) -> MockKnob:
        if name not in self._knobs:
            raise NameError(f"Knob '{name}' not on {self._name}")
        return self._knobs[name]


class MockNukeMenu:
    def __init__(self, name: str) -> None:
        self.name = name
        self.submenus: dict[str, MockNukeMenu] = {}
        self.commands: list[tuple[str, str]] = []

    def addMenu(self, title: str) -> MockNukeMenu:
        sub = self.submenus.setdefault(title, MockNukeMenu(title))
        return sub

    def addCommand(self, title: str, command: str) -> None:
        self.commands.append((title, command))


class MockNukeNodesFactory:
    def __init__(self, owner: MockNukeModule) -> None:
        self._owner = owner

    def Read(self, **kwargs: Any) -> MockNukeNode:
        return self._owner.createNode("Read", **kwargs)

    def NoOp(self, **kwargs: Any) -> MockNukeNode:
        return self._owner.createNode("NoOp", **kwargs)


class MockNukeModule:
    NUKE_VERSION_STRING = "15.1v3"

    def __init__(self, *, scene_name: str = "NOVA_sq010_sh0020_comp_v004.nk") -> None:
        self._root = MockNukeNode(
            "Root",
            scene_name,
            {
                "first_frame": 1001,
                "last_frame": 1002,
                "colorManagement": "OCIO",
                "OCIO_config": "aces_1.2",
            },
        )
        self.created_nodes: list[MockNukeNode] = []
        self.menus: dict[str, MockNukeMenu] = {"Nodes": MockNukeMenu("Nodes")}
        self.nodes = MockNukeNodesFactory(self)

    def root(self) -> MockNukeNode:
        return self._root

    def createNode(
        self, node_class: str, *, inpanel: bool = False, name: str | None = None
    ) -> MockNukeNode:
        _ = inpanel
        idx = len(self.created_nodes) + 1
        default_knobs: dict[str, Any] = {}
        if node_class == "Read":
            default_knobs = {
                "file": "",
                "first": 1,
                "last": 1,
                "origfirst": 1,
                "origlast": 1,
                "colorspace": "default",
                "label": "",
            }
        node = MockNukeNode(node_class, name or f"{node_class}{idx}", default_knobs)
        self.created_nodes.append(node)
        return node

    def menu(self, name: str) -> MockNukeMenu:
        return self.menus.setdefault(name, MockNukeMenu(name))

    @staticmethod
    def String_Knob(name: str, label: str = "") -> MockKnob:
        return MockKnob(name, "", label)

    @staticmethod
    def Int_Knob(name: str, label: str = "") -> MockKnob:
        return MockKnob(name, 0, label)

    @staticmethod
    def Double_Knob(name: str, label: str = "") -> MockKnob:
        return MockKnob(name, 0.0, label)


# ---------------------------------------------------------------------------
# In-memory Mock SideFX Houdini API
# ---------------------------------------------------------------------------


class MockHouParm:
    def __init__(self, raw_value: Any, hou_mod: MockHouModule | None = None) -> None:
        self._raw = raw_value
        self._hou = hou_mod

    def unexpandedString(self) -> str:
        return str(self._raw)

    def eval(self) -> Any:
        if isinstance(self._raw, str) and self._hou is not None:
            return self._hou.expandString(self._raw)
        return self._raw

    def set(self, value: Any) -> None:
        self._raw = value


class MockHouNode:
    def __init__(
        self, path: str, parms: dict[str, Any], hou_mod: MockHouModule | None = None
    ) -> None:
        self._path = path
        self._parms: dict[str, MockHouParm] = {
            k: MockHouParm(v, hou_mod) for k, v in parms.items()
        }

    def path(self) -> str:
        return self._path

    def parm(self, name: str) -> MockHouParm | None:
        return self._parms.get(name)


class MockHipFile:
    def __init__(self, hip_path: str) -> None:
        self._path = hip_path

    def path(self) -> str:
        return self._path


class MockPlaybar:
    def __init__(self, start: int, end: int) -> None:
        self._range = (start, end)

    def frameRange(self) -> tuple[int, int]:
        return self._range


class MockPDGWorkItem:
    def __init__(self, name: str, index: int, attribs: dict[str, Any]) -> None:
        self.name = name
        self.index = index
        self._attribs = dict(attribs)

    def attribValue(self, name: str) -> Any:
        if name == "name":
            return self.name
        if name == "index":
            return self.index
        return self._attribs.get(name)


class MockHouModule:
    def __init__(
        self,
        *,
        hip_path: str = "/shows/NOVA/sq010/sh0030/lookdev/NOVA_sq010_sh0030_lookdev_v002.hip",
        hip_dir: str = "/shows/NOVA/sq010/sh0030",
        current_frame: int = 1001,
        frame_range: tuple[int, int] = (1001, 1002),
    ) -> None:
        self.hipFile = MockHipFile(hip_path)
        self.playbar = MockPlaybar(*frame_range)
        self._hip_dir = hip_dir
        self._frame = current_frame

    def applicationVersionString(self) -> str:
        return "20.5.370"

    def fps(self) -> float:
        return 24.0

    def frame(self) -> int:
        return self._frame

    def expandString(self, expr: str) -> str:
        return (
            expr.replace("$HIP", self._hip_dir)
            .replace("$JOB", self._hip_dir)
            .replace("$F4", f"{self._frame:04d}")
            .replace("$F", str(self._frame))
        )


# ---------------------------------------------------------------------------
# Nuke Hook Tests
# ---------------------------------------------------------------------------


def test_nuke_unavailable_raises_clean_error() -> None:
    with pytest.raises(NukeUnavailableError):
        capture_nuke_context()


def test_nuke_context_inputs_and_menu_node_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REZ_USED_RESOLVE", "comfyguard-0.1.0 nuke-15.1")
    monkeypatch.setenv("USERNAME", "comp_artist")

    mock_nuke = MockNukeModule()
    ctx = capture_nuke_context(nuke_mod=mock_nuke)
    assert ctx.dcc_app == "nuke"
    assert ctx.scene_file == "NOVA_sq010_sh0020_comp_v004.nk"
    assert ctx.artist == "comp_artist"
    assert ctx.extra_metadata["nuke_version"] == "15.1v3"
    assert ctx.extra_metadata["ocio_config"] == "aces_1.2"
    assert ctx.rez_packages == ["comfyguard-0.1.0", "nuke-15.1"]

    beauty_read = MockNukeNode("Read", "Read_Beauty", {"file": "/plates/beauty.%04d.exr"})
    depth_read = MockNukeNode("Read", "Read_Depth", {"file": "/plates/depth.####.exr"})
    extracted = extract_nuke_inputs(beauty_read, depth_read)
    assert extracted == {
        "beauty": "/plates/beauty.%04d.exr",
        "depth": "/plates/depth.####.exr",
    }

    with pytest.raises(ValueError, match="missing a non-empty 'file' knob"):
        extract_nuke_inputs(MockNukeNode("Read", "EmptyRead", {"file": ""}))

    submit_node = create_nuke_submit_node(nuke_mod=mock_nuke)
    assert submit_node.knob("cg_show").value() == "NOVA"
    assert submit_node.knob("cg_steps").value() == 24

    menu = register_nuke_menu(nuke_mod=mock_nuke)
    assert len(menu.commands) == 2

    job, _ = build_job_from_nuke(
        beauty_node=beauty_read,
        depth_node=depth_read,
        submit_node=submit_node,
        workflow_dir=WORKFLOW_DIR,
        output_dir=tmp_path / "publish",
        nuke_mod=mock_nuke,
    )
    assert job.show == "NOVA"
    assert job.sequence == "sq010"
    assert job.shot == "sh0010"
    assert job.frame_range.frames() == [1001, 1002]

    # Without submit_node, shot tokens infer from the Nuke script filename
    inferred_job, _ = build_job_from_nuke(
        beauty_node=beauty_read,
        depth_node=depth_read,
        workflow_dir=WORKFLOW_DIR,
        output_dir=tmp_path / "publish",
        nuke_mod=mock_nuke,
    )
    assert inferred_job.shot == "sh0020"


async def test_nuke_submit_creates_read_node_on_pass_and_skips_on_reject(
    tmp_path: Path,
) -> None:
    for frame in (1001, 1002):
        save_png(tmp_path / "plates" / f"beauty.{frame:04d}.png", render_plate_rgb(512, 512))
        save_png(tmp_path / "plates" / f"depth.{frame:04d}.png", render_plate_rgb(512, 512))

    mock_nuke = MockNukeModule()
    beauty_read = MockNukeNode(
        "Read", "Read_Beauty", {"file": str(tmp_path / "plates" / "beauty.%04d.png")}
    )
    depth_read = MockNukeNode(
        "Read", "Read_Depth", {"file": str(tmp_path / "plates" / "depth.%04d.png")}
    )

    contract = WorkflowContract.from_yaml(WORKFLOW_DIR / "contract.yaml")
    model_hashes = {m.filename: m.sha256 for m in contract.required_models if m.sha256}
    obj_info: dict[str, Any] = json.loads(MOCK_OBJECT_INFO_PATH.read_text(encoding="utf-8"))
    relit_bytes = png_bytes(render_plate_rgb(512, 512, rim_boost=0.85))

    async with DemoComfyServer(object_info=obj_info, render_fn=lambda _g: relit_bytes) as srv:
        outcome = await submit_from_nuke_async(
            beauty_node=beauty_read,
            depth_node=depth_read,
            workflow_dir=WORKFLOW_DIR,
            output_dir=tmp_path / "publish",
            endpoints=[srv.base_url],
            model_hashes=model_hashes,
            nuke_mod=mock_nuke,
        )

    assert outcome.passed is True
    assert outcome.read_node_name == "Read1"
    assert outcome.published_pattern is not None
    assert outcome.published_pattern.endswith("NOVA_sq010_sh0020_relight_v001.%04d.exr")
    created_read = mock_nuke.created_nodes[0]
    assert created_read.knob("file").value() == outcome.published_pattern
    assert created_read.knob("first").value() == 1001
    assert created_read.knob("last").value() == 1002
    assert created_read.knob("colorspace").value() == "ACEScg"
    assert outcome.submission is not None and outcome.submission.publish_result is not None
    exr_1001 = outcome.submission.publish_result.published_files[1001]
    assert read_exr_attributes(exr_1001)["comfyguard:colorspace"] == "ACEScg"

    # Unaligned 1920x1080 input must block at preflight and NOT create a Read node
    save_png(tmp_path / "bad" / "beauty.1001.png", render_plate_rgb(1920, 1080))
    save_png(tmp_path / "bad" / "depth.1001.png", render_plate_rgb(1920, 1080))
    bad_nuke = MockNukeModule()
    bad_outcome = await submit_from_nuke_async(
        beauty_node=MockNukeNode(
            "Read", "BadBeauty", {"file": str(tmp_path / "bad" / "beauty.%04d.png")}
        ),
        depth_node=MockNukeNode(
            "Read", "BadDepth", {"file": str(tmp_path / "bad" / "depth.%04d.png")}
        ),
        workflow_dir=WORKFLOW_DIR,
        output_dir=tmp_path / "publish_bad",
        start_frame=1001,
        end_frame=1001,
        endpoints=["http://127.0.0.1:1"],
        skip_model_check=True,
        nuke_mod=bad_nuke,
    )
    assert bad_outcome.passed is False
    assert bad_outcome.read_node_name is None
    assert bad_nuke.created_nodes == []


def test_nuke_deadline_export_sync(tmp_path: Path) -> None:
    mock_nuke = MockNukeModule()
    beauty_read = MockNukeNode("Read", "Read1", {"file": "/plates/beauty.%04d.exr"})
    depth_read = MockNukeNode("Read", "Read2", {"file": "/plates/depth.%04d.exr"})
    dl_job_file = tmp_path / "deadline" / "nuke_job.json"

    outcome = submit_from_nuke(
        beauty_node=beauty_read,
        depth_node=depth_read,
        workflow_dir=WORKFLOW_DIR,
        output_dir=tmp_path / "publish",
        to_deadline=True,
        job_file_path=dl_job_file,
        nuke_mod=mock_nuke,
    )
    assert outcome.passed is True
    assert dl_job_file.is_file()
    assert outcome.deadline_job_info is not None
    assert outcome.deadline_job_info["Frames"] == "1001-1002"
    assert outcome.deadline_job_info["Name"] == "NOVA_sq010_sh0020_relight_v001"


# ---------------------------------------------------------------------------
# Houdini & PDG Hook Tests
# ---------------------------------------------------------------------------


def test_houdini_unavailable_raises_clean_error() -> None:
    with pytest.raises(HoudiniUnavailableError):
        capture_houdini_context()


def test_houdini_pattern_normalization_and_frame_range(tmp_path: Path) -> None:
    plates_dir = tmp_path / "plates"
    mock_hou = MockHouModule(hip_dir=plates_dir.as_posix(), current_frame=1005)

    assert (
        normalize_houdini_frame_pattern("$HIP/beauty.$F4.exr", hou_mod=mock_hou)
        == f"{plates_dir.as_posix()}/beauty.####.exr"
    )
    assert (
        normalize_houdini_frame_pattern("$JOB/depth.${F3}.png", hou_mod=mock_hou)
        == f"{plates_dir.as_posix()}/depth.###.png"
    )
    assert (
        normalize_houdini_frame_pattern("$HIP/mask.$F.exr", hou_mod=mock_hou)
        == f"{plates_dir.as_posix()}/mask.%d.exr"
    )

    rop_current = MockHouNode("/out/comfyguard1", {"trange": 0}, hou_mod=mock_hou)
    assert extract_houdini_frame_range(rop_current, hou_mod=mock_hou) == (1005, 1005, 1)

    rop_range = MockHouNode(
        "/out/comfyguard1", {"trange": 1, "f1": 1001, "f2": 1009, "f3": 2}, hou_mod=mock_hou
    )
    assert extract_houdini_frame_range(rop_range, hou_mod=mock_hou) == (1001, 1009, 2)

    ctx = capture_houdini_context(hou_mod=mock_hou)
    assert ctx.dcc_app == "houdini"
    assert ctx.extra_metadata["houdini_version"] == "20.5.370"
    assert ctx.extra_metadata["fps"] == 24.0


async def test_houdini_hda_and_pdg_submit_end_to_end(tmp_path: Path) -> None:
    plates_dir = tmp_path / "plates"
    for frame in (1001, 1002):
        save_png(plates_dir / f"beauty.{frame:04d}.png", render_plate_rgb(512, 512))
        save_png(plates_dir / f"depth.{frame:04d}.png", render_plate_rgb(512, 512))

    mock_hou = MockHouModule(hip_dir=plates_dir.as_posix())
    hda_node = MockHouNode(
        "/out/comfyguard_relight1",
        {
            "cg_show": "NOVA",
            "cg_sequence": "sq010",
            "cg_shot": "sh0030",
            "cg_pass_name": "relight",
            "cg_version": 2,
            "cg_workflow_dir": str(WORKFLOW_DIR),
            "cg_output_dir": str(tmp_path / "publish"),
            "cg_beauty_path": "$HIP/beauty.$F4.png",
            "cg_depth_path": "$HIP/depth.$F4.png",
            "cg_steps": 24,
            "cg_denoise": 0.45,
            "trange": 1,
            "f1": 1001,
            "f2": 1002,
            "f3": 1,
            "cg_last_job_id": "",
            "cg_last_status": "",
            "cg_published_path": "",
            "cg_manifest_path": "",
        },
        hou_mod=mock_hou,
    )

    contract = WorkflowContract.from_yaml(WORKFLOW_DIR / "contract.yaml")
    model_hashes = {m.filename: m.sha256 for m in contract.required_models if m.sha256}
    obj_info: dict[str, Any] = json.loads(MOCK_OBJECT_INFO_PATH.read_text(encoding="utf-8"))
    relit_bytes = png_bytes(render_plate_rgb(512, 512, rim_boost=0.85))

    async with DemoComfyServer(object_info=obj_info, render_fn=lambda _g: relit_bytes) as srv:
        outcome = await submit_from_houdini_async(
            node=hda_node,
            endpoints=[srv.base_url],
            model_hashes=model_hashes,
            hou_mod=mock_hou,
        )

    assert outcome.passed is True
    assert outcome.published_pattern is not None
    assert outcome.published_pattern.endswith("NOVA_sq010_sh0030_relight_v002.$F4.exr")
    assert hda_node.parm("cg_last_status").eval() == "PUBLISHED"
    assert hda_node.parm("cg_published_path").unexpandedString() == outcome.published_pattern
    assert hda_node.parm("cg_manifest_path").eval().endswith("lineage_manifest.json")

    # PDG / TOPs single-frame work item submission
    work_item = MockPDGWorkItem(
        "relight_f1001",
        0,
        {
            "show": "NOVA",
            "sequence": "sq010",
            "shot": "sh0030",
            "pass_name": "relight_pdg",
            "version": 1,
            "frame": 1001,
            "beauty_path": "$HIP/beauty.$F4.png",
            "depth_path": "$HIP/depth.$F4.png",
            "output_dir": str(tmp_path / "publish_pdg"),
            "workflow_dir": str(WORKFLOW_DIR),
        },
    )
    pdg_job, pdg_ctx = build_job_from_pdg_work_item(work_item, hou_mod=mock_hou)
    assert pdg_job.frame_range.frames() == [1001]
    assert pdg_ctx.extra_metadata["pdg_work_item"] == "relight_f1001"

    async with DemoComfyServer(object_info=obj_info, render_fn=lambda _g: relit_bytes) as srv:
        pdg_outcome = await submit_from_houdini_async(
            work_item=work_item,
            endpoints=[srv.base_url],
            model_hashes=model_hashes,
            hou_mod=mock_hou,
        )
    assert pdg_outcome.passed is True
    assert pdg_outcome.published_pattern is not None
    assert pdg_outcome.published_pattern.endswith("NOVA_sq010_sh0030_relight_pdg_v001.$F4.exr")


def test_houdini_deadline_export_sync(tmp_path: Path) -> None:
    mock_hou = MockHouModule(hip_dir=(tmp_path / "plates").as_posix())
    hda_node = MockHouNode(
        "/out/comfyguard_relight1",
        {
            "cg_show": "NOVA",
            "cg_sequence": "sq010",
            "cg_shot": "sh0030",
            "cg_pass_name": "relight",
            "cg_version": 4,
            "cg_workflow_dir": str(WORKFLOW_DIR),
            "cg_output_dir": str(tmp_path / "publish"),
            "cg_beauty_path": "$HIP/beauty.$F4.exr",
            "cg_depth_path": "$HIP/depth.$F4.exr",
            "trange": 1,
            "f1": 1001,
            "f2": 1004,
            "f3": 1,
        },
        hou_mod=mock_hou,
    )
    job, _ = build_job_from_houdini(node=hda_node, hou_mod=mock_hou)
    assert job.version == 4

    dl_file = tmp_path / "deadline" / "houdini_job.json"
    outcome = submit_from_houdini(
        node=hda_node,
        to_deadline=True,
        job_file_path=dl_file,
        hou_mod=mock_hou,
    )
    assert outcome.passed is True
    assert dl_file.is_file()
    assert outcome.deadline_job_info is not None
    assert outcome.deadline_job_info["Frames"] == "1001-1004"
    assert outcome.deadline_job_info["Name"] == "NOVA_sq010_sh0030_relight_v004"
