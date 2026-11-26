"""SideFX Houdini ROP/HDA and TOPs/PDG submission hooks for ComfyGuard."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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
_F_PAD_RE = re.compile(r"\$\{F(\d+)\}|\$F(\d+)")
_F_UNPADDED_RE = re.compile(r"\$\{F\}|\$F\b")
_SENTINEL_PAD_RE = re.compile(r"__CG_FRAME_PAD_(\d+)__")


class HoudiniUnavailableError(RuntimeError):
    """Raised when Houdini Python bindings ('hou') are requested outside Houdini."""


class HoudiniSubmissionOutcome(BaseModel):
    """Result returned to Houdini HDA/ROP callbacks and PDG processors."""

    job_id: str
    passed: bool
    submission: SubmissionResult | None = None
    published_pattern: str | None = None
    deadline_job_info: dict[str, str] | None = None
    updated_parms: dict[str, str] = Field(default_factory=dict)


def _resolve_hou(hou_mod: Any | None = None) -> Any:
    if hou_mod is not None:
        return hou_mod
    try:
        import hou  # type: ignore[import-not-found]
    except ImportError as exc:
        raise HoudiniUnavailableError(
            "Houdini Python API ('hou') is only available inside SideFX Houdini / hython. "
            "Pass a mock hou_mod for offline testing."
        ) from exc
    return hou


def _parm_value(node: Any, name: str, default: Any = None, *, unexpanded: bool = False) -> Any:
    if node is None or not hasattr(node, "parm"):
        return default
    parm = node.parm(name)
    if parm is None:
        return default
    if unexpanded and hasattr(parm, "unexpandedString"):
        val = parm.unexpandedString()
    elif hasattr(parm, "eval"):
        val = parm.eval()
    else:
        val = parm
    return default if val in (None, "") else val


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


def normalize_houdini_frame_pattern(raw_path: str, *, hou_mod: Any | None = None) -> str:
    """Expand $HIP/$JOB via hou.expandString while preserving $F4/$F as ####/%d."""
    protected = _F_PAD_RE.sub(
        lambda m: f"__CG_FRAME_PAD_{m.group(1) or m.group(2)}__",
        raw_path,
    )
    protected = _F_UNPADDED_RE.sub("__CG_FRAME_UNPADDED__", protected)

    if hou_mod is not None and hasattr(hou_mod, "expandString"):
        protected = str(hou_mod.expandString(protected))

    normalized = _SENTINEL_PAD_RE.sub(lambda m: "#" * int(m.group(1)), protected)
    return normalized.replace("__CG_FRAME_UNPADDED__", "%d")


def format_houdini_sequence_pattern(publish_dir: Path, job: JobRequest) -> str:
    """Format the published EXR sequence path using Houdini $F4 frame syntax."""
    stem = (
        f"{job.show}_{job.sequence}_{job.shot}_{job.pass_name}"
        f"_v{job.version:03d}"
    )
    return f"{publish_dir.as_posix()}/{stem}.$F4.exr"


def capture_houdini_context(*, hou_mod: Any | None = None) -> DCCContext:
    """Capture Houdini .hip path, version, FPS, playbar range, and Rez environment."""
    hou = _resolve_hou(hou_mod)
    raw_hip = str(hou.hipFile.path()) if hasattr(hou, "hipFile") else ""
    scene_file = "" if raw_hip == "untitled.hip" else raw_hip

    ctx = DCCContext.from_environment("houdini", scene_file=scene_file)
    if hasattr(hou, "applicationVersionString"):
        ctx.extra_metadata["houdini_version"] = str(hou.applicationVersionString())
    if hasattr(hou, "fps"):
        ctx.extra_metadata["fps"] = float(hou.fps())
    if hasattr(hou, "playbar") and hasattr(hou.playbar, "frameRange"):
        f_start, f_end = hou.playbar.frameRange()
        ctx.extra_metadata["playbar_range"] = [int(f_start), int(f_end)]
    return ctx


