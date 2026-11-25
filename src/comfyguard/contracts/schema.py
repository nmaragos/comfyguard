"""Pydantic contract schemas and job request definitions for ComfyGuard."""

from __future__ import annotations

import math
import re
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_HASH_PAD_RE = re.compile(r"#+")
_PRINTF_FRAME_RE = re.compile(r"%0?(\d*)d")
_BRACE_FRAME_RE = re.compile(r"\{frame(?::0?(\d+)d)?\}")


class ParamType(StrEnum):
    """Supported parameter types for contract-governed workflow inputs."""

    INT = "int"
    FLOAT = "float"
    STRING = "string"
    BOOL = "bool"
    ENUM = "enum"
    FILE_PATH = "file_path"


class ExposedParam(BaseModel):
    """Schema definition for an exposed ComfyUI node input parameter."""

    node_id: str
    input_name: str
    param_type: ParamType
    required: bool = True
    default: Any = None
    min_value: int | float | None = None
    max_value: int | float | None = None
    choices: list[Any] | None = None
    description: str = ""
    per_frame: bool = False
    is_seed: bool = False
    is_output_prefix: bool = False

    @model_validator(mode="after")
    def _validate_definition(self) -> ExposedParam:
        has_bounds = self.min_value is not None or self.max_value is not None
        if has_bounds and self.param_type not in (ParamType.INT, ParamType.FLOAT):
            raise ValueError(
                f"Numeric bounds are only valid for int/float parameters, got {self.param_type}"
            )
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError(
                f"min_value ({self.min_value}) must not exceed max_value ({self.max_value})"
            )
        if self.param_type == ParamType.ENUM and not self.choices:
            raise ValueError("ENUM parameters require a non-empty 'choices' list")
        if self.default is not None:
            self.validate_value(self.default, param_name="default")
        return self

    def validate_value(self, value: Any, param_name: str = "") -> Any:
        """Validate and normalize a parameter value against type and range rules."""
        label = param_name or f"{self.node_id}.{self.input_name}"
        if value is None:
            if self.required:
                raise ValueError(f"Parameter '{label}' is required and cannot be None")
            return None

        if self.param_type == ParamType.INT:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"Parameter '{label}' expects int, got {type(value).__name__} ({value!r})"
                )
            self._check_numeric_bounds(value, label)
            return value

        if self.param_type == ParamType.FLOAT:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"Parameter '{label}' expects float, got {type(value).__name__} ({value!r})"
                )
            coerced = float(value)
            if not math.isfinite(coerced):
                raise ValueError(f"Parameter '{label}' must be finite, got {value!r}")
            self._check_numeric_bounds(coerced, label)
            return coerced

        if self.param_type == ParamType.BOOL:
            if not isinstance(value, bool):
                raise ValueError(
                    f"Parameter '{label}' expects bool, got {type(value).__name__} ({value!r})"
                )
            return value

        if self.param_type == ParamType.STRING:
            if not isinstance(value, str):
                raise ValueError(
                    f"Parameter '{label}' expects str, got {type(value).__name__} ({value!r})"
                )
            if self.required and not value.strip():
                raise ValueError(f"Parameter '{label}' cannot be an empty string")
            return value

        if self.param_type == ParamType.FILE_PATH:
            if isinstance(value, Path):
                value = str(value)
            if not isinstance(value, str):
                raise ValueError(
                    f"Parameter '{label}' expects file path string, "
                    f"got {type(value).__name__} ({value!r})"
                )
            if self.required and not value.strip():
                raise ValueError(f"Parameter '{label}' cannot be an empty file path")
            return value

        if self.param_type == ParamType.ENUM:
            if not self.choices or value not in self.choices:
                raise ValueError(
                    f"Parameter '{label}' value {value!r} not in allowed choices {self.choices}"
                )
            return value

        raise ValueError(f"Unsupported parameter type: {self.param_type}")

    def _check_numeric_bounds(self, value: int | float, label: str) -> None:
        if self.min_value is not None and value < self.min_value:
            raise ValueError(
                f"Parameter '{label}' value {value} is below minimum {self.min_value}"
            )
        if self.max_value is not None and value > self.max_value:
            raise ValueError(
                f"Parameter '{label}' value {value} exceeds maximum {self.max_value}"
            )


class RequiredModel(BaseModel):
    """Model weight dependency required by a workflow contract."""

    node_class: str
    input_name: str
    filename: str
    sha256: str | None = None
    node_id: str | None = None

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SHA256_RE.fullmatch(value):
            raise ValueError(f"Invalid SHA-256 hex digest: {value!r}")
        return value.lower()


class ResolutionRule(BaseModel):
    """Resolution alignment, bounds, and aspect ratio constraints."""

    multiple_of: int = Field(default=32, ge=1)
    min_width: int = Field(default=64, ge=1)
    max_width: int = Field(default=8192, ge=1)
    min_height: int = Field(default=64, ge=1)
    max_height: int = Field(default=8192, ge=1)
    expected_aspect_ratio: float | None = Field(default=None, gt=0)
    aspect_ratio_tolerance: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> ResolutionRule:
        if self.min_width > self.max_width:
            raise ValueError(
                f"min_width ({self.min_width}) must not exceed max_width ({self.max_width})"
            )
        if self.min_height > self.max_height:
            raise ValueError(
                f"min_height ({self.min_height}) must not exceed max_height ({self.max_height})"
            )
        return self


