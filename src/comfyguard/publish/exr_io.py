"""OpenEXR and image buffer I/O adapter with OIIO support and pure-Python EXR v2 fallback."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, UnidentifiedImageError

_EXR_MAGIC = b"\x76\x2f\x31\x01"
_NATURAL_CHANNEL_ORDER = {"R": 0, "G": 1, "B": 2, "A": 3, "Z": 4, "Y": 5}


class EXRIOError(Exception):
    """Raised when reading or writing an EXR or image buffer fails."""


def _default_channel_names(num_channels: int) -> list[str]:
    defaults = {
        1: ["Y"],
        2: ["R", "G"],
        3: ["R", "G", "B"],
        4: ["R", "G", "B", "A"],
    }
    if num_channels in defaults:
        return defaults[num_channels]
    return [f"ch{i}" for i in range(num_channels)]


def _pack_exr_attr(name: str, attr_type: str, payload: bytes) -> bytes:
    return (
        name.encode("utf-8")
        + b"\x00"
        + attr_type.encode("ascii")
        + b"\x00"
        + struct.pack("<i", len(payload))
        + payload
    )


def _read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end == -1:
        raise EXRIOError("Unterminated C-string in OpenEXR header")
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def _parse_chlist(payload: bytes) -> list[tuple[str, int]]:
    channels: list[tuple[str, int]] = []
    pos = 0
    while pos < len(payload):
        if payload[pos : pos + 1] == b"\x00":
            break
        name, pos = _read_cstring(payload, pos)
        if pos + 16 > len(payload):
            raise EXRIOError(f"Truncated OpenEXR channel descriptor for '{name}'")
        pixel_type = struct.unpack_from("<i", payload, pos)[0]
        channels.append((name, pixel_type))
        pos += 16
    return channels


def _write_exr_oiio(
    path: Path,
    arr: np.ndarray,
    channels: list[str],
    attributes: dict[str, str],
) -> bool:
    try:
        import OpenImageIO as oiio  # type: ignore[import-not-found]
    except ImportError:
        return False

    height, width, count = arr.shape
    spec = oiio.ImageSpec(width, height, count, oiio.FLOAT)
    spec.channelnames = tuple(channels)
    for key, val in attributes.items():
        spec.attribute(key, str(val))

    out = oiio.ImageOutput.create(str(path))
    if out is None:
        raise EXRIOError(f"OpenImageIO could not create '{path}': {oiio.geterror()}")
    try:
        if not out.open(str(path), spec):
            raise EXRIOError(f"OpenImageIO failed opening '{path}': {out.geterror()}")
        if not out.write_image(np.ascontiguousarray(arr, dtype=np.float32)):
            raise EXRIOError(f"OpenImageIO failed writing '{path}': {out.geterror()}")
    finally:
        out.close()
    return True


def _read_exr_oiio(path: Path) -> tuple[np.ndarray, list[str], dict[str, str]] | None:
    try:
        import OpenImageIO as oiio  # type: ignore[import-not-found]
    except ImportError:
        return None

    inp = oiio.ImageInput.open(str(path))
    if inp is None:
        raise EXRIOError(f"OpenImageIO could not open '{path}': {oiio.geterror()}")
    try:
        spec = inp.spec()
        pixels = inp.read_image(format=oiio.FLOAT)
        if pixels is None:
            raise EXRIOError(f"OpenImageIO failed reading pixels from '{path}'")
        arr = np.asarray(pixels, dtype=np.float32)
        if arr.ndim == 2:
            arr = arr[..., np.newaxis]
        attrs: dict[str, str] = {}
        for param in spec.extra_attribs:
            if isinstance(param.value, str):
                attrs[str(param.name)] = param.value
        return arr, list(spec.channelnames), attrs
    finally:
        inp.close()


def write_exr(
    path: str | Path,
    pixels: np.ndarray,
    *,
    channels: list[str] | None = None,
    attributes: dict[str, str] | None = None,
) -> Path:
    """Write a float32 OpenEXR image with custom string metadata attributes."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    arr = np.asarray(pixels, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr[..., np.newaxis]
    if arr.ndim != 3:
        raise EXRIOError(f"Expected 2D or 3D pixel array, got shape {arr.shape}")

    height, width, num_channels = arr.shape
    if height <= 0 or width <= 0 or num_channels <= 0:
        raise EXRIOError(f"Invalid image dimensions {arr.shape}")

    ch_names = list(channels) if channels is not None else _default_channel_names(num_channels)
    if len(ch_names) != num_channels:
        raise EXRIOError(
            f"Channel count mismatch: {len(ch_names)} names for {num_channels} channels"
        )
    attrs = {str(k): str(v) for k, v in (attributes or {}).items()}

    if _write_exr_oiio(target, arr, ch_names, attrs):
        return target

    # Pure-Python OpenEXR v2 uncompressed float32 scanline writer.
    sorted_pairs = sorted(enumerate(ch_names), key=lambda pair: pair[1])
    chlist_payload = bytearray()
    for _, ch_name in sorted_pairs:
        chlist_payload.extend(ch_name.encode("utf-8") + b"\x00")
        chlist_payload.extend(struct.pack("<iB3Bii", 2, 0, 0, 0, 0, 1, 1))
    chlist_payload.append(0)

    box2i = struct.pack("<iiii", 0, 0, width - 1, height - 1)
    header = bytearray()
    header.extend(_EXR_MAGIC)
    header.extend(struct.pack("<I", 2))
    header.extend(_pack_exr_attr("channels", "chlist", bytes(chlist_payload)))
    header.extend(_pack_exr_attr("compression", "compression", struct.pack("<B", 0)))
    header.extend(_pack_exr_attr("dataWindow", "box2i", box2i))
    header.extend(_pack_exr_attr("displayWindow", "box2i", box2i))
    header.extend(_pack_exr_attr("lineOrder", "lineOrder", struct.pack("<B", 0)))
    header.extend(_pack_exr_attr("pixelAspectRatio", "float", struct.pack("<f", 1.0)))
    header.extend(_pack_exr_attr("screenWindowCenter", "v2f", struct.pack("<ff", 0.0, 0.0)))
    header.extend(_pack_exr_attr("screenWindowWidth", "float", struct.pack("<f", 1.0)))
    for key in sorted(attrs):
        val_bytes = attrs[key].encode("utf-8")
        header.extend(_pack_exr_attr(key, "string", val_bytes))
    header.append(0)

    offset_table_start = len(header)
    scanline_data_size = width * num_channels * 4
    scanline_block_size = 8 + scanline_data_size
    first_block_offset = offset_table_start + height * 8

    offsets = bytearray()
    for y in range(height):
        offsets.extend(struct.pack("<Q", first_block_offset + y * scanline_block_size))

    sorted_indices = [idx for idx, _ in sorted_pairs]
    # Transpose per row into planar alphabetical channel order: (H, C_sorted, W)
    planar = np.ascontiguousarray(arr[:, :, sorted_indices].transpose(0, 2, 1), dtype="<f4")

    with target.open("wb") as fh:
        fh.write(header)
        fh.write(offsets)
        for y in range(height):
            fh.write(struct.pack("<ii", y, scanline_data_size))
            fh.write(planar[y].tobytes())

    return target


