"""ComfyGuard: Deterministic ComfyUI Pipeline Broker & Contract Gatekeeper."""

from comfyguard.broker.client import ComfyUIClient
from comfyguard.broker.mutator import GraphMutator, compute_graph_sha256, graph_diff
from comfyguard.broker.pool import RetryPolicy, WorkerConfig, WorkerPool
from comfyguard.contracts.manifest import LineageManifest, ValidationReport
from comfyguard.contracts.schema import FrameRange, JobRequest, WorkflowContract
from comfyguard.dcc.submitter import DCCContext, PipelineOrchestrator
from comfyguard.publish.publisher import Publisher
from comfyguard.validation.postflight import PostflightValidator
from comfyguard.validation.preflight import PreflightValidator

__version__ = "0.1.0"

__all__ = [
    "ComfyUIClient",
    "DCCContext",
    "FrameRange",
    "GraphMutator",
    "JobRequest",
    "LineageManifest",
    "PipelineOrchestrator",
    "PostflightValidator",
    "PreflightValidator",
    "Publisher",
    "RetryPolicy",
    "ValidationReport",
    "WorkerConfig",
    "WorkerPool",
    "WorkflowContract",
    "__version__",
    "compute_graph_sha256",
    "graph_diff",
]
