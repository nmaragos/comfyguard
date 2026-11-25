"""Validation reports and provenance lineage manifest models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class CheckStatus(StrEnum):
    """Outcome status for an individual pre-flight or post-flight check."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class CheckResult(BaseModel):
    """Result record for a single pipeline validation check."""

    check_name: str
    status: CheckStatus
    message: str
    frame: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    """Aggregated validation report across pre-flight or post-flight gates."""

    stage: Literal["preflight", "postflight"]
    passed: bool = True
    checks: list[CheckResult] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def add_check(self, check: CheckResult) -> None:
        """Append a check result and keep overall pass status synchronized."""
        self.checks.append(check)
        if check.status == CheckStatus.FAIL:
            self.passed = False

    @property
    def failed_checks(self) -> list[CheckResult]:
        """Return all checks that failed."""
        return [c for c in self.checks if c.status == CheckStatus.FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        """Return all checks that emitted warnings."""
        return [c for c in self.checks if c.status == CheckStatus.WARN]

    def summary(self) -> str:
        """Return a concise one-line summary of the validation report."""
        passed_count = sum(1 for c in self.checks if c.status == CheckStatus.PASS)
        fail_count = len(self.failed_checks)
        warn_count = len(self.warnings)
        state = "PASSED" if self.passed else "FAILED"
        return (
            f"[{self.stage.upper()}] {state} "
            f"(pass={passed_count}, fail={fail_count}, warn={warn_count})"
        )


class FrameLineage(BaseModel):
    """Per-frame execution provenance and output/quarantine record."""

    frame: int
    seed: int
    graph_sha256: str
    input_files: dict[str, str] = Field(default_factory=dict)
    output_file: str | None = None
    quarantined: bool = False
    quarantine_reason: str | None = None
    prompt_id: str | None = None


class LineageManifest(BaseModel):
    """Immutable sidecar provenance manifest written alongside published passes."""

    job_id: str
    show: str
    sequence: str
    shot: str
    pass_name: str
    version: int
    contract_name: str
    contract_version: str
    workflow_sha256: str
    base_seed: int
    colorspace: str
    rez_packages: list[str] = Field(default_factory=list)
    dcc_source: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    preflight_report: ValidationReport | None = None
    postflight_report: ValidationReport | None = None
    frames: list[FrameLineage] = Field(default_factory=list)
    graph_snapshot: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_json_file(self, path: str | Path) -> None:
        """Serialize manifest deterministically to a JSON file."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def from_json_file(cls, path: str | Path) -> LineageManifest:
        """Load and validate a LineageManifest from a JSON file."""
        source = Path(path)
        return cls.model_validate_json(source.read_text(encoding="utf-8"))