def _parse_exr_header_bytes(
    data: bytes, path: Path
) -> tuple[int, int, list[tuple[str, int]], int, dict[str, str], int]:
    if len(data) < 8 or data[:4] != _EXR_MAGIC:
        raise EXRIOError(f"Not a valid OpenEXR file: {path}")

    pos = 8
    width: int | None = None
    height: int | None = None
    channels: list[tuple[str, int]] = []
    compression = 0
    attrs: dict[str, str] = {}

    while pos < len(data):
        if data[pos : pos + 1] == b"\x00":
            pos += 1
            break
        attr_name, pos = _read_cstring(data, pos)
        attr_type, pos = _read_cstring(data, pos)
        if pos + 4 > len(data):
            raise EXRIOError(f"Truncated attribute '{attr_name}' in '{path}'")
        attr_size = struct.unpack_from("<i", data, pos)[0]
        pos += 4
        if attr_size < 0 or pos + attr_size > len(data):
            raise EXRIOError(f"Invalid attribute size for '{attr_name}' in '{path}'")
        payload = data[pos : pos + attr_size]
        pos += attr_size

        if attr_name == "dataWindow" and attr_type == "box2i" and attr_size == 16:
            x_min, y_min, x_max, y_max = struct.unpack("<iiii", payload)
            width = x_max - x_min + 1
            height = y_max - y_min + 1
        elif attr_name == "channels" and attr_type == "chlist":
            channels = _parse_chlist(payload)
        elif attr_name == "compression" and attr_type == "compression" and attr_size == 1:
            compression = payload[0]
        elif attr_type == "string":
            attrs[attr_name] = payload.decode("utf-8", errors="replace")

    if width is None or height is None or width <= 0 or height <= 0:
        raise EXRIOError(f"OpenEXR header missing valid dataWindow: {path}")
    if not channels:
        raise EXRIOError(f"OpenEXR header missing channels list: {path}")
    return width, height, channels, compression, attrs, pos


