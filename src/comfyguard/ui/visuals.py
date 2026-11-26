"""Visual preview encoding and Sobel edge-drift / temporal diagnostic renderers."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import numpy as np
from PIL import Image

from comfyguard.publish.exr_io import load_image_buffer
from comfyguard.validation.postflight import (
    compute_edge_iou,
    compute_sobel_edges,
    compute_temporal_delta,
)


def _to_rgb_uint8(arr: np.ndarray) -> np.ndarray:
    data = np.asarray(arr, dtype=np.float32)
    if data.ndim == 2:
        data = data[..., np.newaxis]

    if data.shape[2] == 1:
        rgb = np.repeat(data, 3, axis=2)
    elif data.shape[2] >= 3:
        rgb = data[:, :, :3].copy()
    else:
        pad = np.zeros((*data.shape[:2], 3 - data.shape[2]), dtype=np.float32)
        rgb = np.concatenate([data, pad], axis=2)

    non_finite = ~np.isfinite(rgb).all(axis=2)
    rgb = np.nan_to_num(rgb, nan=0.0, posinf=1.0, neginf=0.0)
    rgb = np.clip(rgb, 0.0, 1.0)
    if np.any(non_finite):
        rgb[non_finite] = np.array([1.0, 0.0, 1.0], dtype=np.float32)
    return (rgb * 255.0).astype(np.uint8)


def array_to_png_data_uri(arr: np.ndarray, *, max_width: int = 640) -> str:
    """Convert a float32/uint8 image array to a base64-encoded PNG data URI."""
    rgb_u8 = _to_rgb_uint8(arr)
    img = Image.fromarray(rgb_u8, mode="RGB")
    if max_width > 0 and img.width > max_width:
        ratio = max_width / img.width
        new_height = max(1, int(round(img.height * ratio)))
        img = img.resize((max_width, new_height), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def image_path_to_data_uri(path: str | Path, *, max_width: int = 640) -> str:
    """Load an EXR or PNG/TIFF file and encode a scaled preview PNG data URI."""
    arr, _ = load_image_buffer(path)
    return array_to_png_data_uri(arr, max_width=max_width)


def _to_luma(arr: np.ndarray) -> np.ndarray:
    if arr.ndim == 2:
        return arr.astype(np.float32)
    if arr.shape[2] >= 3:
        return np.mean(arr[:, :, :3], axis=2, dtype=np.float32)
    return arr[:, :, 0].astype(np.float32)


def render_edge_overlay_data_uri(
    output_path: str | Path,
    reference_path: str | Path,
    *,
    max_width: int = 640,
    edge_threshold: float = 0.2,
) -> tuple[str, float]:
    """Render a color-coded Sobel edge-drift overlay and return (data_uri, edge_iou).

    Legend:
      - Emerald green: matched structural edges (output & reference)
      - Sky cyan: missing CG reference edges (reference & ~output)
      - Crimson red: drifted / hallucinated AI edges (output & ~reference)
    """
    out_arr, _ = load_image_buffer(output_path)
    ref_arr, _ = load_image_buffer(reference_path)

    if out_arr.shape[:2] != ref_arr.shape[:2]:
        return array_to_png_data_uri(out_arr, max_width=max_width), 0.0

    out_gray = _to_luma(np.nan_to_num(out_arr, nan=0.0, posinf=1.0, neginf=0.0))
    ref_gray = _to_luma(np.nan_to_num(ref_arr, nan=0.0, posinf=1.0, neginf=0.0))

    iou = compute_edge_iou(out_arr, ref_arr, edge_threshold=edge_threshold)
    out_edges = compute_sobel_edges(out_gray, threshold=edge_threshold)
    ref_edges = compute_sobel_edges(ref_gray, threshold=edge_threshold)

    backdrop = np.clip(out_gray, 0.0, 1.0) * 0.24
    canvas = np.stack([backdrop, backdrop, backdrop], axis=2)

    matched = out_edges & ref_edges
    missing_ref = ref_edges & ~out_edges
    drifted_ai = out_edges & ~ref_edges

    canvas[matched] = np.array([0.13, 0.80, 0.42], dtype=np.float32)
    canvas[missing_ref] = np.array([0.22, 0.74, 0.97], dtype=np.float32)
    canvas[drifted_ai] = np.array([0.95, 0.26, 0.28], dtype=np.float32)

    return array_to_png_data_uri(canvas, max_width=max_width), iou


def render_temporal_diff_data_uri(
    prev_path: str | Path,
    curr_path: str | Path,
    *,
    max_width: int = 640,
) -> tuple[str, float]:
    """Render an amplified inter-frame temporal difference heatmap and return (uri, delta)."""
    prev_arr, _ = load_image_buffer(prev_path)
    curr_arr, _ = load_image_buffer(curr_path)
    if prev_arr.shape != curr_arr.shape:
        return array_to_png_data_uri(curr_arr, max_width=max_width), 1.0

    delta = compute_temporal_delta(prev_arr, curr_arr)
    diff = np.mean(np.abs(curr_arr - prev_arr), axis=2) if curr_arr.ndim == 3 else np.abs(
        curr_arr - prev_arr
    )
    heat = np.clip(diff * 4.0, 0.0, 1.0)
    rgb = np.stack(
        [
            np.clip(heat * 1.4, 0.04, 1.0),
            np.clip(heat * 0.75 * (1.0 - heat * 0.4), 0.06, 0.85),
            np.clip(0.18 + (1.0 - heat) * 0.25, 0.05, 0.45),
        ],
        axis=2,
    )
    return array_to_png_data_uri(rgb, max_width=max_width), delta
