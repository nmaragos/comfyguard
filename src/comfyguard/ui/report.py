"""Visual view-models, workspace scanner, and self-contained HTML report builder."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from comfyguard.broker.mutator import GraphDiff
from comfyguard.contracts.manifest import LineageManifest
from comfyguard.contracts.schema import JobRequest
from comfyguard.dcc.submitter import SubmissionResult
from comfyguard.publish.exr_io import EXRIOError, read_exr_attributes
from comfyguard.ui.templates import STATIC_REPORT_HTML
from comfyguard.ui.visuals import (
    image_path_to_data_uri,
    render_edge_overlay_data_uri,
    render_temporal_diff_data_uri,
)
from comfyguard.validation.postflight import PostflightResult
from comfyguard.validation.preflight import ImageInspectionError, inspect_image_header


class FrameVisualRecord(BaseModel):
    """Visual inspection record for a single frame in a job."""

    frame: int
    seed: int
    graph_sha256: str
    status: Literal["published", "quarantined", "rejected"]
    quarantine_reason: str | None = None
    resolution: str = ""
    input_preview: str | None = None
    output_preview: str | None = None
    edge_overlay: str | None = None
    temporal_diff_preview: str | None = None
    edge_iou: float | None = None
    temporal_delta: float | None = None
    exr_attributes: dict[str, str] = Field(default_factory=dict)
    output_file: str | None = None


class JobVisualRecord(BaseModel):
    """Complete job visual summary for the dashboard and offline HTML report."""

    job_id: str
    title: str
    scenario_tag: str = "JOB"
    status: Literal["PUBLISHED", "QUARANTINED", "PREFLIGHT_REJECTED"]
    shot_label: str
    pass_label: str
    colorspace: str
    contract_label: str
    dcc_source: str = "standalone"
    rez_packages: list[str] = Field(default_factory=list)
    gpu_prompts_dispatched: int = 0
    preflight_report: dict[str, Any] | None = None
    postflight_report: dict[str, Any] | None = None
    frames: list[FrameVisualRecord] = Field(default_factory=list)
    publish_dir: str | None = None
    quarantine_dir: str | None = None
    manifest: dict[str, Any] | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class GraphDiffRecord(BaseModel):
    """Serializable workflow graph diff record for UI rendering."""

    v1_label: str = "v001"
    v2_label: str = "v002"
    v1_sha256: str
    v2_sha256: str
    is_identical: bool
    added_nodes: dict[str, str] = Field(default_factory=dict)
    removed_nodes: dict[str, str] = Field(default_factory=dict)
    class_changes: list[dict[str, Any]] = Field(default_factory=list)
    modified_inputs: list[dict[str, Any]] = Field(default_factory=list)
    human_text: str

    @classmethod
    def from_graph_diff(
        cls,
        diff: GraphDiff,
        *,
        v1_label: str = "v001",
        v2_label: str = "v002",
    ) -> GraphDiffRecord:
        return cls(
            v1_label=v1_label,
            v2_label=v2_label,
            v1_sha256=diff.v1_sha256,
            v2_sha256=diff.v2_sha256,
            is_identical=diff.is_identical,
            added_nodes=dict(diff.added_nodes),
            removed_nodes=dict(diff.removed_nodes),
            class_changes=[c.model_dump(mode="json") for c in diff.class_changes],
            modified_inputs=[m.model_dump(mode="json") for m in diff.modified_inputs],
            human_text=diff.format_human(),
        )


class ReportPayload(BaseModel):
    """Top-level payload embedded in static HTML reports and API exports."""

    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    title: str = "ComfyGuard Pipeline Validation & Lineage Report"
    jobs: list[JobVisualRecord] = Field(default_factory=list)
    graph_diff: GraphDiffRecord | None = None


def _safe_preview(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        return image_path_to_data_uri(path)
    except (EXRIOError, OSError):
        return None


def _safe_resolution(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        info = inspect_image_header(path)
        return f"{info.width}x{info.height}"
    except ImageInspectionError:
        return ""


def _safe_exr_attrs(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file() or path.suffix.lower() != ".exr":
        return {}
    try:
        return read_exr_attributes(path)
    except EXRIOError:
        return {}


def build_job_record_from_submission(
    result: SubmissionResult,
    job: JobRequest,
    *,
    title: str,
    scenario_tag: str,
    gpu_prompts_dispatched: int,
    postflight_result: PostflightResult | None = None,
    reference_aov: str = "depth",
    contract_label: str = "lookdev_relight:v001",
) -> JobVisualRecord:
    """Convert a SubmissionResult + JobRequest into a rich visual inspection record."""
    shot_label = f"{job.show} / {job.sequence} / {job.shot}"
    pass_label = f"{job.pass_name} v{job.version:03d}"
    colorspace = job.colorspace or "ACEScg"

    if not result.preflight_report.passed:
        rejected_frames: list[FrameVisualRecord] = []
        for frame in job.frame_range.frames():
            in_path = (
                job.resolve_input_frame("beauty", frame)
                if "beauty" in job.input_sequences
                else None
            )
            rejected_frames.append(
                FrameVisualRecord(
                    frame=frame,
                    seed=job.base_seed + frame,
                    graph_sha256="not-mutated",
                    status="rejected",
                    quarantine_reason="Blocked at pre-flight gate (0 GPU prompts dispatched)",
                    resolution=_safe_resolution(in_path),
                    input_preview=_safe_preview(in_path),
                )
            )
        return JobVisualRecord(
            job_id=result.job_id,
            title=title,
            scenario_tag=scenario_tag,
            status="PREFLIGHT_REJECTED",
            shot_label=shot_label,
            pass_label=pass_label,
            colorspace=colorspace,
            contract_label=contract_label,
            dcc_source=str(job.metadata.get("dcc_app", "standalone")),
            rez_packages=list(job.rez_packages),
            gpu_prompts_dispatched=0,
            preflight_report=result.preflight_report.model_dump(mode="json"),
            frames=rejected_frames,
        )

    pub = result.publish_result
    assert pub is not None
    manifest = pub.manifest
    metrics_map = postflight_result.frame_metrics if postflight_result else {}

    frame_records: list[FrameVisualRecord] = []
    prev_out_path: Path | None = None
    for frame_lineage in manifest.frames:
        frame = frame_lineage.frame
        out_path = Path(frame_lineage.output_file) if frame_lineage.output_file else None
        in_beauty = (
            Path(frame_lineage.input_files["beauty"])
            if "beauty" in frame_lineage.input_files
            else None
        )
        ref_path = (
            Path(frame_lineage.input_files[reference_aov])
            if reference_aov in frame_lineage.input_files
            else None
        )

        edge_overlay: str | None = None
        edge_iou: float | None = None
        if (
            out_path is not None
            and ref_path is not None
            and out_path.is_file()
            and ref_path.is_file()
        ):
            try:
                edge_overlay, edge_iou = render_edge_overlay_data_uri(out_path, ref_path)
            except (EXRIOError, OSError):
                edge_overlay = None

        metric = metrics_map.get(frame)
        if metric is not None and metric.edge_iou is not None:
            edge_iou = metric.edge_iou

        temp_diff_uri: str | None = None
        temp_delta: float | None = metric.temporal_delta if metric else None
        if (
            prev_out_path is not None
            and out_path is not None
            and prev_out_path.is_file()
            and out_path.is_file()
        ):
            try:
                temp_diff_uri, calc_delta = render_temporal_diff_data_uri(prev_out_path, out_path)
                if temp_delta is None:
                    temp_delta = calc_delta
            except (EXRIOError, OSError):
                temp_diff_uri = None
        if out_path is not None and out_path.is_file():
            prev_out_path = out_path

        frame_records.append(
            FrameVisualRecord(
                frame=frame,
                seed=frame_lineage.seed,
                graph_sha256=frame_lineage.graph_sha256,
                status="quarantined" if frame_lineage.quarantined else "published",
                quarantine_reason=frame_lineage.quarantine_reason,
                resolution=_safe_resolution(out_path or in_beauty),
                input_preview=_safe_preview(in_beauty),
                output_preview=_safe_preview(out_path),
                edge_overlay=edge_overlay,
                temporal_diff_preview=temp_diff_uri,
                edge_iou=edge_iou,
                temporal_delta=temp_delta,
                exr_attributes=_safe_exr_attrs(out_path),
                output_file=str(out_path) if out_path else None,
            )
        )

    status: Literal["PUBLISHED", "QUARANTINED", "PREFLIGHT_REJECTED"] = (
        "PUBLISHED" if result.passed and not pub.quarantined_files else "QUARANTINED"
    )
    return JobVisualRecord(
        job_id=result.job_id,
        title=title,
        scenario_tag=scenario_tag,
        status=status,
        shot_label=shot_label,
        pass_label=pass_label,
        colorspace=manifest.colorspace,
        contract_label=f"{manifest.contract_name}:{manifest.contract_version}",
        dcc_source=manifest.dcc_source or "standalone",
        rez_packages=list(manifest.rez_packages),
        gpu_prompts_dispatched=gpu_prompts_dispatched,
        preflight_report=result.preflight_report.model_dump(mode="json"),
        postflight_report=(
            result.postflight_report.model_dump(mode="json") if result.postflight_report else None
        ),
        frames=frame_records,
        publish_dir=str(pub.publish_dir) if pub.published_files else None,
        quarantine_dir=str(pub.quarantine_dir) if pub.quarantined_files else None,
        manifest=manifest.model_dump(mode="json"),
    )


def scan_workspace_jobs(root_dir: str | Path) -> list[JobVisualRecord]:
    """Discover published and quarantined LineageManifest files under a workspace directory."""
    root = Path(root_dir)
    if not root.is_dir():
        return []

    manifests_by_job: dict[str, tuple[Path, LineageManifest]] = {}
    for manifest_file in sorted(root.rglob("lineage_manifest.json")):
        try:
            manifest = LineageManifest.from_json_file(manifest_file)
        except Exception:
            continue
        manifests_by_job.setdefault(manifest.job_id, (manifest_file, manifest))

    records: list[JobVisualRecord] = []
    for manifest_file, manifest in manifests_by_job.values():
        frame_records: list[FrameVisualRecord] = []
        for frame_entry in manifest.frames:
            out_path = Path(frame_entry.output_file) if frame_entry.output_file else None
            in_beauty = (
                Path(frame_entry.input_files["beauty"])
                if "beauty" in frame_entry.input_files
                else None
            )
            ref_path = (
                Path(frame_entry.input_files["depth"])
                if "depth" in frame_entry.input_files
                else None
            )
            edge_overlay: str | None = None
            edge_iou: float | None = None
            if (
                out_path is not None
                and ref_path is not None
                and out_path.is_file()
                and ref_path.is_file()
            ):
                with contextlib.suppress(EXRIOError, OSError):
                    edge_overlay, edge_iou = render_edge_overlay_data_uri(out_path, ref_path)

            frame_records.append(
                FrameVisualRecord(
                    frame=frame_entry.frame,
                    seed=frame_entry.seed,
                    graph_sha256=frame_entry.graph_sha256,
                    status="quarantined" if frame_entry.quarantined else "published",
                    quarantine_reason=frame_entry.quarantine_reason,
                    resolution=_safe_resolution(out_path or in_beauty),
                    input_preview=_safe_preview(in_beauty),
                    output_preview=_safe_preview(out_path),
                    edge_overlay=edge_overlay,
                    edge_iou=edge_iou,
                    exr_attributes=_safe_exr_attrs(out_path),
                    output_file=frame_entry.output_file,
                )
            )

        has_quarantine = any(f.quarantined for f in manifest.frames)
        status: Literal["PUBLISHED", "QUARANTINED", "PREFLIGHT_REJECTED"] = (
            "QUARANTINED" if has_quarantine else "PUBLISHED"
        )
        records.append(
            JobVisualRecord(
                job_id=manifest.job_id,
                title=(
                    f"{manifest.show} {manifest.shot} "
                    f"{manifest.pass_name} v{manifest.version:03d}"
                ),
                scenario_tag="DISK",
                status=status,
                shot_label=f"{manifest.show} / {manifest.sequence} / {manifest.shot}",
                pass_label=f"{manifest.pass_name} v{manifest.version:03d}",
                colorspace=manifest.colorspace,
                contract_label=f"{manifest.contract_name}:{manifest.contract_version}",
                dcc_source=manifest.dcc_source or "standalone",
                rez_packages=list(manifest.rez_packages),
                gpu_prompts_dispatched=len(manifest.frames),
                preflight_report=(
                    manifest.preflight_report.model_dump(mode="json")
                    if manifest.preflight_report
                    else None
                ),
                postflight_report=(
                    manifest.postflight_report.model_dump(mode="json")
                    if manifest.postflight_report
                    else None
                ),
                frames=frame_records,
                publish_dir=str(manifest_file.parent) if not has_quarantine else None,
                quarantine_dir=str(manifest_file.parent) if has_quarantine else None,
                manifest=manifest.model_dump(mode="json"),
                created_at=manifest.created_at.isoformat(),
            )
        )

    return records


def render_static_report_html(payload: ReportPayload) -> str:
    """Render a self-contained offline HTML report with embedded PNG data URIs."""
    serialized = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)
    safe_json = serialized.replace("</", "<\\/")
    return STATIC_REPORT_HTML.replace("__REPORT_DATA_JSON__", safe_json)


def export_report(payload: ReportPayload, output_path: str | Path) -> Path:
    """Write a self-contained static HTML visual report to disk."""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_static_report_html(payload), encoding="utf-8")
    return target
