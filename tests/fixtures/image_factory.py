"""Lightweight test image writers for PNG and OpenEXR headers."""

from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image


def write_png(path: str | Path, width: int, height: int, mode: str = "RGB") -> Path:
    """Create a valid PNG file with the specified dimensions and channel mode."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new(mode, (width, height))
    img.save(target, format="PNG")
    return target


def _pack_exr_attr(name: str, attr_type: str, payload: bytes) -> bytes:
    return (
        name.encode("ascii")
        + b"\x00"
        + attr_type.encode("ascii")
        + b"\x00"
        + struct.pack("<i", len(payload))
        + payload
    )


def write_exr_header(
    path: str | Path,
    width: int,
    height: int,
    channels: list[str] | None = None,
) -> Path:
    """Write a structurally valid OpenEXR v2 header + dummy scanline offset table."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    channel_names = sorted(channels if channels is not None else ["B", "G", "R"])

    chlist_parts = bytearray()
    for ch in channel_names:
        # pixel_type=1 (HALF), pLinear=0, reserved=3 bytes, xSampling=1, ySampling=1
        chlist_parts.extend(ch.encode("ascii") + b"\x00")
        chlist_parts.extend(struct.pack("<iB3Bii", 1, 0, 0, 0, 0, 1, 1))
    chlist_parts.append(0)

    box2i = struct.pack("<iiii", 0, 0, width - 1, height - 1)
    header = bytearray()
    header.extend(b"\x76\x2f\x31\x01")
    header.extend(struct.pack("<I", 2))
    header.extend(_pack_exr_attr("channels", "chlist", bytes(chlist_parts)))
    header.extend(_pack_exr_attr("compression", "compression", struct.pack("<B", 0)))
    header.extend(_pack_exr_attr("dataWindow", "box2i", box2i))
    header.extend(_pack_exr_attr("displayWindow", "box2i", box2i))
    header.extend(_pack_exr_attr("lineOrder", "lineOrder", struct.pack("<B", 0)))
    header.extend(_pack_exr_attr("pixelAspectRatio", "float", struct.pack("<f", 1.0)))
    header.extend(_pack_exr_attr("screenWindowCenter", "v2f", struct.pack("<ff", 0.0, 0.0)))
    header.extend(_pack_exr_attr("screenWindowWidth", "float", struct.pack("<f", 1.0)))
    header.append(0)  # end of header
    # Minimal scanline offset table stub
    header.extend(b"\x00" * (8 * min(height, 4)))

    target.write_bytes(bytes(header))
    return target
