"""Post-flight validation checks for generated frames (Phase 3)."""

from __future__ import annotations


class PostflightValidator:
    """Validates output resolution, NaN/Inf buffers, edge drift IoU, and temporal delta."""

    def validate(self) -> None:
        raise NotImplementedError("Implemented in Phase 3")
