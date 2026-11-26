"""Interactive web dashboard, visual diagnostics, and static HTML report generator."""

from comfyguard.ui.demo_runner import run_portfolio_scenarios
from comfyguard.ui.report import (
    FrameVisualRecord,
    GraphDiffRecord,
    JobVisualRecord,
    ReportPayload,
    build_job_record_from_submission,
    export_report,
    render_static_report_html,
    scan_workspace_jobs,
)
from comfyguard.ui.server import DashboardServer
from comfyguard.ui.visuals import (
    array_to_png_data_uri,
    image_path_to_data_uri,
    render_edge_overlay_data_uri,
    render_temporal_diff_data_uri,
)

__all__ = [
    "DashboardServer",
    "FrameVisualRecord",
    "GraphDiffRecord",
    "JobVisualRecord",
    "ReportPayload",
    "array_to_png_data_uri",
    "build_job_record_from_submission",
    "export_report",
    "image_path_to_data_uri",
    "render_edge_overlay_data_uri",
    "render_static_report_html",
    "render_temporal_diff_data_uri",
    "run_portfolio_scenarios",
    "scan_workspace_jobs",
]
