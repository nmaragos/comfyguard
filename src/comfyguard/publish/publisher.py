"""Versioned EXR publisher, metadata stamper, and automatic quarantine router."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from comfyguard.contracts.manifest import FrameLineage, LineageManifest, ValidationReport
from comfyguard.contracts.schema import JobRequest, WorkflowContract
from comfyguard.publish.exr_io import load_image_buffer, write_exr
from comfyguard.validation.postflight import PostflightResult


class FramePublishInput(BaseModel):
    """Per-frame generated output and lineage metadata ready for publishing/quarantine."""

    frame: int
    seed: int
    graph_sha256: str
    source_path: Path
    input_files: dict[str, str] = Field(default_factory=dict)
    prompt_id: str | None = None


class PublishResult(BaseModel):
    """Summary of published EXR assets, quarantined files, and written lineage manifests."""

    job_id: str
    publish_dir: Path
    quarantine_dir: Path
    published_files: dict[int, Path] = Field(default_factory=dict)
    quarantined_files: dict[int, Path] = Field(default_factory=dict)
    manifest_path: Path | None = None
    quarantine_manifest_path: Path | None = None
    manifest: LineageManifest


class Publisher:
    """Publishes validated outputs as stamped versioned EXRs and quarantines failures."""

    def resolve_publish_dir(self, job: JobRequest) -> Path:
        """Compute canonical shot/pass/version directory for approved outputs."""
        return (
            job.output_dir
            / job.show
            / job.sequence
            / job.shot
            / job.pass_name
            / f"v{job.version:03d}"
        )

    def resolve_quarantine_dir(self, job: JobRequest) -> Path:
        """Compute isolated quarantine directory for failed frames."""
        return job.output_dir / "_quarantine" / job.job_id

    def publish_job(
        self,
        job: JobRequest,
        contract: WorkflowContract,
        workflow_sha256: str,
        frames: Sequence[FramePublishInput],
        postflight_result: PostflightResult,
        *,
        preflight_report: ValidationReport | None = None,
        graph_snapshot: dict[str, Any] | None = None,
        dcc_source: str | None = None,
        atomic_job_quarantine: bool = False,
    ) -> PublishResult:
        """Stamp approved EXRs + write LineageManifest; isolate rejected frames to quarantine."""
        publish_dir = self.resolve_publish_dir(job)
        quarantine_dir = self.resolve_quarantine_dir(job)
        colorspace = job.colorspace or contract.colorspace

        published_files: dict[int, Path] = {}
        quarantined_files: dict[int, Path] = {}
        frame_lineages: list[FrameLineage] = []

        ordered_frames = sorted(frames, key=lambda item: item.frame)
        failed_map = postflight_result.failed_frames
        quarantine_all = atomic_job_quarantine and bool(failed_map)

        for item in ordered_frames:
            reasons = list(failed_map.get(item.frame, []))
            if quarantine_all and not reasons:
                reasons = ["Quarantined by atomic_job_quarantine due to sibling frame failure"]

            stem = (
                f"{job.show}_{job.sequence}_{job.shot}_{job.pass_name}"
                f"_v{job.version:03d}.{item.frame:04d}"
            )

            if reasons:
                quarantine_dir.mkdir(parents=True, exist_ok=True)
                suffix = item.source_path.suffix or ".exr"
                dest_q = quarantine_dir / f"{stem}{suffix}"
                if item.source_path.is_file():
                    shutil.copy2(item.source_path, dest_q)
                else:
                    dest_q.write_bytes(b"")
                quarantined_files[item.frame] = dest_q
                frame_lineages.append(
                    FrameLineage(
                        frame=item.frame,
                        seed=item.seed,
                        graph_sha256=item.graph_sha256,
                        input_files=item.input_files,
                        output_file=str(dest_q),
                        quarantined=True,
                        quarantine_reason="; ".join(reasons),
                        prompt_id=item.prompt_id,
                    )
                )
                continue

            publish_dir.mkdir(parents=True, exist_ok=True)
            pixels, channels = load_image_buffer(item.source_path)
            exr_attrs = {
                "comfyguard:graph_hash": item.graph_sha256,
                "comfyguard:seed": str(item.seed),
                "comfyguard:colorspace": colorspace,
                "comfyguard:job_id": job.job_id,
                "comfyguard:contract": f"{contract.name}:{contract.version}",
                "comfyguard:frame": str(item.frame),
                "comfyguard:shot": f"{job.show}/{job.sequence}/{job.shot}",
            }
            dest_exr = publish_dir / f"{stem}.exr"
            write_exr(dest_exr, pixels, channels=channels, attributes=exr_attrs)
            published_files[item.frame] = dest_exr
            frame_lineages.append(
                FrameLineage(
                    frame=item.frame,
                    seed=item.seed,
                    graph_sha256=item.graph_sha256,
                    input_files=item.input_files,
                    output_file=str(dest_exr),
                    quarantined=False,
                    prompt_id=item.prompt_id,
                )
            )

        manifest = LineageManifest(
            job_id=job.job_id,
            show=job.show,
            sequence=job.sequence,
            shot=job.shot,
            pass_name=job.pass_name,
            version=job.version,
            contract_name=contract.name,
            contract_version=contract.version,
            workflow_sha256=workflow_sha256,
            base_seed=job.base_seed,
            colorspace=colorspace,
            rez_packages=job.rez_packages,
            dcc_source=dcc_source,
            preflight_report=preflight_report,
            postflight_report=postflight_result.report,
            frames=frame_lineages,
            graph_snapshot=graph_snapshot or {},
            metadata=job.metadata,
        )

        manifest_path: Path | None = None
        if published_files:
            manifest_path = publish_dir / "lineage_manifest.json"
            manifest.to_json_file(manifest_path)

        quarantine_manifest_path: Path | None = None
        if quarantined_files:
            quarantine_manifest_path = quarantine_dir / "lineage_manifest.json"
            manifest.to_json_file(quarantine_manifest_path)

        return PublishResult(
            job_id=job.job_id,
            publish_dir=publish_dir,
            quarantine_dir=quarantine_dir,
            published_files=published_files,
            quarantined_files=quarantined_files,
            manifest_path=manifest_path,
            quarantine_manifest_path=quarantine_manifest_path,
            manifest=manifest,
        )
