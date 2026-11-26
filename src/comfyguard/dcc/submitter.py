"""DCC/render-farm submission wrappers and end-to-end pipeline orchestrator."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from comfyguard.broker.client import ComfyUIClient, ComfyUIConnectionError
from comfyguard.broker.mutator import GraphMutator
from comfyguard.broker.pool import (
    BrokerJobSnapshot,
    JobStatus,
    RetryPolicy,
    WorkerConfig,
    WorkerPool,
)
from comfyguard.contracts.manifest import ValidationReport
from comfyguard.contracts.schema import FrameRange, JobRequest
from comfyguard.publish.publisher import FramePublishInput, Publisher, PublishResult
from comfyguard.validation.postflight import (
    PostflightFrameInput,
    PostflightValidator,
)
from comfyguard.validation.preflight import PreflightValidator, inspect_image_header


class DCCContext(BaseModel):
    """Runtime context captured from Nuke, Houdini, Maya, Deadline, or Rez."""

    dcc_app: Literal["nuke", "houdini", "maya", "deadline", "standalone"] = "standalone"
    scene_file: str = ""
    artist: str = ""
    rez_packages: list[str] = Field(default_factory=list)
    extra_metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_environment(
        cls,
        dcc_app: Literal["nuke", "houdini", "maya", "deadline", "standalone"] = "standalone",
        *,
        scene_file: str = "",
        artist: str = "",
    ) -> DCCContext:
        """Capture artist, scene, and resolved Rez packages from environment variables."""
        resolved_env = os.environ.get("REZ_USED_RESOLVE", "").strip()
        rez_packages = resolved_env.split() if resolved_env else []
        resolved_artist = artist or os.environ.get("USERNAME") or os.environ.get("USER") or ""
        return cls(
            dcc_app=dcc_app,
            scene_file=scene_file,
            artist=resolved_artist,
            rez_packages=rez_packages,
        )

    def format_source(self) -> str:
        """Format concise DCC provenance string for LineageManifest."""
        if self.scene_file:
            return f"{self.dcc_app}:{self.scene_file}"
        return self.dcc_app


class SubmissionResult(BaseModel):
    """End-to-end result covering pre-flight, broker execution, post-flight, and publishing."""

    job_id: str
    preflight_report: ValidationReport
    postflight_report: ValidationReport | None = None
    publish_result: PublishResult | None = None
    broker_snapshots: list[BrokerJobSnapshot] = Field(default_factory=list)
    passed: bool


def build_job_request(
    *,
    show: str,
    sequence: str,
    shot: str,
    pass_name: str,
    version: int,
    workflow_dir: str | Path,
    start_frame: int,
    end_frame: int,
    input_sequences: dict[str, str],
    output_dir: str | Path,
    base_seed: int = 42,
    params: dict[str, Any] | None = None,
    colorspace: str | None = None,
    step: int = 1,
    dcc_context: DCCContext | None = None,
) -> JobRequest:
    """Build a typed JobRequest enriched with optional DCC and Rez context."""
    ctx = dcc_context or DCCContext.from_environment()
    metadata = dict(ctx.extra_metadata)
    if ctx.artist:
        metadata["artist"] = ctx.artist
    if ctx.scene_file:
        metadata["scene_file"] = ctx.scene_file
    metadata["dcc_app"] = ctx.dcc_app

    return JobRequest(
        show=show,
        sequence=sequence,
        shot=shot,
        pass_name=pass_name,
        version=version,
        workflow_dir=Path(workflow_dir),
        frame_range=FrameRange(start=start_frame, end=end_frame, step=step),
        base_seed=base_seed,
        input_sequences=input_sequences,
        params=params or {},
        output_dir=Path(output_dir),
        colorspace=colorspace,
        rez_packages=list(ctx.rez_packages),
        metadata=metadata,
    )


def generate_deadline_job_info(
    job: JobRequest,
    *,
    pool: str = "gpu_comfy",
    group: str = "comfyguard",
    priority: int = 50,
    job_file_path: str | Path = "job.json",
) -> dict[str, str]:
    """Generate a Thinkbox Deadline job_info dictionary for farm submission."""
    publish_dir = Publisher().resolve_publish_dir(job)
    return {
        "Plugin": "CommandLine",
        "Name": f"{job.show}_{job.sequence}_{job.shot}_{job.pass_name}_v{job.version:03d}",
        "BatchName": f"{job.show}_{job.sequence}_{job.shot}",
        "Frames": job.frame_range.format_range(),
        "Pool": pool,
        "Group": group,
        "Priority": str(priority),
        "Executable": "comfyguard",
        "Arguments": f"submit --job {job_file_path}",
        "OutputDirectory0": str(publish_dir),
        "ExtraInfo0": f"comfyguard:{job.job_id}",
    }


def _worker_url_map(endpoints: Sequence[WorkerConfig | str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for idx, entry in enumerate(endpoints):
        if isinstance(entry, str):
            mapping[f"worker-{idx}"] = entry.rstrip("/")
        else:
            mapping[entry.name] = entry.base_url.rstrip("/")
    return mapping


class PipelineOrchestrator:
    """Coordinates Pre-Flight -> Graph Mutation -> WorkerPool -> Post-Flight -> Publisher."""

    async def run_job(
        self,
        job: JobRequest,
        *,
        endpoints: Sequence[WorkerConfig | str],
        staging_dir: Path | None = None,
        object_info: dict[str, Any] | None = None,
        model_hashes: dict[str, str] | None = None,
        models_dir: Path | None = None,
        skip_model_check: bool = False,
        retry_policy: RetryPolicy | None = None,
        timeout: float = 300.0,
        reference_aov: str | None = "depth",
        atomic_job_quarantine: bool = False,
        dcc_source: str | None = None,
    ) -> SubmissionResult:
        """Execute a complete contract-gated ComfyGuard job."""
        mutator = GraphMutator.from_workflow_dir(job.workflow_dir)
        worker_urls = _worker_url_map(endpoints)

        resolved_object_info = object_info
        if resolved_object_info is None and not skip_model_check and endpoints:
            first_url = next(iter(worker_urls.values()))
            try:
                async with ComfyUIClient(first_url) as client:
                    resolved_object_info = await client.get_object_info()
            except ComfyUIConnectionError:
                resolved_object_info = None

        preflight_report = PreflightValidator().validate(
            job=job,
            contract=mutator.contract,
            template_graph=mutator.template_graph,
            object_info=resolved_object_info,
            model_hashes=model_hashes,
            models_dir=models_dir,
            skip_model_check=skip_model_check,
        )
        if not preflight_report.passed:
            return SubmissionResult(
                job_id=job.job_id,
                preflight_report=preflight_report,
                passed=False,
            )

        mutated_frames = mutator.mutate_for_job(job)

        async with WorkerPool(
            endpoints,
            default_timeout=timeout,
            retry_policy=retry_policy,
        ) as pool:
            snapshots = await pool.submit_batch(mutated_frames, timeout=timeout)

        temp_ctx: tempfile.TemporaryDirectory[str] | None = None
        if staging_dir is None:
            temp_ctx = tempfile.TemporaryDirectory(prefix=f"comfyguard_{job.job_id}_")
            active_staging = Path(temp_ctx.name)
        else:
            active_staging = Path(staging_dir) / job.job_id
            active_staging.mkdir(parents=True, exist_ok=True)

        try:
            mutated_by_frame = {m.frame: m for m in mutated_frames}
            staged_paths: dict[int, Path] = {}

            clients: dict[str, ComfyUIClient] = {}
            try:
                for snap in snapshots:
                    assert snap.frame is not None
                    if (
                        snap.status != JobStatus.COMPLETED
                        or snap.result is None
                        or not snap.result.outputs
                    ):
                        staged_paths[snap.frame] = active_staging / f"failed_{snap.frame:04d}.png"
                        continue

                    worker_name = snap.assigned_worker or next(iter(worker_urls))
                    worker_url = worker_urls.get(worker_name, next(iter(worker_urls.values())))
                    if worker_url not in clients:
                        clients[worker_url] = ComfyUIClient(worker_url)
                    artifact = snap.result.outputs[0]
                    raw_bytes = await clients[worker_url].fetch_output_bytes(artifact)
                    suffix = Path(artifact.filename).suffix or ".png"
                    local_file = active_staging / f"staged.{snap.frame:04d}{suffix}"
                    local_file.write_bytes(raw_bytes)
                    staged_paths[snap.frame] = local_file
            finally:
                for client in clients.values():
                    await client.close()

            expected_resolution = _detect_expected_resolution(job)
            postflight_inputs: list[PostflightFrameInput] = []
            publish_inputs: list[FramePublishInput] = []

            for snap in snapshots:
                assert snap.frame is not None
                frame = snap.frame
                mutated = mutated_by_frame[frame]
                staged_file = staged_paths[frame]
                ref_path = (
                    job.resolve_input_frame(reference_aov, frame)
                    if reference_aov and reference_aov in job.input_sequences
                    else None
                )
                postflight_inputs.append(
                    PostflightFrameInput(
                        frame=frame,
                        output_path=staged_file,
                        reference_path=ref_path,
                    )
                )
                publish_inputs.append(
                    FramePublishInput(
                        frame=frame,
                        seed=mutated.seed,
                        graph_sha256=mutated.graph_sha256,
                        source_path=staged_file,
                        input_files=mutated.input_files,
                        prompt_id=snap.prompt_id,
                    )
                )

            postflight_res = PostflightValidator().validate_sequence(
                postflight_inputs,
                mutator.contract,
                expected_resolution=expected_resolution,
            )
            publish_res = Publisher().publish_job(
                job=job,
                contract=mutator.contract,
                workflow_sha256=mutator.template_sha256,
                frames=publish_inputs,
                postflight_result=postflight_res,
                preflight_report=preflight_report,
                graph_snapshot=mutator.template_graph,
                dcc_source=dcc_source or str(job.metadata.get("dcc_app", "standalone")),
                atomic_job_quarantine=atomic_job_quarantine,
            )

            all_completed = all(s.status == JobStatus.COMPLETED for s in snapshots)
            return SubmissionResult(
                job_id=job.job_id,
                preflight_report=preflight_report,
                postflight_report=postflight_res.report,
                publish_result=publish_res,
                broker_snapshots=snapshots,
                passed=preflight_report.passed and postflight_res.report.passed and all_completed,
            )
        finally:
            if temp_ctx is not None:
                temp_ctx.cleanup()


def _detect_expected_resolution(job: JobRequest) -> tuple[int, int] | None:
    first_frame = job.frame_range.frames()[0]
    for aov_name in ("beauty", *sorted(job.input_sequences)):
        if aov_name in job.input_sequences:
            path = job.resolve_input_frame(aov_name, first_frame)
            if path.is_file():
                info = inspect_image_header(path)
                return (info.width, info.height)
    return None


def submit_from_dcc(
    job: JobRequest,
    *,
    endpoints: Sequence[WorkerConfig | str],
    dcc_context: DCCContext | None = None,
    **kwargs: Any,
) -> SubmissionResult:
    """Synchronous submission entry point for Nuke, Houdini, Maya, and Deadline scripts."""
    dcc_source = dcc_context.format_source() if dcc_context else None
    return asyncio.run(
        PipelineOrchestrator().run_job(
            job,
            endpoints=endpoints,
            dcc_source=dcc_source,
            **kwargs,
        )
    )
