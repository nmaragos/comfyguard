"""Versioned output publisher, EXR attribute stamper, and quarantine router (Phase 3)."""

from __future__ import annotations


class Publisher:
    """Publishes approved passes with lineage manifests and quarantines failures."""

    def publish(self) -> None:
        raise NotImplementedError("Implemented in Phase 3")