class AOVRequirement(BaseModel):
    """Input AOV sequence requirement bound to an exposed file_path parameter."""

    name: str
    param_name: str
    required: bool = True
    required_channels: list[str] = Field(default_factory=list)
    expected_colorspace: str | None = None


class PostflightThresholds(BaseModel):
    """Automated quality thresholds enforced after generation."""

    min_edge_iou: float = Field(default=0.85, ge=0.0, le=1.0)
    max_temporal_delta: float = Field(default=0.20, ge=0.0)
    min_pixel_std: float = Field(default=1e-4, ge=0.0)
    allow_nan_or_inf: bool = False
    expected_channels: int = Field(default=3, ge=1)


class WorkflowContract(BaseModel):
    """Contract governing inputs, parameter bounds, models, and output checks."""

    name: str
    version: str
    description: str = ""
    workflow_file: str = "workflow_api.json"
    colorspace: str = "ACEScg"
    resolution: ResolutionRule = Field(default_factory=ResolutionRule)
    required_models: list[RequiredModel] = Field(default_factory=list)
    required_aovs: list[AOVRequirement] = Field(default_factory=list)
    exposed_params: dict[str, ExposedParam] = Field(default_factory=dict)
    seed_param: str | None = "seed"
    postflight: PostflightThresholds = Field(default_factory=PostflightThresholds)

    @model_validator(mode="after")
    def _validate_references(self) -> WorkflowContract:
        if self.seed_param is not None and self.exposed_params:
            seed_def = self.exposed_params.get(self.seed_param)
            if seed_def is None:
                raise ValueError(
                    f"seed_param '{self.seed_param}' is not declared in exposed_params"
                )
            if seed_def.param_type != ParamType.INT:
                raise ValueError(
                    f"seed_param '{self.seed_param}' must have param_type 'int', "
                    f"got '{seed_def.param_type}'"
                )
        for aov in self.required_aovs:
            if aov.param_name not in self.exposed_params:
                raise ValueError(
                    f"AOV '{aov.name}' references unknown exposed_param '{aov.param_name}'"
                )
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> WorkflowContract:
        """Load and validate a WorkflowContract from a YAML file."""
        contract_path = Path(path)
        data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Expected mapping in contract YAML '{contract_path}'")
        return cls.model_validate(data)


class FrameRange(BaseModel):
    """Inclusive frame range with step increment."""

    start: int
    end: int
    step: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _validate_order(self) -> FrameRange:
        if self.end < self.start:
            raise ValueError(f"FrameRange end ({self.end}) must be >= start ({self.start})")
        return self

    def frames(self) -> list[int]:
        """Return concrete ordered frame numbers in this range."""
        return list(range(self.start, self.end + 1, self.step))

    def format_range(self) -> str:
        """Return compact VFX frame range string such as '1001-1010' or '1001-1010x2'."""
        if self.start == self.end:
            return str(self.start)
        base = f"{self.start}-{self.end}"
        return f"{base}x{self.step}" if self.step > 1 else base


def expand_frame_pattern(pattern: str, frame: int) -> Path:
    """Expand ####, %0Nd, or {frame:0Nd} placeholders to a concrete frame path."""
    if "#" in pattern:
        expanded = _HASH_PAD_RE.sub(lambda m: f"{frame:0{len(m.group(0))}d}", pattern)
        return Path(expanded)
    if _PRINTF_FRAME_RE.search(pattern):
        expanded = _PRINTF_FRAME_RE.sub(
            lambda m: f"{frame:0{int(m.group(1))}d}" if m.group(1) else str(frame),
            pattern,
        )
        return Path(expanded)
    if _BRACE_FRAME_RE.search(pattern):
        expanded = _BRACE_FRAME_RE.sub(
            lambda m: f"{frame:0{int(m.group(1))}d}" if m.group(1) else str(frame),
            pattern,
        )
        return Path(expanded)
    return Path(pattern)


class JobRequest(BaseModel):
    """Submission request representing a shot/pass render job against a workflow contract."""

    job_id: str = Field(default_factory=lambda: f"cg_{uuid4().hex[:12]}")
    show: str
    sequence: str
    shot: str
    pass_name: str
    version: int = Field(default=1, ge=1)
    workflow_dir: Path
    frame_range: FrameRange
    base_seed: int = Field(default=42, ge=0)
    input_sequences: dict[str, str]
    params: dict[str, Any] = Field(default_factory=dict)
    output_dir: Path
    colorspace: str | None = None
    rez_packages: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def resolve_input_frame(self, aov_name: str, frame: int) -> Path:
        """Resolve an input AOV sequence pattern to a concrete frame file path."""
        if aov_name not in self.input_sequences:
            raise KeyError(f"AOV '{aov_name}' is not present in job.input_sequences")
        return expand_frame_pattern(self.input_sequences[aov_name], frame)
