"""Typer CLI entrypoint for ComfyGuard."""

from __future__ import annotations

import asyncio
import contextlib
import json
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from comfyguard.broker.client import ComfyUIClient, ComfyUIConnectionError
from comfyguard.broker.mutator import GraphMutator, graph_diff
from comfyguard.broker.smoke import (
    DEFAULT_LOCAL_ENDPOINTS,
    discover_live_endpoint,
    probe_worker,
    run_smoke_test,
)
from comfyguard.contracts.manifest import LineageManifest, ValidationReport
from comfyguard.contracts.schema import JobRequest
from comfyguard.dcc.submitter import PipelineOrchestrator
from comfyguard.publish.exr_io import EXRIOError, read_exr_attributes
from comfyguard.ui.server import DashboardServer
from comfyguard.validation.preflight import (
    ImageInspectionError,
    PreflightValidator,
    inspect_image_header,
)

app = typer.Typer(
    name="comfyguard",
    help="Deterministic ComfyUI Pipeline Broker & Contract Gatekeeper.",
    no_args_is_help=True,
)


def _load_job_file(job_path: Path) -> JobRequest:
    if not job_path.is_file():
        typer.echo(f"Error: Job file not found: {job_path}", err=True)
        raise typer.Exit(code=2)
    raw_text = job_path.read_text(encoding="utf-8")
    if job_path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(raw_text)
    else:
        data = json.loads(raw_text)
    return JobRequest.model_validate(data)


def _print_report(report: ValidationReport) -> None:
    typer.echo(report.summary())
    for check in report.checks:
        tag = check.status.value.upper()
        frame_tag = f" [f{check.frame}]" if check.frame is not None else ""
        typer.echo(f"  [{tag}] {check.check_name}{frame_tag}: {check.message}")


async def _fetch_object_info(endpoint: str) -> dict[str, Any] | None:
    try:
        async with ComfyUIClient(endpoint) as client:
            return await client.get_object_info()
    except ComfyUIConnectionError:
        return None


@app.callback()
def main() -> None:
    """ComfyGuard CLI command group."""


@app.command()
def version() -> None:
    """Print the installed ComfyGuard version."""
    from comfyguard import __version__

    typer.echo(f"comfyguard {__version__}")


@app.command()
def validate(
    job: Annotated[Path, typer.Option("--job", help="Path to job JSON or YAML file.")],
    object_info: Annotated[
        Path | None,
        typer.Option("--object-info", help="Optional cached /object_info JSON payload."),
    ] = None,
    endpoint: Annotated[
        str | None,
        typer.Option("--endpoint", help="ComfyUI worker URL to query /object_info."),
    ] = None,
    models_dir: Annotated[
        Path | None,
        typer.Option("--models-dir", help="Local ComfyUI models directory for SHA-256 checks."),
    ] = None,
    skip_model_check: Annotated[
        bool,
        typer.Option("--skip-model-check", help="Skip /object_info and model hash checks."),
    ] = False,
) -> None:
    """Run pre-flight contract validation without dispatching GPU work."""
    job_req = _load_job_file(job)
    mutator = GraphMutator.from_workflow_dir(job_req.workflow_dir)

    obj_info_data: dict[str, Any] | None = None
    if object_info is not None:
        obj_info_data = json.loads(object_info.read_text(encoding="utf-8"))
    elif endpoint is not None and not skip_model_check:
        obj_info_data = asyncio.run(_fetch_object_info(endpoint))

    report = PreflightValidator().validate(
        job=job_req,
        contract=mutator.contract,
        template_graph=mutator.template_graph,
        object_info=obj_info_data,
        models_dir=models_dir,
        skip_model_check=skip_model_check,
    )
    _print_report(report)
    if not report.passed:
        raise typer.Exit(code=1)


