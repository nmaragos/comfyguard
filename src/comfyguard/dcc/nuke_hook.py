"""Foundry Nuke submission hook, Read-node builder, and Deadline wrapper."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from comfyguard.broker.pool import WorkerConfig
from comfyguard.contracts.schema import JobRequest
from comfyguard.dcc.submitter import (
    DCCContext,
    PipelineOrchestrator,
    SubmissionResult,
    build_job_request,
    generate_deadline_job_info,
)

_SHOT_TOKEN_RE = re.compile(r"^([A-Za-z0-9]+)_([A-Za-z0-9]+)_([A-Za-z0-9]+)")


class NukeUnavailableError(RuntimeError):
    """Raised when Nuke Python bindings are requested outside a Nuke session."""


class NukeSubmissionOutcome(BaseModel):
    """Result returned to Nuke artists/TDs after direct or Deadline submission."""

    job_id: str
    passed: bool
    submission: SubmissionResult | None = None
    read_node_name: str | None = None
    published_pattern: str | None = None
    deadline_job_info: dict[str, str] | None = None


def _resolve_nuke(nuke_mod: Any | None = None) -> Any:
    if nuke_mod is not None:
        return nuke_mod
    try:
        import nuke  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NukeUnavailableError(
            "Nuke Python API ('nuke') is only available inside Foundry Nuke. "
            "Pass a mock nuke_mod for offline testing."
        ) from exc
    return nuke


def _knob_value(node: Any, name: str, default: Any = None) -> Any:
    if node is None:
        return default
    knob = None
    if hasattr(node, "knob"):
        knob = node.knob(name)
    else:
        try:
            knob = node[name]
        except (KeyError, NameError, IndexError, TypeError):
            knob = None
    if knob is None:
        return default
    val = knob.value() if hasattr(knob, "value") else knob
    return default if val in (None, "") else val


def _set_knob(node: Any, name: str, value: Any) -> bool:
    if node is None:
        return False
    knob = None
    if hasattr(node, "knob"):
        knob = node.knob(name)
    else:
        try:
            knob = node[name]
        except (KeyError, NameError, IndexError, TypeError):
            knob = None
    if knob is None:
        return False
    if hasattr(knob, "setValue"):
        knob.setValue(value)
        return True
    return False


def _infer_shot_tokens(scene_file: str) -> tuple[str, str, str]:
    env_show = os.environ.get("SHOW", "").strip()
    env_seq = (os.environ.get("SEQ") or os.environ.get("SEQUENCE") or "").strip()
    env_shot = os.environ.get("SHOT", "").strip()
    if env_show and env_seq and env_shot:
        return env_show, env_seq, env_shot

    if scene_file:
        stem = Path(scene_file).stem
        match = _SHOT_TOKEN_RE.match(stem)
        if match:
            return match.group(1), match.group(2), match.group(3)

    return env_show or "DEV", env_seq or "sq000", env_shot or "sh0000"


def _default_workflow_dir() -> Path:
    env_root = os.environ.get("COMFYGUARD_WORKFLOW_ROOT", "").strip()
    if env_root:
        candidate = Path(env_root) / "lookdev_relight_v001"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parents[3] / "workflows" / "lookdev_relight_v001"


def capture_nuke_context(*, nuke_mod: Any | None = None) -> DCCContext:
    """Capture Nuke script path, version, OCIO settings, and Rez environment."""
    nuke = _resolve_nuke(nuke_mod)
    root = nuke.root()
    raw_scene = str(root.name()) if hasattr(root, "name") else ""
    scene_file = "" if raw_scene in ("", "Root") else raw_scene

    ctx = DCCContext.from_environment("nuke", scene_file=scene_file)
    nuke_ver = getattr(nuke, "NUKE_VERSION_STRING", None)
    if nuke_ver:
        ctx.extra_metadata["nuke_version"] = str(nuke_ver)
    color_mgmt = _knob_value(root, "colorManagement")
    if color_mgmt is not None:
        ctx.extra_metadata["color_management"] = str(color_mgmt)
    ocio_cfg = _knob_value(root, "OCIO_config")
    if ocio_cfg is not None:
        ctx.extra_metadata["ocio_config"] = str(ocio_cfg)
    return ctx


def extract_nuke_inputs(
    beauty_node: Any,
    depth_node: Any | None = None,
    *,
    extra_aov_nodes: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Read unexpanded sequence patterns (%04d / ####) from Nuke Read/Write nodes."""
    beauty_path = _knob_value(beauty_node, "file")
    if not beauty_path:
        node_name = beauty_node.name() if hasattr(beauty_node, "name") else repr(beauty_node)
        raise ValueError(f"Nuke beauty node '{node_name}' is missing a non-empty 'file' knob")

    inputs: dict[str, str] = {"beauty": str(beauty_path)}
    if depth_node is not None:
        depth_path = _knob_value(depth_node, "file")
        if not depth_path:
            node_name = depth_node.name() if hasattr(depth_node, "name") else repr(depth_node)
            raise ValueError(
                f"Nuke depth node '{node_name}' is missing a non-empty 'file' knob"
            )
        inputs["depth"] = str(depth_path)

    for aov_name, aov_node in (extra_aov_nodes or {}).items():
        aov_path = _knob_value(aov_node, "file")
        if not aov_path:
            node_name = aov_node.name() if hasattr(aov_node, "name") else repr(aov_node)
            raise ValueError(
                f"Nuke AOV '{aov_name}' node '{node_name}' is missing a non-empty 'file' knob"
            )
        inputs[aov_name] = str(aov_path)

    return inputs