def extract_houdini_frame_range(
    node: Any | None = None,
    *,
    hou_mod: Any | None = None,
) -> tuple[int, int, int]:
    """Extract (start, end, step) from ROP trange/f1/f2/f3 parms or playbar range."""
    hou = _resolve_hou(hou_mod)
    if node is not None and hasattr(node, "parm"):
        trange = _parm_value(node, "trange")
        if trange is not None:
            mode = int(trange)
            if mode == 0:
                cur = int(hou.frame()) if hasattr(hou, "frame") else 1001
                return cur, cur, 1
            f1 = int(_parm_value(node, "f1", 1001))
            f2 = int(_parm_value(node, "f2", f1))
            f3 = max(1, int(_parm_value(node, "f3", 1)))
            return f1, f2, f3

    if hasattr(hou, "playbar") and hasattr(hou.playbar, "frameRange"):
        f_start, f_end = hou.playbar.frameRange()
        return int(f_start), int(f_end), 1
    cur = int(hou.frame()) if hasattr(hou, "frame") else 1001
    return cur, cur, 1


def build_job_from_houdini(
    *,
    node: Any | None = None,
    beauty_path: str | None = None,
    depth_path: str | None = None,
    extra_inputs: dict[str, str] | None = None,
    show: str | None = None,
    sequence: str | None = None,
    shot: str | None = None,
    pass_name: str | None = None,
    version: int | None = None,
    workflow_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    step: int | None = None,
    base_seed: int | None = None,
    params: dict[str, Any] | None = None,
    colorspace: str | None = None,
    hou_mod: Any | None = None,
) -> tuple[JobRequest, DCCContext]:
    """Construct a typed JobRequest and DCCContext from a Houdini ROP/HDA node."""
    hou = _resolve_hou(hou_mod)
    ctx = capture_houdini_context(hou_mod=hou)

    inferred_show, inferred_seq, inferred_shot = _infer_shot_tokens(ctx.scene_file)
    resolved_show = str(show or _parm_value(node, "cg_show") or inferred_show)
    resolved_seq = str(sequence or _parm_value(node, "cg_sequence") or inferred_seq)
    resolved_shot = str(shot or _parm_value(node, "cg_shot") or inferred_shot)
    resolved_pass = str(pass_name or _parm_value(node, "cg_pass_name") or "relight")
    resolved_version = int(
        version if version is not None else _parm_value(node, "cg_version", 1)
    )

    raw_wf = workflow_dir or _parm_value(node, "cg_workflow_dir", unexpanded=True)
    if raw_wf:
        wf_expanded = normalize_houdini_frame_pattern(str(raw_wf), hou_mod=hou)
        wf_path = Path(wf_expanded)
    else:
        wf_path = _default_workflow_dir()
    if not wf_path.is_dir():
        raise ValueError(f"Workflow directory not found: {wf_path}")

    raw_out = output_dir or _parm_value(node, "cg_output_dir", unexpanded=True)
    if not raw_out:
        raise ValueError("output_dir must be provided or set on node.parm('cg_output_dir')")
    resolved_out = Path(normalize_houdini_frame_pattern(str(raw_out), hou_mod=hou))

    rop_start, rop_end, rop_step = extract_houdini_frame_range(node, hou_mod=hou)
    resolved_start = int(start_frame if start_frame is not None else rop_start)
    resolved_end = int(end_frame if end_frame is not None else rop_end)
    resolved_step = max(1, int(step if step is not None else rop_step))
    resolved_seed = int(
        base_seed if base_seed is not None else _parm_value(node, "cg_base_seed", 42)
    )

    raw_beauty = beauty_path or _parm_value(node, "cg_beauty_path", unexpanded=True)
    if not raw_beauty:
        raise ValueError("beauty_path must be provided or set on node.parm('cg_beauty_path')")
    input_sequences: dict[str, str] = {
        "beauty": normalize_houdini_frame_pattern(str(raw_beauty), hou_mod=hou)
    }

    raw_depth = depth_path or _parm_value(node, "cg_depth_path", unexpanded=True)
    if raw_depth:
        input_sequences["depth"] = normalize_houdini_frame_pattern(str(raw_depth), hou_mod=hou)

    for aov_name, raw_aov in (extra_inputs or {}).items():
        input_sequences[aov_name] = normalize_houdini_frame_pattern(str(raw_aov), hou_mod=hou)

    node_params: dict[str, Any] = {}
    parm_map = (
        ("cg_steps", "steps", int),
        ("cg_cfg", "cfg", float),
        ("cg_denoise", "denoise", float),
        ("cg_controlnet_strength", "controlnet_strength", float),
        ("cg_positive_prompt", "positive_prompt", str),
        ("cg_negative_prompt", "negative_prompt", str),
    )
    for parm_name, param_key, caster in parm_map:
        raw_val = _parm_value(node, parm_name)
        if raw_val is not None:
            node_params[param_key] = caster(raw_val)
    if params:
        node_params.update(params)

    resolved_colorspace = colorspace or _parm_value(node, "cg_colorspace")
    job = build_job_request(
        show=resolved_show,
        sequence=resolved_seq,
        shot=resolved_shot,
        pass_name=resolved_pass,
        version=resolved_version,
        workflow_dir=wf_path,
        start_frame=resolved_start,
        end_frame=resolved_end,
        step=resolved_step,
        base_seed=resolved_seed,
        input_sequences=input_sequences,
        params=node_params,
        output_dir=resolved_out,
        colorspace=str(resolved_colorspace) if resolved_colorspace else None,
        dcc_context=ctx,
    )
    return job, ctx


