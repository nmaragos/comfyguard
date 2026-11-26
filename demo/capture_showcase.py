"""Automated animated showcase capture of `comfyguard ui --demo` for README / LinkedIn.

Launches DashboardServer preloaded with all 3 portfolio scenarios, drives a
headless Chromium-based browser (Chrome / Edge) through a curated storyboard
over Chrome DevTools Protocol (CDP), and encodes a looping animated GIF (and
optional MP4 when ffmpeg is available).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import websockets
from PIL import Image

from comfyguard.ui.server import DashboardServer


@dataclass(frozen=True)
class StoryboardStep:
    """Single scripted UI state + dwell duration for animated showcase capture."""

    name: str
    script: str
    duration_ms: int
    wait_after_ms: int = 120


def find_browser_executable(preferred: str | None = None) -> Path:
    """Locate Chrome, Edge, or Chromium executable across Windows, macOS, and Linux."""
    if preferred:
        explicit = Path(preferred)
        if explicit.is_file():
            return explicit
        on_path = shutil.which(preferred)
        if on_path:
            return Path(on_path)
        raise FileNotFoundError(f"Requested browser executable not found: {preferred}")

    for env_key in ("CHROME_PATH", "BROWSER_PATH"):
        env_val = os.environ.get(env_key, "").strip()
        if env_val and Path(env_val).is_file():
            return Path(env_val)

    for binary in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(binary)
        if found:
            return Path(found)

    candidates = (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    )
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path

    raise FileNotFoundError(
        "No Chrome, Edge, or Chromium executable found. Pass --browser <path> or set CHROME_PATH."
    )


def build_default_storyboard() -> list[StoryboardStep]:
    """Return the deterministic multi-scenario UI tour storyboard."""
    steps: list[StoryboardStep] = [
        StoryboardStep(
            name="s1_visual_reject",
            script="""
            (async () => {
              const job = appState.jobs.find(j => j.scenario_tag === 'SCENARIO 1');
              await selectJob(job.job_id);
              setTab('visual');
              window.scrollTo(0, 0);
              return job.status;
            })()
            """,
            duration_ms=1400,
        ),
        StoryboardStep(
            name="s1_preflight_checks",
            script="""
            (() => {
              setTab('gates');
              window.scrollTo(0, 0);
              return 'gates';
            })()
            """,
            duration_ms=1700,
        ),
        StoryboardStep(
            name="s2_quarantine_visual",
            script="""
            (async () => {
              const job = appState.jobs.find(j => j.scenario_tag === 'SCENARIO 2');
              await selectJob(job.job_id);
              setTab('visual');
              window.scrollTo(0, 0);
              return job.status;
            })()
            """,
            duration_ms=1100,
        ),
    ]

    wipe_positions = (
        (18, 170),
        (34, 170),
        (50, 170),
        (66, 170),
        (82, 220),
        (66, 170),
        (50, 950),
    )
    for idx, (pos, dwell) in enumerate(wipe_positions):
        steps.append(
            StoryboardStep(
                name=f"s2_wipe_{idx}_{pos}",
                script=f"""
                (() => {{
                  const slider = document.querySelector('.wipe-slider');
                  if (slider) slider.value = '{pos}';
                  updateWipe({pos});
                  return {pos};
                }})()
                """,
                duration_ms=dwell,
                wait_after_ms=60,
            )
        )

    steps.extend(
        [
            StoryboardStep(
                name="s3_publish_visual",
                script="""
                (async () => {
                  const job = appState.jobs.find(j => j.scenario_tag === 'SCENARIO 3');
                  await selectJob(job.job_id);
                  setTab('visual');
                  window.scrollTo(0, 0);
                  return job.status;
                })()
                """,
                duration_ms=1100,
            ),
            StoryboardStep(
                name="s3_frame_1002",
                script="""
                (() => {
                  setFrame(1);
                  return 'f1002';
                })()
                """,
                duration_ms=900,
            ),
            StoryboardStep(
                name="s3_exr_lineage",
                script="""
                (() => {
                  setTab('lineage');
                  window.scrollTo(0, 0);
                  return 'lineage';
                })()
                """,
                duration_ms=2000,
            ),
            StoryboardStep(
                name="diff_contract_violation",
                script="""
                (async () => {
                  setTab('diff');
                  document.getElementById('df-denoise').value = '0.99';
                  await submitDiff();
                  window.scrollTo(0, 0);
                  return 'violation';
                })()
                """,
                duration_ms=1800,
            ),
            StoryboardStep(
                name="diff_contract_validated",
                script="""
                (async () => {
                  document.getElementById('df-steps').value = '32';
                  document.getElementById('df-denoise').value = '0.50';
                  await submitDiff();
                  window.scrollTo(0, 0);
                  return 'validated';
                })()
                """,
                duration_ms=2200,
            ),
        ]
    )
    return steps


def encode_animated_gif(
    png_frames: Sequence[bytes],
    durations_ms: Sequence[int],
    output_path: str | Path,
    *,
    max_width: int = 1080,
) -> Path:
    """Encode raw PNG screenshot frames into an optimized looping animated GIF."""
    if not png_frames:
        raise ValueError("Cannot encode animated GIF from empty frame list")
    if len(png_frames) != len(durations_ms):
        raise ValueError(
            f"Frame count ({len(png_frames)}) does not match durations ({len(durations_ms)})"
        )

    palette_frames: list[Image.Image] = []
    for raw_png in png_frames:
        with Image.open(io.BytesIO(raw_png)) as opened:
            rgb = opened.convert("RGB")
            if max_width > 0 and rgb.width > max_width:
                new_h = max(1, round(rgb.height * (max_width / rgb.width)))
                rgb = rgb.resize((max_width, new_h), Image.Resampling.LANCZOS)
            quantized = rgb.quantize(
                colors=256,
                method=Image.Quantize.FASTOCTREE,
                dither=Image.Dither.NONE,
            )
            palette_frames.append(quantized)

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    palette_frames[0].save(
        target,
        format="GIF",
        save_all=True,
        append_images=palette_frames[1:],
        duration=[max(20, int(d)) for d in durations_ms],
        loop=0,
        optimize=True,
    )
    return target


def export_mp4_from_pngs(
    png_frames: Sequence[bytes],
    durations_ms: Sequence[int],
    output_path: str | Path,
) -> Path:
    """Encode PNG frames to H.264 MP4 via ffmpeg concat demuxer (requires ffmpeg on PATH)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg executable not found on PATH; cannot export MP4")

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="comfyguard_mp4_") as tmp:
        tmp_dir = Path(tmp)
        concat_lines: list[str] = []
        last_img: Path | None = None
        for idx, (raw, dur_ms) in enumerate(zip(png_frames, durations_ms, strict=True)):
            frame_file = tmp_dir / f"frame_{idx:04d}.png"
            frame_file.write_bytes(raw)
            concat_lines.append(f"file '{frame_file.as_posix()}'")
            concat_lines.append(f"duration {max(dur_ms, 20) / 1000.0:.3f}")
            last_img = frame_file
        if last_img is not None:
            concat_lines.append(f"file '{last_img.as_posix()}'")
        concat_file = tmp_dir / "frames.txt"
        concat_file.write_text("\n".join(concat_lines), encoding="utf-8")

        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-vf",
                "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(target),
            ],
            check=True,
            capture_output=True,
        )
    return target


