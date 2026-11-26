"""End-to-end 3-scenario portfolio demonstration and visual report/UI launcher.

Demonstrates:
  1. Pre-flight rejection of unaligned 1920x1080 input (0 GPU prompts dispatched).
  2. Post-flight quarantine of a high edge-drift generated frame.
  3. Clean multi-worker execution of a 1920x1088 sequence, stamped EXR publishing,
     LineageManifest verification, and v001 -> v002 graph_diff inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import webbrowser
from pathlib import Path
from typing import Any

from comfyguard.ui.demo_runner import (
    DemoComfyServer,
    run_portfolio_scenarios,
)
from comfyguard.ui.demo_runner import (
    png_bytes as _png_bytes,
)
from comfyguard.ui.demo_runner import (
    render_plate_rgb as _render_plate_rgb,
)
from comfyguard.ui.server import DashboardServer

__all__ = [
    "DemoComfyServer",
    "_png_bytes",
    "_render_plate_rgb",
    "main",
    "run_demo",
]


async def run_demo(
    output_dir: Path | None = None,
    *,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Execute all 3 portfolio scenarios and optionally write a static HTML report."""
    return await run_portfolio_scenarios(
        output_dir=output_dir,
        scenarios=("1", "2", "3"),
        report_path=report_path,
        verbose=True,
    )


async def _serve_demo_ui(port: int, open_browser: bool) -> None:
    server = DashboardServer(host="127.0.0.1", port=port)
    await server.preload_demo()
    await server.start()
    print(f"ComfyGuard Dashboard running at {server.base_url} (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(server.base_url)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


def main() -> None:
    """CLI entrypoint for running the ComfyGuard portfolio demo."""
    parser = argparse.ArgumentParser(description="ComfyGuard 3-Scenario Portfolio Demo")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Persist generated plates, EXRs, and quarantine artifacts in this directory.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write a self-contained static HTML visual report to this file.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Preload all 3 scenarios and launch the interactive web dashboard.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8788,
        help="Port for --serve web dashboard (default: 8788).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open a browser tab when --serve is enabled.",
    )
    args = parser.parse_args()

    if args.serve:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_serve_demo_ui(args.port, not args.no_browser))
        return

    asyncio.run(run_demo(output_dir=args.output_dir, report_path=args.report))


if __name__ == "__main__":
    main()