def _pdg_attr(work_item: Any, name: str, default: Any = None) -> Any:
    if isinstance(work_item, dict):
        val = work_item.get(name, default)
        return default if val in (None, "") else val
    if hasattr(work_item, "attribValue"):
        try:
            val = work_item.attribValue(name)
        except (KeyError, AttributeError):
            return default
        return default if val in (None, "") else val
    return default


def build_job_from_pdg_work_item(
    work_item: Any,
    *,
    workflow_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    hou_mod: Any | None = None,
) -> tuple[JobRequest, DCCContext]:
    """Construct a JobRequest from a Houdini TOPs/PDG work item (object or dict)."""
    hou = _resolve_hou(hou_mod)
    ctx = capture_houdini_context(hou_mod=hou)
    item_tag = _pdg_attr(work_item, "name") or _pdg_attr(work_item, "index")
    if item_tag is not None:
        ctx.extra_metadata["pdg_work_item"] = str(item_tag)

    inferred_show, inferred_seq, inferred_shot = _infer_shot_tokens(ctx.scene_file)
    show = str(_pdg_attr(work_item, "show", inferred_show))
    sequence = str(_pdg_attr(work_item, "sequence", inferred_seq))
    shot = str(_pdg_attr(work_item, "shot", inferred_shot))
    pass_name = str(_pdg_attr(work_item, "pass_name", "relight"))
    version = int(_pdg_attr(work_item, "version", 1))

    frame_val = _pdg_attr(work_item, "frame")
    if frame_val is not None:
        start_frame = int(frame_val)
        end_frame = int(frame_val)
    else:
        start_frame = int(_pdg_attr(work_item, "start_frame", 1001))
        end_frame = int(_pdg_attr(work_item, "end_frame", start_frame))
    step = max(1, int(_pdg_attr(work_item, "step", 1)))
    base_seed = int(_pdg_attr(work_item, "base_seed", 42))

    raw_wf = workflow_dir or _pdg_attr(work_item, "workflow_dir")
    wf_path = (
        Path(normalize_houdini_frame_pattern(str(raw_wf), hou_mod=hou))
        if raw_wf
        else _default_workflow_dir()
    )
    raw_out = output_dir or _pdg_attr(work_item, "output_dir")
    if not raw_out:
        raise ValueError("PDG work item requires 'output_dir' argument or attribute")
    resolved_out = Path(normalize_houdini_frame_pattern(str(raw_out), hou_mod=hou))

    raw_beauty = _pdg_attr(work_item, "beauty_path") or _pdg_attr(work_item, "beauty")
    if not raw_beauty:
        raise ValueError("PDG work item requires 'beauty_path' or 'beauty' attribute")
    input_sequences: dict[str, str] = {
        "beauty": normalize_houdini_frame_pattern(str(raw_beauty), hou_mod=hou)
    }
    raw_depth = _pdg_attr(work_item, "depth_path") or _pdg_attr(work_item, "depth")
    if raw_depth:
        input_sequences["depth"] = normalize_houdini_frame_pattern(str(raw_depth), hou_mod=hou)

    raw_params = _pdg_attr(work_item, "params")
    if isinstance(raw_params, str):
        parsed_params = json.loads(raw_params)
    elif isinstance(raw_params, dict):
        parsed_params = dict(raw_params)
    else:
        parsed_params = {}

    colorspace = _pdg_attr(work_item, "colorspace")
    job = build_job_request(
        show=show,
        sequence=sequence,
        shot=shot,
        pass_name=pass_name,
        version=version,
        workflow_dir=wf_path,
        start_frame=start_frame,
        end_frame=end_frame,
        step=step,
        base_seed=base_seed,
        input_sequences=input_sequences,
        params=parsed_params,
        output_dir=resolved_out,
        colorspace=str(colorspace) if colorspace else None,
        dcc_context=ctx,
    )
    return job, ctx


