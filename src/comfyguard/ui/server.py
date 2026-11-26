"""Lightweight aiohttp web dashboard server for ComfyGuard."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from aiohttp import web

from comfyguard.broker.client import ComfyUIClient, ComfyUIConnectionError
from comfyguard.broker.mutator import GraphMutator, ParamValidationError, graph_diff
from comfyguard.ui.demo_runner import resolve_default_workflow_dir, run_portfolio_scenarios
from comfyguard.ui.report import (
    GraphDiffRecord,
    JobVisualRecord,
    ReportPayload,
    render_static_report_html,
    scan_workspace_jobs,
)
from comfyguard.ui.templates import DASHBOARD_HTML


class DashboardServer:
    """Local dark-mode web console for queue monitoring, visual gates, and lineage inspection."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8788,
        root_dir: Path | None = None,
        endpoints: Sequence[str] | None = None,
        workflow_dir: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.root_dir = root_dir
        self.endpoints = [ep.rstrip("/") for ep in (endpoints or [])]
        self.workflow_dir = workflow_dir or resolve_default_workflow_dir()

        self.jobs: dict[str, JobVisualRecord] = {}
        self.job_order: list[str] = []
        self.latest_diff: GraphDiffRecord | None = self._build_default_diff()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _build_default_diff(self) -> GraphDiffRecord | None:
        if not (self.workflow_dir / "contract.yaml").is_file():
            return None
        mutator = GraphMutator.from_workflow_dir(self.workflow_dir)
        v1 = mutator.template_graph
        v2 = mutator.mutate_for_frame(
            frame=1001,
            base_seed=42,
            params={"steps": 28, "cfg": 7.0},
            input_files={"beauty": "beauty.1001.exr", "depth": "depth.1001.exr"},
        ).graph
        return GraphDiffRecord.from_graph_diff(
            graph_diff(v1, v2),
            v1_label="v001 (template)",
            v2_label="v002 (mutated)",
        )

    def _upsert_job(self, record: JobVisualRecord) -> None:
        if record.job_id in self.job_order:
            self.job_order.remove(record.job_id)
        self.job_order.insert(0, record.job_id)
        self.jobs[record.job_id] = record

    def rescan(self) -> int:
        """Scan configured root_dir for LineageManifest files and load them."""
        if self.root_dir is None:
            return 0
        discovered = scan_workspace_jobs(self.root_dir)
        for rec in discovered:
            self._upsert_job(rec)
        return len(discovered)

    async def preload_demo(self) -> None:
        """Pre-populate the dashboard with all 3 portfolio scenarios."""
        data = await run_portfolio_scenarios(
            scenarios=("1", "2", "3"),
            workflow_dir=self.workflow_dir,
            verbose=False,
        )
        for rec in data["visual_jobs"]:
            self._upsert_job(rec)
        self.latest_diff = data["diff_record"]

    async def __aenter__(self) -> DashboardServer:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start the aiohttp web server."""
        if self.root_dir is not None:
            self.rescan()

        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/api/state", self._handle_state)
        app.router.add_get("/api/jobs/{job_id}", self._handle_get_job)
        app.router.add_post("/api/demo/run", self._handle_run_demo)
        app.router.add_post("/api/diff", self._handle_diff)
        app.router.add_get("/api/report", self._handle_export_report)
        app.router.add_post("/api/rescan", self._handle_rescan)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        assert self._site._server is not None
        sockets = self._site._server.sockets
        self.port = int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        """Shut down the web server cleanly."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def serve_forever(self) -> None:
        """Run the dashboard server until interrupted."""
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    async def _worker_statuses(self) -> list[dict[str, Any]]:
        if not self.endpoints:
            return [
                {
                    "name": "mock-gpu-0",
                    "url": "in-process://mock-0",
                    "online": False,
                    "mode": "DEMO POOL",
                },
                {
                    "name": "mock-gpu-1",
                    "url": "in-process://mock-1",
                    "online": False,
                    "mode": "DEMO POOL",
                },
            ]
        results: list[dict[str, Any]] = []
        for idx, ep in enumerate(self.endpoints):
            online = False
            mode = "OFFLINE"
            try:
                async with ComfyUIClient(ep, request_timeout=2.0) as client:
                    q = await client.get_queue_status()
                    running = len(q.get("queue_running", []))
                    pending = len(q.get("queue_pending", []))
                    online = True
                    mode = f"ONLINE ({running} run / {pending} q)"
            except ComfyUIConnectionError:
                online = False
            results.append({"name": f"worker-{idx}", "url": ep, "online": online, "mode": mode})
        return results

    def _compute_kpis(self) -> dict[str, int]:
        published_frames = 0
        quarantined_frames = 0
        preflight_rejected = 0
        gpu_prompts_saved = 0
        for job in self.jobs.values():
            if job.status == "PREFLIGHT_REJECTED":
                preflight_rejected += 1
                gpu_prompts_saved += max(len(job.frames), 1)
            for frame in job.frames:
                if frame.status == "published":
                    published_frames += 1
                elif frame.status == "quarantined":
                    quarantined_frames += 1
        return {
            "total_jobs": len(self.jobs),
            "published_frames": published_frames,
            "quarantined_frames": quarantined_frames,
            "preflight_rejected": preflight_rejected,
            "gpu_prompts_saved": gpu_prompts_saved,
        }

    async def _handle_index(self, _request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def _handle_state(self, _request: web.Request) -> web.Response:
        ordered_summaries = []
        for job_id in self.job_order:
            j = self.jobs[job_id]
            ordered_summaries.append(
                {
                    "job_id": j.job_id,
                    "title": j.title,
                    "scenario_tag": j.scenario_tag,
                    "status": j.status,
                    "shot_label": j.shot_label,
                    "pass_label": j.pass_label,
                }
            )
        payload = {
            "kpis": self._compute_kpis(),
            "workers": await self._worker_statuses(),
            "jobs": ordered_summaries,
            "latest_diff": (
                self.latest_diff.model_dump(mode="json") if self.latest_diff else None
            ),
        }
        return web.json_response(payload)

    async def _handle_get_job(self, request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        rec = self.jobs.get(job_id)
        if rec is None:
            return web.json_response({"error": f"Unknown job_id '{job_id}'"}, status=404)
        return web.json_response(rec.model_dump(mode="json"))

    async def _handle_run_demo(self, request: web.Request) -> web.Response:
        body: dict[str, Any] = {}
        if request.can_read_body:
            raw = await request.text()
            if raw:
                body = json.loads(raw)
        scenario = str(body.get("scenario", "all"))
        selected = ("1", "2", "3") if scenario == "all" else (scenario,)
        if any(s not in {"1", "2", "3"} for s in selected):
            return web.json_response({"error": f"Invalid scenario {scenario!r}"}, status=400)

        data = await run_portfolio_scenarios(
            scenarios=selected,
            workflow_dir=self.workflow_dir,
            verbose=False,
        )
        for rec in data["visual_jobs"]:
            self._upsert_job(rec)
        self.latest_diff = data["diff_record"]
        latest_id = data["visual_jobs"][-1].job_id if data["visual_jobs"] else None
        return web.json_response({"ok": True, "latest_job_id": latest_id})

    async def _handle_diff(self, request: web.Request) -> web.Response:
        body = await request.json()
        params = body.get("params", {})
        if not isinstance(params, dict):
            return web.json_response({"error": "Expected 'params' mapping"}, status=400)

        mutator = GraphMutator.from_workflow_dir(self.workflow_dir)
        try:
            mutated = mutator.mutate_for_frame(
                frame=1001,
                base_seed=42,
                params=params,
                input_files={"beauty": "beauty.1001.exr", "depth": "depth.1001.exr"},
            )
        except ParamValidationError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        diff = graph_diff(mutator.template_graph, mutated.graph)
        record = GraphDiffRecord.from_graph_diff(
            diff, v1_label="v001 (contract baseline)", v2_label="sandbox override"
        )
        self.latest_diff = record
        return web.json_response(record.model_dump(mode="json"))

    async def _handle_export_report(self, _request: web.Request) -> web.Response:
        ordered_jobs = [self.jobs[jid] for jid in self.job_order]
        payload = ReportPayload(jobs=ordered_jobs, graph_diff=self.latest_diff)
        html = render_static_report_html(payload)
        return web.Response(
            text=html,
            content_type="text/html",
            headers={"Content-Disposition": 'attachment; filename="comfyguard_report.html"'},
        )

    async def _handle_rescan(self, _request: web.Request) -> web.Response:
        count = self.rescan()
        return web.json_response({"ok": True, "discovered": count})
