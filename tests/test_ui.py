"""Tests for ComfyGuard visual diagnostics, static HTML report, and web dashboard."""

from __future__ import annotations

import re
from pathlib import Path

import aiohttp
from typer.testing import CliRunner

from comfyguard.cli import app
from comfyguard.publish.exr_io import write_exr
from comfyguard.ui.demo_runner import render_plate_rgb, run_portfolio_scenarios, save_png
from comfyguard.ui.report import scan_workspace_jobs
from comfyguard.ui.server import DashboardServer
from comfyguard.ui.visuals import (
    array_to_png_data_uri,
    image_path_to_data_uri,
    render_edge_overlay_data_uri,
    render_temporal_diff_data_uri,
)

runner = CliRunner()


def test_visuals_png_exr_and_edge_overlay(tmp_path: Path) -> None:
    ref = render_plate_rgb(256, 256)
    relit = render_plate_rgb(256, 256, rim_boost=0.8)
    drifted = render_plate_rgb(256, 256, shift_x=60, rim_boost=1.0)

    ref_png = save_png(tmp_path / "ref.png", ref)
    relit_exr = write_exr(tmp_path / "relit.exr", relit)
    drifted_png = save_png(tmp_path / "drifted.png", drifted)

    assert image_path_to_data_uri(ref_png).startswith("data:image/png;base64,")
    assert image_path_to_data_uri(relit_exr).startswith("data:image/png;base64,")

    # Verify NaN pixel highlighting works cleanly without crashing
    nan_arr = ref.copy()
    nan_arr[10, 10, 0] = float("nan")
    assert array_to_png_data_uri(nan_arr).startswith("data:image/png;base64,")

    good_uri, good_iou = render_edge_overlay_data_uri(relit_exr, ref_png)
    bad_uri, bad_iou = render_edge_overlay_data_uri(drifted_png, ref_png)
    assert good_uri.startswith("data:image/png;base64,")
    assert bad_uri.startswith("data:image/png;base64,")
    assert good_iou >= 0.85
    assert bad_iou < 0.50

    temp_uri, delta = render_temporal_diff_data_uri(ref_png, drifted_png)
    assert temp_uri.startswith("data:image/png;base64,")
    assert delta > 0.0


async def test_static_html_report_and_workspace_scan(tmp_path: Path) -> None:
    report_file = tmp_path / "demo_report.html"
    workspace = tmp_path / "workspace"

    res = await run_portfolio_scenarios(
        output_dir=workspace,
        scenarios=("1", "2", "3"),
        report_path=report_file,
        verbose=False,
    )
    assert report_file.is_file()
    html = report_file.read_text(encoding="utf-8")
    assert "ComfyGuard" in html
    assert "data:image/png;base64," in html
    assert "comfyguard:graph_hash" in html
    assert "comfyguard:colorspace" in html
    assert "Graph Diff" in html
    assert r"changes.join('\n')" in html
    assert len(res["visual_jobs"]) == 3

    discovered = scan_workspace_jobs(workspace / "publish")
    statuses = {rec.status for rec in discovered}
    assert statuses == {"PUBLISHED", "QUARANTINED"}
    published_rec = next(r for r in discovered if r.status == "PUBLISHED")
    assert len(published_rec.frames) == 3
    assert published_rec.frames[0].exr_attributes["comfyguard:seed"] == "2001"


async def test_dashboard_server_api_end_to_end() -> None:
    async with (
        DashboardServer(host="127.0.0.1", port=0) as server,
        aiohttp.ClientSession() as session,
    ):
        async with session.get(f"{server.base_url}/") as resp:
            assert resp.status == 200
            html = await resp.text()
            assert "ComfyGuard Pipeline Console" in html

        async with session.post(
            f"{server.base_url}/api/demo/run", json={"scenario": "all"}
        ) as resp:
            assert resp.status == 200
            run_data = await resp.json()
            assert run_data["ok"] is True

        async with session.get(f"{server.base_url}/api/state") as resp:
            assert resp.status == 200
            state = await resp.json()
            assert state["kpis"]["total_jobs"] == 3
            assert state["kpis"]["published_frames"] == 3
            assert state["kpis"]["quarantined_frames"] == 1
            assert state["kpis"]["preflight_rejected"] == 1
            assert len(state["jobs"]) == 3

        quar_summary = next(j for j in state["jobs"] if j["status"] == "QUARANTINED")
        async with session.get(
            f"{server.base_url}/api/jobs/{quar_summary['job_id']}"
        ) as resp:
            assert resp.status == 200
            detail = await resp.json()
            assert detail["frames"][0]["edge_overlay"].startswith("data:image/png;base64,")
            assert detail["frames"][0]["edge_iou"] < 0.85

        # Valid graph diff sandbox request
        async with session.post(
            f"{server.base_url}/api/diff",
            json={"params": {"steps": 36, "cfg": 7.5, "denoise": 0.5}},
        ) as resp:
            assert resp.status == 200
            diff_json = await resp.json()
            assert "steps" in diff_json["human_text"]

        # Out-of-bounds parameter must fail contract validation with HTTP 400
        async with session.post(
            f"{server.base_url}/api/diff",
            json={"params": {"denoise": 0.99}},
        ) as resp:
            assert resp.status == 400
            err_json = await resp.json()
            assert "denoise" in err_json["error"]

        # Downloadable static HTML report from live dashboard state
        async with session.get(f"{server.base_url}/api/report") as resp:
            assert resp.status == 200
            exported = await resp.text()
            assert "data:image/png;base64," in exported
            assert "comfyguard:graph_hash" in exported


def test_cli_ui_help() -> None:
    res = runner.invoke(app, ["ui", "--help"], env={"NO_COLOR": "1"})
    assert res.exit_code == 0
    clean = re.sub(r"\x1b\[[0-9;]*m", "", res.stdout)
    assert "--demo" in clean
    assert "--port" in clean