def write_result_to_hda_node(
    node: Any,
    result: SubmissionResult,
    job: JobRequest,
) -> dict[str, str]:
    """Write ComfyGuard job status, published $F4.exr pattern, and manifest back to HDA parms."""
    if node is None or not hasattr(node, "parm"):
        return {}

    if not result.preflight_report.passed:
        status_label = "PREFLIGHT_REJECTED"
    elif result.passed and result.publish_result and result.publish_result.published_files:
        status_label = "PUBLISHED"
    else:
        status_label = "QUARANTINED"

    published_pattern = ""
    manifest_path = ""
    if result.publish_result is not None:
        if result.passed and result.publish_result.published_files:
            published_pattern = format_houdini_sequence_pattern(
                result.publish_result.publish_dir, job
            )
        if result.publish_result.manifest_path is not None:
            manifest_path = result.publish_result.manifest_path.as_posix()
        elif result.publish_result.quarantine_manifest_path is not None:
            manifest_path = result.publish_result.quarantine_manifest_path.as_posix()

    updates = {
        "cg_last_job_id": job.job_id,
        "cg_last_status": status_label,
        "cg_published_path": published_pattern,
        "cg_manifest_path": manifest_path,
    }
    written: dict[str, str] = {}
    for parm_name, value in updates.items():
        parm = node.parm(parm_name)
        if parm is not None and hasattr(parm, "set"):
            parm.set(value)
            written[parm_name] = value
    return written


async def submit_from_houdini_async(
    *,
    node: Any | None = None,
    work_item: Any | None = None,
    endpoints: Sequence[WorkerConfig | str] = ("http://127.0.0.1:8188",),
    to_deadline: bool = False,
    job_file_path: str | Path | None = None,
    deadline_pool: str = "gpu_comfy",
    deadline_group: str = "comfyguard",
    deadline_priority: int = 50,
    upload_inputs: bool = False,
    upload_subfolder: str | None = None,
    object_info: dict[str, Any] | None = None,
    model_hashes: dict[str, str] | None = None,
    models_dir: Path | None = None,
    skip_model_check: bool = False,
    timeout: float = 300.0,
    hou_mod: Any | None = None,
    **job_kwargs: Any,
) -> HoudiniSubmissionOutcome:
    """Async Houdini HDA/ROP or PDG submission entrypoint."""
    hou = _resolve_hou(hou_mod)
    if work_item is not None:
        job, ctx = build_job_from_pdg_work_item(
            work_item,
            workflow_dir=job_kwargs.get("workflow_dir"),
            output_dir=job_kwargs.get("output_dir"),
            hou_mod=hou,
        )
    else:
        job, ctx = build_job_from_houdini(
            node=node,
            hou_mod=hou,
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
        return HoudiniSubmissionOutcome(
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

    updated_parms = write_result_to_hda_node(node, res, job)
    published_pattern: str | None = None
    if res.passed and res.publish_result is not None and res.publish_result.published_files:
        published_pattern = format_houdini_sequence_pattern(res.publish_result.publish_dir, job)

    return HoudiniSubmissionOutcome(
        job_id=job.job_id,
        passed=res.passed,
        submission=res,
        published_pattern=published_pattern,
        updated_parms=updated_parms,
    )


def submit_from_houdini(**kwargs: Any) -> HoudiniSubmissionOutcome:
    """Synchronous Houdini HDA/PDG submission entrypoint."""
    return asyncio.run(submit_from_houdini_async(**kwargs))