@app.command()
def submit(
    job: Annotated[Path, typer.Option("--job", help="Path to job JSON or YAML file.")],
    endpoint: Annotated[
        list[str] | None,
        typer.Option("--endpoint", help="ComfyUI worker URL (repeatable)."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate and mutate graphs without dispatching."),
    ] = False,
    models_dir: Annotated[
        Path | None,
        typer.Option("--models-dir", help="Local models directory for SHA-256 verification."),
    ] = None,
    skip_model_check: Annotated[
        bool,
        typer.Option("--skip-model-check", help="Skip /object_info and model hash checks."),
    ] = False,
    atomic_quarantine: Annotated[
        bool,
        typer.Option("--atomic-quarantine", help="Quarantine entire job if any frame fails."),
    ] = False,
    upload_inputs: Annotated[
        bool,
        typer.Option(
            "--upload-inputs/--no-upload-inputs",
            help="Stage local input frames via POST /upload/image before dispatch.",
        ),
    ] = False,
    upload_subfolder: Annotated[
        str | None,
        typer.Option(
            "--upload-subfolder",
            help="Optional ComfyUI input subfolder prefix when --upload-inputs is enabled.",
        ),
    ] = None,
) -> None:
    """Validate, execute on ComfyUI worker(s), post-flight gate, and publish or quarantine."""
    job_req = _load_job_file(job)
    endpoints = endpoint or ["http://127.0.0.1:8188"]

    if dry_run:
        mutator = GraphMutator.from_workflow_dir(job_req.workflow_dir)
        report = PreflightValidator().validate(
            job=job_req,
            contract=mutator.contract,
            template_graph=mutator.template_graph,
            models_dir=models_dir,
            skip_model_check=skip_model_check or endpoint is None,
        )
        _print_report(report)
        if not report.passed:
            raise typer.Exit(code=1)
        mutated = mutator.mutate_for_job(job_req)
        typer.echo(f"Dry-run OK: prepared {len(mutated)} deterministic frame graph(s):")
        for frame_graph in mutated:
            typer.echo(
                f"  frame={frame_graph.frame} seed={frame_graph.seed} "
                f"sha256={frame_graph.graph_sha256[:12]}"
            )
        return

    try:
        res = asyncio.run(
            PipelineOrchestrator().run_job(
                job_req,
                endpoints=endpoints,
                models_dir=models_dir,
                skip_model_check=skip_model_check,
                atomic_job_quarantine=atomic_quarantine,
                dcc_source="cli",
                upload_inputs=upload_inputs,
                upload_subfolder=upload_subfolder,
            )
        )
    except ComfyUIConnectionError as exc:
        typer.echo(f"Error communicating with ComfyUI worker(s): {exc}", err=True)
        raise typer.Exit(code=1) from exc

    _print_report(res.preflight_report)
    if res.staged_inputs:
        total_uploads = sum(len(aovs) for aovs in res.staged_inputs.values())
        typer.echo(f"Staged inputs: {total_uploads} frame upload(s) via /upload/image")
    if res.postflight_report is not None:
        _print_report(res.postflight_report)

    if res.publish_result is not None:
        pub = res.publish_result
        typer.echo(
            f"Published: {len(pub.published_files)} frame(s) -> {pub.publish_dir}"
        )
        if pub.manifest_path is not None:
            typer.echo(f"Manifest:  {pub.manifest_path}")
        if pub.quarantined_files:
            typer.echo(
                f"Quarantined: {len(pub.quarantined_files)} frame(s) -> {pub.quarantine_dir}"
            )
            if pub.quarantine_manifest_path is not None:
                typer.echo(f"Quarantine manifest: {pub.quarantine_manifest_path}")

    if not res.passed:
        raise typer.Exit(code=1)


@app.command()
def diff(
    v1_path: Annotated[Path, typer.Argument(help="Path to baseline workflow_api.json.")],
    v2_path: Annotated[Path, typer.Argument(help="Path to updated workflow_api.json.")],
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON diff output.")] = False,
) -> None:
    """Compare two ComfyUI API graph JSON files structurally and by parameter values."""
    for p in (v1_path, v2_path):
        if not p.is_file():
            typer.echo(f"Error: Graph file not found: {p}", err=True)
            raise typer.Exit(code=2)

    v1 = json.loads(v1_path.read_text(encoding="utf-8"))
    v2 = json.loads(v2_path.read_text(encoding="utf-8"))
    result = graph_diff(v1, v2)
    if as_json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        typer.echo(result.format_human())


@app.command()
def inspect(
    target: Annotated[Path, typer.Argument(help="Path to lineage_manifest.json or .exr file.")],
) -> None:
    """Inspect EXR pipeline attributes or a published LineageManifest JSON."""
    if not target.is_file():
        typer.echo(f"Error: File not found: {target}", err=True)
        raise typer.Exit(code=2)

    if target.suffix.lower() == ".json":
        manifest = LineageManifest.from_json_file(target)
        typer.echo(
            f"Job {manifest.job_id} | "
            f"{manifest.show}/{manifest.sequence}/{manifest.shot} "
            f"{manifest.pass_name} v{manifest.version:03d}"
        )
        typer.echo(
            f"Contract: {manifest.contract_name}:{manifest.contract_version} "
            f"Colorspace: {manifest.colorspace} BaseSeed: {manifest.base_seed}"
        )
        typer.echo(f"Workflow SHA-256: {manifest.workflow_sha256}")
        if manifest.preflight_report:
            typer.echo(f"  {manifest.preflight_report.summary()}")
        if manifest.postflight_report:
            typer.echo(f"  {manifest.postflight_report.summary()}")
        for frame in manifest.frames:
            state = "QUARANTINED" if frame.quarantined else "PUBLISHED"
            extra = f" ({frame.quarantine_reason})" if frame.quarantine_reason else ""
            typer.echo(
                f"  frame {frame.frame}: {state} seed={frame.seed} "
                f"graph={frame.graph_sha256[:12]}{extra}"
            )
        return

    try:
        info = inspect_image_header(target)
        typer.echo(
            f"Image: {target.name} ({info.format}) "
            f"{info.width}x{info.height} channels={info.channels}"
        )
        if info.format == "EXR":
            attrs = read_exr_attributes(target)
            if attrs:
                typer.echo("Attributes:")
                for key in sorted(attrs):
                    typer.echo(f"  {key}: {attrs[key]}")
    except (ImageInspectionError, EXRIOError) as exc:
        typer.echo(f"Error inspecting '{target}': {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("worker-status")
def worker_status(
    endpoint: Annotated[
        str | None,
        typer.Argument(
            help="ComfyUI worker base URL (auto-probes :8188 and :8000 if omitted)."
        ),
    ] = None,
) -> None:
    """Query live /queue, /object_info, and installed checkpoints from a ComfyUI worker."""
    if endpoint is None:
        probe = asyncio.run(discover_live_endpoint())
        if probe is None:
            checked = ", ".join(DEFAULT_LOCAL_ENDPOINTS)
            typer.echo(f"Workers unreachable ({checked})", err=True)
            raise typer.Exit(code=1)
    else:
        probe = asyncio.run(probe_worker(endpoint))
        if not probe.online:
            typer.echo(f"Worker unreachable ({probe.endpoint}): {probe.error}", err=True)
            raise typer.Exit(code=1)

    typer.echo(
        f"Worker {probe.endpoint}: ONLINE | running={probe.queue_running} "
        f"pending={probe.queue_pending} registered_nodes={probe.registered_nodes} "
        f"checkpoints={len(probe.checkpoints)}"
    )
    if probe.checkpoints:
        preview = ", ".join(probe.checkpoints[:5])
        more = f" (+{len(probe.checkpoints) - 5} more)" if len(probe.checkpoints) > 5 else ""
        typer.echo(f"  Checkpoints: {preview}{more}")
    else:
        typer.echo(
            "  Checkpoints: NONE FOUND "
            "(install a .safetensors model in ComfyUI models/checkpoints)",
            err=True,
        )


@app.command("smoke-test")
def smoke_test(
    endpoint: Annotated[
        str | None,
        typer.Option(
            "--endpoint",
            help="ComfyUI worker URL (auto-probes :8188 and ComfyUI Desktop :8000 if omitted).",
        ),
    ] = None,
    checkpoint: Annotated[
        str | None,
        typer.Option(
            "--checkpoint",
            help="Checkpoint filename from /object_info (defaults to first installed).",
        ),
    ] = None,
    width: Annotated[
        int,
        typer.Option("--width", help="Test plate width in pixels (multiple of 32)."),
    ] = 512,
    height: Annotated[
        int,
        typer.Option("--height", help="Test plate height in pixels (multiple of 32)."),
    ] = 512,
    steps: Annotated[int, typer.Option("--steps", help="KSampler step count.")] = 4,
    denoise: Annotated[
        float,
        typer.Option("--denoise", help="KSampler denoise strength (0.01..1.0)."),
    ] = 0.15,
    min_edge_iou: Annotated[
        float,
        typer.Option("--min-edge-iou", help="Minimum Sobel edge IoU threshold."),
    ] = 0.25,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Optional directory to persist smoke-test outputs."),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Execution timeout in seconds."),
    ] = 180.0,
    check_only: Annotated[
        bool,
        typer.Option(
            "--check-only",
            help="Verify ComfyUI reachability and installed checkpoints without running GPU work.",
        ),
    ] = False,
    upload_inputs: Annotated[
        bool,
        typer.Option(
            "--upload-inputs/--no-upload-inputs",
            help="Stage test plate via POST /upload/image before execution.",
        ),
    ] = True,
) -> None:
    """Probe a live ComfyUI worker and run an end-to-end img2img + EXR provenance smoke test."""
    try:
        res = asyncio.run(
            run_smoke_test(
                endpoint=endpoint,
                checkpoint=checkpoint,
                width=width,
                height=height,
                steps=steps,
                denoise=denoise,
                min_edge_iou=min_edge_iou,
                output_dir=output_dir,
                timeout=timeout,
                check_only=check_only,
                upload_inputs=upload_inputs,
            )
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(res.message)
    if res.probe.online:
        typer.echo(
            f"  Endpoint: {res.probe.endpoint} | nodes={res.probe.registered_nodes} "
            f"checkpoints={len(res.probe.checkpoints)}"
        )
        if res.probe.checkpoints:
            preview = ", ".join(res.probe.checkpoints[:5])
            more = (
                f" (+{len(res.probe.checkpoints) - 5} more)"
                if len(res.probe.checkpoints) > 5
                else ""
            )
            typer.echo(f"  Available checkpoints: {preview}{more}")

    if res.submission is not None:
        _print_report(res.submission.preflight_report)
        if res.submission.staged_inputs:
            total_uploads = sum(len(aovs) for aovs in res.submission.staged_inputs.values())
            typer.echo(f"  Staged inputs: {total_uploads} upload(s) via /upload/image")
        if res.submission.postflight_report is not None:
            _print_report(res.submission.postflight_report)
        if res.published_exr is not None:
            typer.echo(f"  Published EXR: {res.published_exr}")
            for key in ("comfyguard:seed", "comfyguard:colorspace", "comfyguard:graph_hash"):
                if key in res.exr_attributes:
                    typer.echo(f"    {key}: {res.exr_attributes[key]}")

    if not res.passed:
        raise typer.Exit(code=1)


@app.command()
def ui(
    host: Annotated[str, typer.Option("--host", help="Bind address for web UI.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="HTTP port for web UI.")] = 8788,
    root: Annotated[
        Path | None,
        typer.Option("--root", help="Optional publish/quarantine root directory to inspect."),
    ] = None,
    endpoint: Annotated[
        list[str] | None,
        typer.Option("--endpoint", help="ComfyUI worker URL(s) to monitor."),
    ] = None,
    demo: Annotated[
        bool,
        typer.Option("--demo", help="Pre-populate with the 3 portfolio scenarios on startup."),
    ] = False,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Do not automatically open the web browser."),
    ] = False,
) -> None:
    """Launch the interactive ComfyGuard pipeline console and visual gate inspector."""

    async def _serve() -> None:
        server = DashboardServer(host=host, port=port, root_dir=root, endpoints=endpoint)
        if demo:
            await server.preload_demo()
        await server.start()
        typer.echo(f"ComfyGuard UI listening at {server.base_url} (Ctrl+C to stop)")
        if not no_browser:
            webbrowser.open(server.base_url)
        try:
            await asyncio.Event().wait()
        finally:
            await server.stop()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve())


if __name__ == "__main__":
    app()
