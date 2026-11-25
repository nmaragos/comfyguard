"""Pre-flight contract checks: resolution, frame continuity, channels, and models."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from comfyguard.broker.mutator import GraphMutationError, GraphMutator
from comfyguard.contracts.manifest import CheckResult, CheckStatus, ValidationReport
from comfyguard.contracts.schema import JobRequest, WorkflowContract

_EXR_MAGIC = b"\x76\x2f\x31\x01"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PNG_COLOR_TYPE_CHANNELS: dict[int, list[str]] = {
    0: ["Y"],
    2: ["R", "G", "B"],
    3: ["I"],
    4: ["Y", "A"],
    6: ["R", "G", "B", "A"],
}


class ImageInspectionError(Exception):
    """Raised when an image header cannot be parsed or is corrupted."""


class ImageInfo(BaseModel):
    """Lightweight image header metadata extracted without decoding full pixel buffers."""

    path: Path
    width: int
    height: int
    channels: list[str] = Field(default_factory=list)
    format: str


def _read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end == -1:
        raise ImageInspectionError("Unterminated C-string in OpenEXR header")
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def _parse_exr_chlist(payload: bytes) -> list[str]:
    channels: list[str] = []
    pos = 0
    while pos < len(payload):
        if payload[pos : pos + 1] == b"\x00":
            break
        ch_name, pos = _read_cstring(payload, pos)
        if pos + 16 > len(payload):
            raise ImageInspectionError("Truncated OpenEXR channel entry")
        channels.append(ch_name)
        pos += 16
    return channels


def _inspect_exr_header(path: Path) -> ImageInfo:
    with path.open("rb") as fh:
        header = fh.read(65536)

    if len(header) < 8 or header[:4] != _EXR_MAGIC:
        raise ImageInspectionError(f"Not a valid OpenEXR file: {path}")

    version_field = struct.unpack_from("<I", header, 4)[0]
    is_multipart = bool(version_field & 0x1000)
    pos = 8
    if is_multipart:
        # Multipart files carry a 0-length string before attributes in non-single-part layouts;
        # still parse the first header cleanly if present.
        pass

    width: int | None = None
    height: int | None = None
    channels: list[str] = []

    while pos < len(header):
        if header[pos : pos + 1] == b"\x00":
            break
        attr_name, pos = _read_cstring(header, pos)
        attr_type, pos = _read_cstring(header, pos)
        if pos + 4 > len(header):
            raise ImageInspectionError(f"Truncated OpenEXR attribute '{attr_name}' in {path}")
        attr_size = struct.unpack_from("<i", header, pos)[0]
        pos += 4
        if attr_size < 0 or pos + attr_size > len(header):
            raise ImageInspectionError(f"Invalid OpenEXR attribute size for '{attr_name}'")
        payload = header[pos : pos + attr_size]
        pos += attr_size

        if attr_name == "dataWindow" and attr_type == "box2i" and attr_size == 16:
            x_min, y_min, x_max, y_max = struct.unpack("<iiii", payload)
            width = x_max - x_min + 1
            height = y_max - y_min + 1
        elif attr_name == "channels" and attr_type == "chlist":
            channels = _parse_exr_chlist(payload)

    if width is None or height is None or width <= 0 or height <= 0:
        raise ImageInspectionError(f"OpenEXR header missing valid dataWindow: {path}")

    return ImageInfo(path=path, width=width, height=height, channels=channels, format="EXR")


def _inspect_png_header(path: Path) -> ImageInfo:
    with path.open("rb") as fh:
        header = fh.read(29)
    if len(header) < 26 or header[:8] != _PNG_MAGIC or header[12:16] != b"IHDR":
        raise ImageInspectionError(f"Invalid PNG IHDR header: {path}")
    width, height = struct.unpack(">II", header[16:24])
    color_type = header[25]
    if width <= 0 or height <= 0:
        raise ImageInspectionError(f"Invalid PNG dimensions ({width}x{height}): {path}")
    channels = _PNG_COLOR_TYPE_CHANNELS.get(color_type, [])
    return ImageInfo(path=path, width=width, height=height, channels=channels, format="PNG")


def _inspect_with_oiio(path: Path) -> ImageInfo | None:
    try:
        import OpenImageIO as oiio  # type: ignore[import-not-found]
    except ImportError:
        return None

    inp = oiio.ImageInput.open(str(path))
    if inp is None:
        raise ImageInspectionError(f"OpenImageIO failed to open '{path}': {oiio.geterror()}")
    try:
        spec = inp.spec()
        return ImageInfo(
            path=path,
            width=int(spec.width),
            height=int(spec.height),
            channels=list(spec.channelnames),
            format=str(inp.format_name()).upper(),
        )
    finally:
        inp.close()


def inspect_image_header(path: str | Path) -> ImageInfo:
    """Inspect image dimensions and channels (OIIO when available, else pure-Python/Pillow)."""
    img_path = Path(path)
    if not img_path.is_file():
        raise ImageInspectionError(f"Image file does not exist: {img_path}")
    if img_path.stat().st_size == 0:
        raise ImageInspectionError(f"Image file is empty (0 bytes): {img_path}")

    oiio_info = _inspect_with_oiio(img_path)
    if oiio_info is not None:
        return oiio_info

    with img_path.open("rb") as fh:
        magic = fh.read(8)

    if magic[:4] == _EXR_MAGIC:
        return _inspect_exr_header(img_path)
    if magic == _PNG_MAGIC:
        return _inspect_png_header(img_path)

    try:
        with Image.open(img_path) as img:
            width, height = img.size
            channels = list(img.getbands())
            fmt = (img.format or img_path.suffix.lstrip(".")).upper()
            return ImageInfo(
                path=img_path, width=width, height=height, channels=channels, format=fmt
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageInspectionError(f"Cannot inspect image '{img_path}': {exc}") from exc


def _compute_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_object_info_choices(node_meta: dict[str, Any], input_name: str) -> list[str] | None:
    input_section = node_meta.get("input", {})
    if not isinstance(input_section, dict):
        return None
    for group in ("required", "optional"):
        group_inputs = input_section.get(group, {})
        if isinstance(group_inputs, dict) and input_name in group_inputs:
            spec = group_inputs[input_name]
            if isinstance(spec, list) and spec and isinstance(spec[0], list):
                return [str(item) for item in spec[0]]
    return None


class PreflightValidator:
    """Validates job inputs, parameters, and worker dependencies before GPU dispatch."""

    def __init__(
        self,
        image_inspector: Callable[[Path], ImageInfo] = inspect_image_header,
    ) -> None:
        self._image_inspector = image_inspector

    def validate(
        self,
        job: JobRequest,
        contract: WorkflowContract,
        template_graph: dict[str, Any] | None = None,
        object_info: dict[str, Any] | None = None,
        model_hashes: dict[str, str] | None = None,
        models_dir: Path | None = None,
        skip_model_check: bool = False,
    ) -> ValidationReport:
        """Run all pre-flight gates without short-circuiting so all issues are reported."""
        report = ValidationReport(stage="preflight")

        self._check_contract_params(report, job, contract, template_graph)
        inspected_frames = self._check_frame_continuity_and_inspect(report, job, contract)
        self._check_resolution_and_channels(report, contract, inspected_frames)

        if skip_model_check:
            report.add_check(
                CheckResult(
                    check_name="model_availability",
                    status=CheckStatus.SKIP,
                    message="Model availability and hash checks skipped by caller",
                )
            )
        else:
            self._check_models(report, contract, object_info, model_hashes, models_dir)

        return report

    def _check_contract_params(
        self,
        report: ValidationReport,
        job: JobRequest,
        contract: WorkflowContract,
        template_graph: dict[str, Any] | None,
    ) -> None:
        try:
            if template_graph is not None:
                mutator = GraphMutator(contract=contract, template_graph=template_graph)
                mutator.validate_params(job.params, include_per_frame=False)
            else:
                unknown = sorted(set(job.params) - set(contract.exposed_params))
                if unknown:
                    raise GraphMutationError(
                        f"Unknown parameter(s) not declared in contract: {', '.join(unknown)}"
                    )
                for name, spec in contract.exposed_params.items():
                    if spec.per_frame and name not in job.params:
                        continue
                    if name in job.params:
                        spec.validate_value(job.params[name], param_name=name)
                    elif spec.default is not None:
                        spec.validate_value(spec.default, param_name=name)
                    elif spec.required:
                        raise GraphMutationError(f"Missing required parameter '{name}'")

            if contract.seed_param is not None and contract.seed_param in contract.exposed_params:
                seed_spec = contract.exposed_params[contract.seed_param]
                for frame in job.frame_range.frames():
                    seed_spec.validate_value(
                        job.base_seed + frame,
                        param_name=contract.seed_param,
                    )

            report.add_check(
                CheckResult(
                    check_name="contract_params",
                    status=CheckStatus.PASS,
                    message="Job parameters and per-frame seeds satisfy contract bounds",
                )
            )
        except (GraphMutationError, ValueError) as exc:
            report.add_check(
                CheckResult(
                    check_name="contract_params",
                    status=CheckStatus.FAIL,
                    message=str(exc),
                )
            )

    def _check_frame_continuity_and_inspect(
        self,
        report: ValidationReport,
        job: JobRequest,
        contract: WorkflowContract,
    ) -> list[tuple[str, int, ImageInfo]]:
        inspected: list[tuple[str, int, ImageInfo]] = []
        missing_aovs: list[str] = []
        missing_files: list[dict[str, Any]] = []
        empty_files: list[dict[str, Any]] = []
        corrupt_files: list[dict[str, Any]] = []

        for aov in contract.required_aovs:
            if aov.required and aov.name not in job.input_sequences:
                missing_aovs.append(aov.name)

        if missing_aovs:
            report.add_check(
                CheckResult(
                    check_name="frame_continuity",
                    status=CheckStatus.FAIL,
                    message=f"Missing required input AOV(s): {', '.join(missing_aovs)}",
                    details={"missing_aovs": missing_aovs},
                )
            )

        frames = job.frame_range.frames()
        for aov_name in sorted(job.input_sequences):
            for frame in frames:
                frame_path = job.resolve_input_frame(aov_name, frame)
                if not frame_path.is_file():
                    missing_files.append(
                        {"aov": aov_name, "frame": frame, "path": str(frame_path)}
                    )
                    continue
                if frame_path.stat().st_size == 0:
                    empty_files.append(
                        {"aov": aov_name, "frame": frame, "path": str(frame_path)}
                    )
                    continue
                try:
                    info = self._image_inspector(frame_path)
                    inspected.append((aov_name, frame, info))
                except ImageInspectionError as exc:
                    corrupt_files.append(
                        {
                            "aov": aov_name,
                            "frame": frame,
                            "path": str(frame_path),
                            "error": str(exc),
                        }
                    )

        if missing_files or empty_files or corrupt_files:
            parts: list[str] = []
            if missing_files:
                parts.append(f"{len(missing_files)} missing")
            if empty_files:
                parts.append(f"{len(empty_files)} empty")
            if corrupt_files:
                parts.append(f"{len(corrupt_files)} unreadable")
            report.add_check(
                CheckResult(
                    check_name="frame_continuity",
                    status=CheckStatus.FAIL,
                    message=f"Input sequence integrity failed ({', '.join(parts)})",
                    details={
                        "missing": missing_files,
                        "empty": empty_files,
                        "corrupt": corrupt_files,
                    },
                )
            )
        elif not missing_aovs:
            report.add_check(
                CheckResult(
                    check_name="frame_continuity",
                    status=CheckStatus.PASS,
                    message=(
                        f"Verified {len(frames)} frame(s) across "
                        f"{len(job.input_sequences)} AOV sequence(s)"
                    ),
                )
            )

        return inspected

    def _check_resolution_and_channels(
        self,
        report: ValidationReport,
        contract: WorkflowContract,
        inspected: list[tuple[str, int, ImageInfo]],
    ) -> None:
        if not inspected:
            return

        res = contract.resolution
        alignment_failures: list[dict[str, Any]] = []
        observed_dims: set[tuple[int, int]] = set()
        aov_lookup = {aov.name: aov for aov in contract.required_aovs}
        channel_failures: list[dict[str, Any]] = []

        for aov_name, frame, info in inspected:
            observed_dims.add((info.width, info.height))
            reasons: list[str] = []
            if info.width % res.multiple_of != 0 or info.height % res.multiple_of != 0:
                reasons.append(
                    f"dimensions {info.width}x{info.height} not divisible by {res.multiple_of}"
                )
            if not (res.min_width <= info.width <= res.max_width):
                reasons.append(
                    f"width {info.width} outside [{res.min_width}, {res.max_width}]"
                )
            if not (res.min_height <= info.height <= res.max_height):
                reasons.append(
                    f"height {info.height} outside [{res.min_height}, {res.max_height}]"
                )
            if res.expected_aspect_ratio is not None:
                tol = res.aspect_ratio_tolerance or 0.01
                actual_ar = info.width / info.height
                if abs(actual_ar - res.expected_aspect_ratio) > tol:
                    reasons.append(
                        f"aspect ratio {actual_ar:.4f} differs from "
                        f"{res.expected_aspect_ratio:.4f} +/- {tol}"
                    )
            if reasons:
                alignment_failures.append(
                    {
                        "aov": aov_name,
                        "frame": frame,
                        "width": info.width,
                        "height": info.height,
                        "reasons": reasons,
                    }
                )

            aov_req = aov_lookup.get(aov_name)
            if aov_req and aov_req.required_channels and info.channels:
                missing_ch = [c for c in aov_req.required_channels if c not in info.channels]
                if missing_ch:
                    channel_failures.append(
                        {
                            "aov": aov_name,
                            "frame": frame,
                            "missing_channels": missing_ch,
                            "observed_channels": info.channels,
                        }
                    )

        if alignment_failures:
            first = alignment_failures[0]
            report.add_check(
                CheckResult(
                    check_name="resolution_alignment",
                    status=CheckStatus.FAIL,
                    frame=first["frame"],
                    message=(
                        f"{first['aov']} frame {first['frame']}: "
                        + "; ".join(first["reasons"])
                    ),
                    details={"failures": alignment_failures},
                )
            )
        else:
            sample_w, sample_h = next(iter(observed_dims))
            report.add_check(
                CheckResult(
                    check_name="resolution_alignment",
                    status=CheckStatus.PASS,
                    message=(
                        f"All frames align to {res.multiple_of}px grid "
                        f"and bounds ({sample_w}x{sample_h})"
                    ),
                )
            )

        if len(observed_dims) > 1:
            dims_list = [f"{w}x{h}" for w, h in sorted(observed_dims)]
            report.add_check(
                CheckResult(
                    check_name="resolution_consistency",
                    status=CheckStatus.FAIL,
                    message=f"Inconsistent frame resolutions across AOVs/frames: {dims_list}",
                    details={"observed_resolutions": dims_list},
                )
            )
        else:
            report.add_check(
                CheckResult(
                    check_name="resolution_consistency",
                    status=CheckStatus.PASS,
                    message="All input frames and AOVs share matching dimensions",
                )
            )

        if channel_failures:
            first_ch = channel_failures[0]
            report.add_check(
                CheckResult(
                    check_name="aov_channels",
                    status=CheckStatus.FAIL,
                    frame=first_ch["frame"],
                    message=(
                        f"AOV '{first_ch['aov']}' frame {first_ch['frame']} missing channels "
                        f"{first_ch['missing_channels']} (found {first_ch['observed_channels']})"
                    ),
                    details={"failures": channel_failures},
                )
            )
        else:
            report.add_check(
                CheckResult(
                    check_name="aov_channels",
                    status=CheckStatus.PASS,
                    message="Required AOV channels satisfied",
                )
            )

    def _check_models(
        self,
        report: ValidationReport,
        contract: WorkflowContract,
        object_info: dict[str, Any] | None,
        model_hashes: dict[str, str] | None,
        models_dir: Path | None,
    ) -> None:
        if not contract.required_models:
            report.add_check(
                CheckResult(
                    check_name="model_availability",
                    status=CheckStatus.PASS,
                    message="No model dependencies declared in contract",
                )
            )
            return

        if object_info is None:
            report.add_check(
                CheckResult(
                    check_name="model_availability",
                    status=CheckStatus.FAIL,
                    message="Missing ComfyUI /object_info payload for model availability check",
                )
            )
        else:
            missing_models: list[str] = []
            for model in contract.required_models:
                node_meta = object_info.get(model.node_class)
                if not isinstance(node_meta, dict):
                    missing_models.append(
                        f"{model.filename} (node class '{model.node_class}' missing)"
                    )
                    continue
                choices = _extract_object_info_choices(node_meta, model.input_name)
                if choices is None:
                    missing_models.append(
                        f"{model.filename} (input '{model.node_class}.{model.input_name}' missing)"
                    )
                    continue
                if model.filename not in choices:
                    missing_models.append(
                        f"{model.filename} (not listed in {model.node_class}.{model.input_name})"
                    )

            if missing_models:
                report.add_check(
                    CheckResult(
                        check_name="model_availability",
                        status=CheckStatus.FAIL,
                        message=f"Unavailable model(s): {'; '.join(missing_models)}",
                        details={"missing_models": missing_models},
                    )
                )
            else:
                report.add_check(
                    CheckResult(
                        check_name="model_availability",
                        status=CheckStatus.PASS,
                        message=(
                            f"All {len(contract.required_models)} required model(s) "
                            "present in /object_info"
                        ),
                    )
                )

        models_requiring_hash = [m for m in contract.required_models if m.sha256 is not None]
        if not models_requiring_hash:
            return

        hash_failures: list[str] = []
        for model in models_requiring_hash:
            actual_hash = _resolve_model_hash(model.filename, model_hashes, models_dir)
            if actual_hash is None:
                hash_failures.append(
                    f"{model.filename}: no hash source available to verify {model.sha256[:12]}..."
                )
            elif actual_hash.lower() != (model.sha256 or "").lower():
                hash_failures.append(
                    f"{model.filename}: SHA-256 mismatch "
                    f"(expected {model.sha256[:12]}..., got {actual_hash[:12]}...)"
                )

        if hash_failures:
            report.add_check(
                CheckResult(
                    check_name="model_hash",
                    status=CheckStatus.FAIL,
                    message=f"Model hash verification failed: {'; '.join(hash_failures)}",
                    details={"failures": hash_failures},
                )
            )
        else:
            report.add_check(
                CheckResult(
                    check_name="model_hash",
                    status=CheckStatus.PASS,
                    message=f"Verified SHA-256 for {len(models_requiring_hash)} required model(s)",
                )
            )


def _resolve_model_hash(
    filename: str,
    model_hashes: dict[str, str] | None,
    models_dir: Path | None,
) -> str | None:
    if model_hashes and filename in model_hashes:
        return model_hashes[filename]
    if models_dir is not None and models_dir.is_dir():
        direct = models_dir / filename
        if direct.is_file():
            return _compute_file_sha256(direct)
        for match in models_dir.rglob(filename):
            if match.is_file():
                return _compute_file_sha256(match)
    return None