def read_exr_attributes(path: str | Path) -> dict[str, str]:
    """Read custom string metadata attributes from an OpenEXR file."""
    src = Path(path)
    if not src.is_file():
        raise EXRIOError(f"EXR file does not exist: {src}")

    try:
        import OpenImageIO as oiio  # type: ignore[import-not-found]
    except ImportError:
        oiio = None

    if oiio is not None:
        inp = oiio.ImageInput.open(str(src))
        if inp is None:
            raise EXRIOError(f"OpenImageIO could not open '{src}': {oiio.geterror()}")
        try:
            spec = inp.spec()
            return {
                str(p.name): p.value
                for p in spec.extra_attribs
                if isinstance(p.value, str)
            }
        finally:
            inp.close()

    raw = src.read_bytes()
    _, _, _, _, attrs, _ = _parse_exr_header_bytes(raw, src)
    return attrs


def read_exr(path: str | Path) -> tuple[np.ndarray, list[str], dict[str, str]]:
    """Read pixels (float32 HxWxC), channel names, and string attributes from an EXR file."""
    src = Path(path)
    if not src.is_file():
        raise EXRIOError(f"EXR file does not exist: {src}")

    oiio_res = _read_exr_oiio(src)
    if oiio_res is not None:
        return oiio_res

    raw = src.read_bytes()
    width, height, channels, compression, attrs, offset_table_pos = _parse_exr_header_bytes(
        raw, src
    )
    if compression != 0:
        raise EXRIOError(
            f"Compressed OpenEXR (compression={compression}) requires OpenImageIO: {src}"
        )

    offset_table_end = offset_table_pos + height * 8
    if offset_table_end > len(raw):
        raise EXRIOError(f"Truncated OpenEXR scanline offset table: {src}")

    offsets = struct.unpack_from(f"<{height}Q", raw, offset_table_pos)
    num_channels = len(channels)
    out = np.empty((height, width, num_channels), dtype=np.float32)

    dtype_map: dict[int, tuple[Any, int]] = {
        0: ("<u4", 4),
        1: ("<f2", 2),
        2: ("<f4", 4),
    }

    for row_idx, offset in enumerate(offsets):
        if offset + 8 > len(raw):
            raise EXRIOError(f"Truncated scanline header at row {row_idx} in '{src}'")
        scan_y, data_size = struct.unpack_from("<ii", raw, offset)
        cursor = offset + 8
        if cursor + data_size > len(raw):
            raise EXRIOError(f"Truncated scanline payload at row {scan_y} in '{src}'")
        target_row = scan_y if 0 <= scan_y < height else row_idx
        for ch_idx, (ch_name, ptype) in enumerate(channels):
            if ptype not in dtype_map:
                raise EXRIOError(f"Unsupported pixel type {ptype} for channel '{ch_name}'")
            np_dtype, elem_bytes = dtype_map[ptype]
            byte_len = width * elem_bytes
            if cursor + byte_len > len(raw):
                raise EXRIOError(f"Scanline underrun reading '{ch_name}' in '{src}'")
            row_vals = np.frombuffer(raw, dtype=np_dtype, count=width, offset=cursor)
            out[target_row, :, ch_idx] = row_vals.astype(np.float32)
            cursor += byte_len

    # Reorder standard channels (e.g., B,G,R -> R,G,B) for consistent RGB indexing.
    ch_names = [name for name, _ in channels]
    indexed = sorted(
        enumerate(ch_names),
        key=lambda pair: (_NATURAL_CHANNEL_ORDER.get(pair[1], 100), pair[1]),
    )
    reorder = [idx for idx, _ in indexed]
    ordered_names = [name for _, name in indexed]
    return np.ascontiguousarray(out[:, :, reorder]), ordered_names, attrs


def load_image_buffer(path: str | Path) -> tuple[np.ndarray, list[str]]:
    """Load an EXR or standard image (PNG/TIFF/JPEG) into float32 (H, W, C) + channel names."""
    src = Path(path)
    if not src.is_file():
        raise EXRIOError(f"Image file does not exist: {src}")
    if src.stat().st_size == 0:
        raise EXRIOError(f"Image file is empty (0 bytes): {src}")

    with src.open("rb") as fh:
        magic = fh.read(4)
    if magic == _EXR_MAGIC:
        arr, channels, _ = read_exr(src)
        return arr, channels

    try:
        with Image.open(src) as img:
            bands = list(img.getbands())
            raw_arr = np.asarray(img)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise EXRIOError(f"Failed to decode image '{src}': {exc}") from exc

    if raw_arr.ndim == 2:
        raw_arr = raw_arr[..., np.newaxis]

    if np.issubdtype(raw_arr.dtype, np.integer):
        max_val = float(np.iinfo(raw_arr.dtype).max)
        arr = raw_arr.astype(np.float32) / max_val
    else:
        arr = raw_arr.astype(np.float32)

    return arr, bands