class _CDPClient:
    """Minimal async Chrome DevTools Protocol client over websockets."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        payload = {"id": req_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(payload))
        while True:
            raw = await self._ws.recv()
            msg = json.loads(raw)
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                raise RuntimeError(f"CDP {method} failed: {msg['error']}")
            result = msg.get("result")
            return result if isinstance(result, dict) else {}


def _allocate_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_cdp_page_ws(cdp_port: int, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{cdp_port}/json/list"
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        targets = await resp.json()
                        for target in targets:
                            if (
                                isinstance(target, dict)
                                and target.get("type") == "page"
                                and "webSocketDebuggerUrl" in target
                            ):
                                return str(target["webSocketDebuggerUrl"])
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.15)
    raise TimeoutError(f"Timed out waiting for headless browser CDP target on port {cdp_port}")


async def capture_showcase(
    *,
    output_path: Path = Path("docs/images/comfyguard_demo_showcase.gif"),
    width: int = 1280,
    height: int = 720,
    max_width: int = 1080,
    browser_path: str | None = None,
    mp4_path: Path | None = None,
) -> dict[str, Any]:
    """Start DashboardServer --demo, capture storyboard via headless CDP, and write GIF."""
    browser_exe = find_browser_executable(browser_path)
    storyboard = build_default_storyboard()
    cdp_port = _allocate_ephemeral_port()

    server = DashboardServer(host="127.0.0.1", port=0)
    await server.preload_demo()
    await server.start()

    proc: subprocess.Popen[bytes] | None = None
    profile_dir = tempfile.mkdtemp(prefix="comfyguard_cdp_")
    try:
        cmd = [
            str(browser_exe),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            f"--remote-debugging-port={cdp_port}",
            f"--window-size={width},{height}",
            "--force-device-scale-factor=1",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            server.base_url,
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        ws_url = await _wait_for_cdp_page_ws(cdp_port)
        async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
            cdp = _CDPClient(ws)
            await cdp.call("Page.enable")
            await cdp.call("Runtime.enable")
            await cdp.call(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
            )

            # Wait until DashboardServer state & all 3 preloaded jobs render
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                eval_res = await cdp.call(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "typeof appState !== 'undefined' && Boolean(appState && "
                            "appState.jobs && "
                            "document.querySelectorAll('.job-item').length >= 3)"
                        ),
                        "returnByValue": True,
                    },
                )
                if eval_res.get("result", {}).get("value") is True:
                    break
                await asyncio.sleep(0.15)
            else:
                raise TimeoutError("Dashboard UI did not finish rendering preloaded scenarios")

            png_frames: list[bytes] = []
            durations_ms: list[int] = []
            for step in storyboard:
                await cdp.call(
                    "Runtime.evaluate",
                    {
                        "expression": step.script,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )
                await asyncio.sleep(step.wait_after_ms / 1000.0)
                shot = await cdp.call("Page.captureScreenshot", {"format": "png"})
                png_frames.append(base64.b64decode(shot["data"]))
                durations_ms.append(step.duration_ms)

            gif_file = encode_animated_gif(
                png_frames,
                durations_ms,
                output_path,
                max_width=max_width,
            )
            mp4_file: Path | None = None
            if mp4_path is not None:
                mp4_file = export_mp4_from_pngs(png_frames, durations_ms, mp4_path)

            return {
                "gif_path": gif_file,
                "mp4_path": mp4_file,
                "frame_count": len(png_frames),
                "total_duration_ms": sum(durations_ms),
                "browser": str(browser_exe),
            }
    finally:
        if proc is not None:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                proc.terminate()
                proc.wait(timeout=5)
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.kill()
                    proc.wait(timeout=3)
        shutil.rmtree(profile_dir, ignore_errors=True)
        await server.stop()


def main() -> None:
    """CLI entrypoint for automated showcase GIF/MP4 recording."""
    parser = argparse.ArgumentParser(
        description="Capture animated showcase GIF of `comfyguard ui --demo`"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/images/comfyguard_demo_showcase.gif"),
        help="Destination animated GIF path.",
    )
    parser.add_argument("--width", type=int, default=1280, help="Browser viewport width.")
    parser.add_argument("--height", type=int, default=720, help="Browser viewport height.")
    parser.add_argument(
        "--max-width",
        type=int,
        default=1080,
        help="Maximum output GIF width (0 preserves viewport width).",
    )
    parser.add_argument(
        "--browser",
        type=str,
        default=None,
        help="Optional explicit Chrome/Edge/Chromium executable path.",
    )
    parser.add_argument(
        "--mp4",
        type=Path,
        default=None,
        help="Optional MP4 output path (requires ffmpeg on PATH).",
    )
    args = parser.parse_args()

    info = asyncio.run(
        capture_showcase(
            output_path=args.output,
            width=args.width,
            height=args.height,
            max_width=args.max_width,
            browser_path=args.browser,
            mp4_path=args.mp4,
        )
    )
    gif_path = Path(info["gif_path"])
    size_mb = gif_path.stat().st_size / (1024 * 1024)
    print(
        f"Captured {info['frame_count']} frames ({info['total_duration_ms'] / 1000:.1f}s) "
        f"-> {gif_path} ({size_mb:.2f} MiB) using {info['browser']}"
    )
    if info["mp4_path"] is not None:
        print(f"Exported MP4 -> {info['mp4_path']}")


if __name__ == "__main__":
    main()