def build_job_from_nuke(
    *,
    beauty_node: Any,
    depth_node: Any | None = None,
    extra_aov_nodes: dict[str, Any] | None = None,
    submit_node: Any | None = None,
    show: str | None = None,
    sequence: str | None = None,
    shot: str | None = None,
    pass_name: str | None = None,
    version: int | None = None,
    workflow_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    step: int = 1,
    base_seed: int | None = None,
    params: dict[str, Any] | None = None,
    colorspace: str | None = None,
    nuke_mod: Any | None = None,
) -> tuple[JobRequest, DCCContext]:
    """Construct a typed JobRequest and DCCContext from Nuke nodes and script globals."""
    nuke = _resolve_nuke(nuke_mod)
    ctx = capture_nuke_context(nuke_mod=nuke)
    root = nuke.root()

    inferred_show, inferred_seq, inferred_shot = _infer_shot_tokens(ctx.scene_file)
    resolved_show = str(show or _knob_value(submit_node, "cg_show") or inferred_show)
    resolved_seq = str(sequence or _knob_value(submit_node, "cg_sequence") or inferred_seq)
    resolved_shot = str(shot or _knob_value(submit_node, "cg_shot") or inferred_shot)
    resolved_pass = str(pass_name or _knob_value(submit_node, "cg_pass_name") or "relight")
    resolved_version = int(
        version if version is not None else _knob_value(submit_node, "cg_version", 1)
    )

    resolved_wf = workflow_dir or _knob_value(submit_node, "cg_workflow_dir")
    wf_path = Path(resolved_wf) if resolved_wf else _default_workflow_dir()
    if not wf_path.is_dir():
        raise ValueError(f"Workflow directory not found: {wf_path}")

    resolved_out = output_dir or _knob_value(submit_node, "cg_output_dir")
    if not resolved_out:
        raise ValueError("output_dir must be provided or configured on submit_node.cg_output_dir")

    root_first = _knob_value(root, "first_frame", 1001)
    root_last = _knob_value(root, "last_frame", root_first)
    resolved_start = int(
        start_frame
        if start_frame is not None
        else _knob_value(submit_node, "cg_first_frame", root_first)
    )
    resolved_end = int(
        end_frame
        if end_frame is not None
        else _knob_value(submit_node, "cg_last_frame", root_last)
    )
    resolved_seed = int(
        base_seed if base_seed is not None else _knob_value(submit_node, "cg_base_seed", 42)
    )

    node_params: dict[str, Any] = {}
    knob_param_map = (
        ("cg_steps", "steps", int),
        ("cg_cfg", "cfg", float),
        ("cg_denoise", "denoise", float),
        ("cg_controlnet_strength", "controlnet_strength", float),
        ("cg_positive_prompt", "positive_prompt", str),
        ("cg_negative_prompt", "negative_prompt", str),
    )
    for knob_name, param_name, caster in knob_param_map:
        raw_val = _knob_value(submit_node, knob_name)
        if raw_val is not None:
            node_params[param_name] = caster(raw_val)
    if params:
        node_params.update(params)

    resolved_colorspace = colorspace or _knob_value(submit_node, "cg_colorspace")
    inputs = extract_nuke_inputs(
        beauty_node,
        depth_node=depth_node,
        extra_aov_nodes=extra_aov_nodes,
    )

    job = build_job_request(
        show=resolved_show,
        sequence=resolved_seq,
        shot=resolved_shot,
        pass_name=resolved_pass,
        version=resolved_version,
        workflow_dir=wf_path,
        start_frame=resolved_start,
        end_frame=resolved_end,
        step=step,
        base_seed=resolved_seed,
        input_sequences=inputs,
        params=node_params,
        output_dir=Path(resolved_out),
        colorspace=str(resolved_colorspace) if resolved_colorspace else None,
        dcc_context=ctx,
    )
    return job, ctx


