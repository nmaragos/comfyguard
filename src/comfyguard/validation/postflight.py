"""Post-flight validation gates: buffer integrity, edge drift IoU, and temporal delta."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from comfyguard.contracts.manifest import CheckResult, CheckStatus, ValidationReport
from comfyguard.contracts.schema import WorkflowContract
from comfyguard.publish.exr_io import EXRIOError, load_image_buffer


class PostflightFrameInput(BaseModel):
    """Input pairing a generated output frame with an optional structural reference pass."""

    frame: int
    output_path: Path
    reference_path: Path | None = None


class FrameQualityMetrics(BaseModel):
    """Quantitative image quality metrics recorded during post-flight inspection."""

    frame: int
    width: int
    height: int
    channels: list[str] = Field(default_factory=list)
    has_nan: bool
    has_inf: bool
    pixel_std: float
    edge_iou: float | None = None
    temporal_delta: float | None = None


class PostflightResult(BaseModel):
    """Aggregated post-flight validation outcome and per-frame pass/fail routing."""

    report: ValidationReport
    frame_metrics: dict[int, FrameQualityMetrics] = Field(default_factory=dict)
    passed_frames: list[int] = Field(default_factory=list)
    failed_frames: dict[int, list[str]] = Field(default_factory=dict)


def _to_grayscale(arr: np.ndarray) -> np.ndarray:
    if arr.ndim == 2:
        return arr.astype(np.float32)
    if arr.shape[2] >= 3:
        return np.mean(arr[:, :, :3], axis=2, dtype=np.float32)
    return arr[:, :, 0].astype(np.float32)


def _dilate_binary_3x3(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    h, w = mask.shape
    out = np.zeros_like(mask, dtype=bool)
    for dy in range(3):
        for dx in range(3):
            out |= padded[dy : dy + h, dx : dx + w]
    return out


def compute_sobel_edges(
    gray: np.ndarray,
    *,
    threshold: float = 0.2,
    dilate: bool = True,
) -> np.ndarray:
    """Compute a normalized binary Sobel edge mask using pure NumPy."""
    g = np.asarray(gray, dtype=np.float32)
    g_min = float(np.min(g))
    g_max = float(np.max(g))
    if g_max - g_min <= 1e-6:
        return np.zeros(g.shape, dtype=bool)

    norm = (g - g_min) / (g_max - g_min)
    p = np.pad(norm, 1, mode="edge")
    h, w = g.shape

    gx = (
        -p[0:h, 0:w]
        + p[0:h, 2 : w + 2]
        - 2.0 * p[1 : h + 1, 0:w]
        + 2.0 * p[1 : h + 1, 2 : w + 2]
        - p[2 : h + 2, 0:w]
        + p[2 : h + 2, 2 : w + 2]
    )
    gy = (
        -p[0:h, 0:w]
        - 2.0 * p[0:h, 1 : w + 1]
        - p[0:h, 2 : w + 2]
        + p[2 : h + 2, 0:w]
        + 2.0 * p[2 : h + 2, 1 : w + 1]
        + p[2 : h + 2, 2 : w + 2]
    )

    mag = np.hypot(gx, gy)
    max_mag = float(np.max(mag))
    if max_mag <= 1e-6:
        return np.zeros(g.shape, dtype=bool)

    edges = (mag / max_mag) >= threshold
    return _dilate_binary_3x3(edges) if dilate else edges


def compute_edge_iou(
    output_arr: np.ndarray,
    ref_arr: np.ndarray,
    *,
    edge_threshold: float = 0.2,
) -> float:
    """Compute Intersection-over-Union between output edges and reference pass edges."""
    out_edges = compute_sobel_edges(_to_grayscale(output_arr), threshold=edge_threshold)
    ref_edges = compute_sobel_edges(_to_grayscale(ref_arr), threshold=edge_threshold)

    union = int(np.sum(out_edges | ref_edges))
    if union == 0:
        return 1.0
    intersection = int(np.sum(out_edges & ref_edges))
    return float(intersection / union)


def compute_temporal_delta(prev_arr: np.ndarray, curr_arr: np.ndarray) -> float:
    """Compute mean absolute per-pixel difference between consecutive frames."""
    return float(np.mean(np.abs(curr_arr.astype(np.float32) - prev_arr.astype(np.float32))))


class PostflightValidator:
    """Validates generated outputs for buffer integrity, edge drift, and temporal flicker."""

    def validate_sequence(
        self,
        frames: Sequence[PostflightFrameInput],
        contract: WorkflowContract,
        *,
        expected_resolution: tuple[int, int] | None = None,
    ) -> PostflightResult:
        """Validate a sequence of generated frames against post-flight contract rules."""
        report = ValidationReport(stage="postflight")
        frame_metrics: dict[int, FrameQualityMetrics] = {}
        frame_reasons: dict[int, list[str]] = {}
        loaded_buffers: dict[int, np.ndarray] = {}
        thresholds = contract.postflight
        res_rule = contract.resolution

        if not frames:
            report.add_check(
                CheckResult(
                    check_name="postflight_inputs",
                    status=CheckStatus.FAIL,
                    message="No generated frames supplied to post-flight validator",
                )
            )
            return PostflightResult(report=report)

        ordered_frames = sorted(frames, key=lambda item: item.frame)

        for item in ordered_frames:
            frame = item.frame
            reasons: list[str] = []

            try:
                arr, channels = load_image_buffer(item.output_path)
            except (EXRIOError, OSError) as exc:
                msg = f"Frame {frame} unreadable: {exc}"
                reasons.append(msg)
                frame_reasons[frame] = reasons
                report.add_check(
                    CheckResult(
                        check_name="buffer_load",
                        status=CheckStatus.FAIL,
                        frame=frame,
                        message=msg,
                    )
                )
                continue

            height, width, num_channels = arr.shape
            has_nan = bool(np.isnan(arr).any())
            has_inf = bool(np.isinf(arr).any())

            if expected_resolution is not None and (width, height) != expected_resolution:
                exp_w, exp_h = expected_resolution
                msg = (
                    f"Frame {frame} resolution {width}x{height} does not match "
                    f"expected {exp_w}x{exp_h}"
                )
                reasons.append(msg)
                report.add_check(
                    CheckResult(
                        check_name="output_resolution",
                        status=CheckStatus.FAIL,
                        frame=frame,
                        message=msg,
                    )
                )
            elif (
                width % res_rule.multiple_of != 0
                or height % res_rule.multiple_of != 0
                or not (res_rule.min_width <= width <= res_rule.max_width)
                or not (res_rule.min_height <= height <= res_rule.max_height)
            ):
                msg = (
                    f"Frame {frame} resolution {width}x{height} violates contract "
                    f"alignment/bounds (multiple_of={res_rule.multiple_of})"
                )
                reasons.append(msg)
                report.add_check(
                    CheckResult(
                        check_name="output_resolution",
                        status=CheckStatus.FAIL,
                        frame=frame,
                        message=msg,
                    )
                )

            if num_channels != thresholds.expected_channels:
                msg = (
                    f"Frame {frame} has {num_channels} channel(s) ({channels}), "
                    f"expected {thresholds.expected_channels}"
                )
                reasons.append(msg)
                report.add_check(
                    CheckResult(
                        check_name="output_channels",
                        status=CheckStatus.FAIL,
                        frame=frame,
                        message=msg,
                    )
                )

            if not thresholds.allow_nan_or_inf and (has_nan or has_inf):
                bad_flags = [
                    name
                    for name, present in (("NaN", has_nan), ("Inf", has_inf))
                    if present
                ]
                msg = f"Frame {frame} buffer contains non-finite values ({'/'.join(bad_flags)})"
                reasons.append(msg)
                report.add_check(
                    CheckResult(
                        check_name="buffer_integrity",
                        status=CheckStatus.FAIL,
                        frame=frame,
                        message=msg,
                    )
                )

            finite_vals = arr[np.isfinite(arr)]
            pixel_std = float(np.std(finite_vals)) if finite_vals.size > 0 else 0.0
            if not (has_nan or has_inf) and pixel_std < thresholds.min_pixel_std:
                msg = (
                    f"Frame {frame} appears flat/corrupt "
                    f"(std={pixel_std:.6g} < {thresholds.min_pixel_std:.6g})"
                )
                reasons.append(msg)
                report.add_check(
                    CheckResult(
                        check_name="flat_frame",
                        status=CheckStatus.FAIL,
                        frame=frame,
                        message=msg,
                    )
                )

            edge_iou: float | None = None
            if item.reference_path is not None and not (has_nan or has_inf):
                try:
                    ref_arr, _ = load_image_buffer(item.reference_path)
                    if ref_arr.shape[:2] != (height, width):
                        msg = (
                            f"Frame {frame} reference shape {ref_arr.shape[1]}x{ref_arr.shape[0]} "
                            f"differs from output {width}x{height}"
                        )
                        reasons.append(msg)
                        report.add_check(
                            CheckResult(
                                check_name="edge_drift",
                                status=CheckStatus.FAIL,
                                frame=frame,
                                message=msg,
                            )
                        )
                    else:
                        edge_iou = compute_edge_iou(arr, ref_arr)
                        if edge_iou < thresholds.min_edge_iou:
                            msg = (
                                f"Frame {frame} structural edge IoU {edge_iou:.4f} "
                                f"below threshold {thresholds.min_edge_iou:.4f}"
                            )
                            reasons.append(msg)
                            report.add_check(
                                CheckResult(
                                    check_name="edge_drift",
                                    status=CheckStatus.FAIL,
                                    frame=frame,
                                    message=msg,
                                    details={"edge_iou": edge_iou},
                                )
                            )
                except (EXRIOError, OSError) as exc:
                    msg = f"Frame {frame} reference pass unreadable: {exc}"
                    reasons.append(msg)
                    report.add_check(
                        CheckResult(
                            check_name="edge_drift",
                            status=CheckStatus.FAIL,
                            frame=frame,
                            message=msg,
                        )
                    )

            frame_metrics[frame] = FrameQualityMetrics(
                frame=frame,
                width=width,
                height=height,
                channels=channels,
                has_nan=has_nan,
                has_inf=has_inf,
                pixel_std=pixel_std,
                edge_iou=edge_iou,
            )
            if not (has_nan or has_inf):
                loaded_buffers[frame] = arr
            if reasons:
                frame_reasons[frame] = reasons

        # Inter-frame temporal flicker detection across consecutive finite frames.
        prev_frame: int | None = None
        for item in ordered_frames:
            curr_frame = item.frame
            if curr_frame not in loaded_buffers:
                prev_frame = None
                continue
            if prev_frame is not None and prev_frame in loaded_buffers:
                prev_arr = loaded_buffers[prev_frame]
                curr_arr = loaded_buffers[curr_frame]
                if prev_arr.shape == curr_arr.shape:
                    delta = compute_temporal_delta(prev_arr, curr_arr)
                    frame_metrics[curr_frame].temporal_delta = delta
                    if delta > thresholds.max_temporal_delta:
                        msg = (
                            f"Frame {curr_frame} temporal delta vs frame {prev_frame} "
                            f"is {delta:.4f} > max {thresholds.max_temporal_delta:.4f}"
                        )
                        frame_reasons.setdefault(curr_frame, []).append(msg)
                        report.add_check(
                            CheckResult(
                                check_name="temporal_delta",
                                status=CheckStatus.FAIL,
                                frame=curr_frame,
                                message=msg,
                                details={
                                    "prev_frame": prev_frame,
                                    "temporal_delta": delta,
                                },
                            )
                        )
            prev_frame = curr_frame

        passed_frames = [
            item.frame for item in ordered_frames if item.frame not in frame_reasons
        ]
        if not frame_reasons:
            report.add_check(
                CheckResult(
                    check_name="postflight_summary",
                    status=CheckStatus.PASS,
                    message=f"All {len(passed_frames)} frame(s) passed post-flight quality gates",
                )
            )
        else:
            report.add_check(
                CheckResult(
                    check_name="postflight_summary",
                    status=CheckStatus.FAIL,
                    message=(
                        f"{len(frame_reasons)} frame(s) failed post-flight validation "
                        f"and were routed to quarantine"
                    ),
                    details={"failed_frames": sorted(frame_reasons)},
                )
            )

        return PostflightResult(
            report=report,
            frame_metrics=frame_metrics,
            passed_frames=passed_frames,
            failed_frames=frame_reasons,
        )