def format_nuke_sequence_pattern(publish_dir: Path, job: JobRequest) -> str:
    """Format the published EXR sequence path using Nuke %04d frame syntax."""
    stem = (
        f"{job.show}_{job.sequence}_{job.shot}_{job.pass_name}"
        f"_v{job.version:03d}"
    )
    return f"{publish_dir.as_posix()}/{stem}.%04d.exr"


def create_read_node_for_result(
    result: SubmissionResult,
    job: JobRequest,
    *,
    nuke_mod: Any | None = None,
) -> Any | None:
    """Create a Nuke Read node pointing to published EXRs (returns None if not passed)."""
    if (
        not result.passed
        or result.publish_result is None
        or not result.publish_result.published_files
    ):
        return None

    nuke = _resolve_nuke(nuke_mod)
    if hasattr(nuke, "nodes") and hasattr(nuke.nodes, "Read"):
        read_node = nuke.nodes.Read()
    else:
        read_node = nuke.createNode("Read", inpanel=False)

    pattern = format_nuke_sequence_pattern(result.publish_result.publish_dir, job)
    first = job.frame_range.start
    last = job.frame_range.end
    colorspace = result.publish_result.manifest.colorspace

    _set_knob(read_node, "file", pattern)
    _set_knob(read_node, "first", first)
    _set_knob(read_node, "last", last)
    _set_knob(read_node, "origfirst", first)
    _set_knob(read_node, "origlast", last)
    _set_knob(read_node, "colorspace", colorspace)
    _set_knob(
        read_node,
        "label",
        (
            f"ComfyGuard | {result.publish_result.manifest.contract_name}:"
            f"{result.publish_result.manifest.contract_version} | "
            f"seed={job.base_seed} | {job.job_id}"
        ),
    )
    return read_node


def _add_user_knob(
    node: Any,
    nuke: Any,
    knob_cls_name: str,
    name: str,
    label: str,
    default: Any,
) -> None:
    if hasattr(node, "knob") and node.knob(name) is not None:
        _set_knob(node, name, default)
        return
    knob_cls = getattr(nuke, knob_cls_name, None)
    if knob_cls is not None and hasattr(node, "addKnob"):
        knob = knob_cls(name, label)
        if hasattr(knob, "setValue"):
            knob.setValue(default)
        node.addKnob(knob)


def create_nuke_submit_node(
    *,
    name: str = "ComfyGuardSubmit1",
    nuke_mod: Any | None = None,
) -> Any:
    """Create a Nuke NoOp node populated with standard ComfyGuard submission knobs."""
    nuke = _resolve_nuke(nuke_mod)
    if hasattr(nuke, "nodes") and hasattr(nuke.nodes, "NoOp"):
        node = nuke.nodes.NoOp(name=name)
    else:
        node = nuke.createNode("NoOp", inpanel=False)
        _set_knob(node, "name", name)

    defaults: tuple[tuple[str, str, str, Any], ...] = (
        ("String_Knob", "cg_show", "Show", "NOVA"),
        ("String_Knob", "cg_sequence", "Sequence", "sq010"),
        ("String_Knob", "cg_shot", "Shot", "sh0010"),
        ("String_Knob", "cg_pass_name", "Pass Name", "relight"),
        ("Int_Knob", "cg_version", "Version", 1),
        ("String_Knob", "cg_workflow_dir", "Workflow Dir", str(_default_workflow_dir())),
        ("String_Knob", "cg_output_dir", "Output Dir", ""),
        ("Int_Knob", "cg_base_seed", "Base Seed", 42),
        ("Int_Knob", "cg_steps", "Steps", 24),
        ("Double_Knob", "cg_cfg", "CFG", 6.5),
        ("Double_Knob", "cg_denoise", "Denoise", 0.45),
        ("Double_Knob", "cg_controlnet_strength", "ControlNet Strength", 0.85),
        (
            "String_Knob",
            "cg_positive_prompt",
            "Positive Prompt",
            "cinematic rim lighting, physically plausible lookdev relight",
        ),
        (
            "String_Knob",
            "cg_negative_prompt",
            "Negative Prompt",
            "watermark, text, oversaturated, blurry",
        ),
        ("String_Knob", "cg_colorspace", "Colorspace", "ACEScg"),
    )
    for knob_cls_name, knob_name, label, default in defaults:
        _add_user_knob(node, nuke, knob_cls_name, knob_name, label, default)
    return node


def register_nuke_menu(*, nuke_mod: Any | None = None) -> Any:
    """Register ComfyGuard submission actions in Nuke's Nodes toolbar menu."""
    nuke = _resolve_nuke(nuke_mod)
    nodes_menu = nuke.menu("Nodes")
    cg_menu = nodes_menu.addMenu("ComfyGuard")
    cg_menu.addCommand(
        "Create ComfyGuard Submit Node",
        "from comfyguard.dcc.nuke_hook import create_nuke_submit_node; create_nuke_submit_node()",
    )
    cg_menu.addCommand(
        "Submit Selected Read to ComfyGuard",
        "from comfyguard.dcc.nuke_hook import submit_from_nuke; "
        "import nuke; sel = nuke.selectedNodes(); "
        "submit_from_nuke(beauty_node=sel[0], depth_node=sel[1] if len(sel) > 1 else None)",
    )
    return cg_menu


async def submit_from_nuke_async(
    *,
    beauty_node: Any,
    depth_node: Any | None = None,
    extra_aov_nodes: dict[str, Any] | None = None,
    submit_node: Any | None = None,
    endpoints: Sequence[WorkerConfig | str] = ("http://127.0.0.1:8188",),
    to_deadline: bool = False,
    job_file_path: str | Path | None = None,
    deadline_pool: str = "gpu_comfy",
    deadline_group: str = "comfyguard",
    deadline_priority: int = 50,
    upload_inputs: bool = False,
    upload_subfolder: str | None = None,
    create_read_node: bool = True,
    object_info: dict[str, Any] | None = None,
    model_hashes: dict[str, str] | None = None,
    models_dir: Path | None = None,
    skip_model_check: bool = False,
    timeout: float = 300.0,
    nuke_mod: Any | None = None,
    **job_kwargs: Any,
) -> NukeSubmissionOutcome:
    """Async Nuke submission entrypoint supporting direct broker run or Deadline export."""
    nuke = _resolve_nuke(nuke_mod)
    job, ctx = build_job_from_nuke(
        beauty_node=beauty_node,
        depth_node=depth_node,
        extra_aov_nodes=extra_aov_nodes,
        submit_node=submit_node,
        nuke_mod=nuke,
        **job_kwargs,
    )

    if to_deadline:
        target_job_file = (
            Path(job_file_path)
            if job_file_path is not None
            else job.output_dir / f"{job.job_id}_job.json"
        )
        target_job_file.parent.mkdir(parents=True, exist_ok=True)
        target_job_file.write_text(job.model_dump_json(indent=2), encoding="utf-8")
        dl_info = generate_deadline_job_info(
            job,
            pool=deadline_pool,
            group=deadline_group,
            priority=deadline_priority,
            job_file_path=target_job_file,
        )
        return NukeSubmissionOutcome(
            job_id=job.job_id,
            passed=True,
            deadline_job_info=dl_info,
        )

    res = await PipelineOrchestrator().run_job(
        job,
        endpoints=endpoints,
        object_info=object_info,
        model_hashes=model_hashes,
        models_dir=models_dir,
        skip_model_check=skip_model_check,
        timeout=timeout,
        reference_aov="depth" if "depth" in job.input_sequences else "beauty",
        dcc_source=ctx.format_source(),
        upload_inputs=upload_inputs,
        upload_subfolder=upload_subfolder,
    )

    read_name: str | None = None
    published_pattern: str | None = None
    if res.passed and res.publish_result is not None and res.publish_result.published_files:
        published_pattern = format_nuke_sequence_pattern(res.publish_result.publish_dir, job)
        if create_read_node:
            read_node = create_read_node_for_result(res, job, nuke_mod=nuke)
            if read_node is not None and hasattr(read_node, "name"):
                read_name = str(read_node.name())

    return NukeSubmissionOutcome(
        job_id=job.job_id,
        passed=res.passed,
        submission=res,
        read_node_name=read_name,
        published_pattern=published_pattern,
    )


def submit_from_nuke(**kwargs: Any) -> NukeSubmissionOutcome:
    """Synchronous Nuke submission entrypoint for shelf/menu callbacks."""
    return asyncio.run(submit_from_nuke_async(**kwargs))
