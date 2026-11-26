"""Typed workflow graph mutation, canonical hashing, and structural graph diffing."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from comfyguard.contracts.schema import JobRequest, WorkflowContract


class GraphMutationError(Exception):
    """Base error raised when mutating or validating a ComfyUI workflow graph."""


class ParamValidationError(GraphMutationError):
    """Raised when user-supplied parameters violate the workflow contract."""


class NodeReferenceError(GraphMutationError):
    """Raised when a contract references a missing node or input in the workflow graph."""


def compute_graph_sha256(graph: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 digest of a ComfyUI API graph."""
    canonical = json.dumps(
        graph,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class NodeInputChange(BaseModel):
    """Difference in a single node input between two workflow graph versions."""

    node_id: str
    class_type: str | None = None
    input_name: str
    old_value: Any = None
    new_value: Any = None


class NodeClassChange(BaseModel):
    """Difference in a node's class_type between two workflow graph versions."""

    node_id: str
    old_class_type: str | None = None
    new_class_type: str | None = None


class GraphDiff(BaseModel):
    """Structured structural and parameter diff between two ComfyUI API graphs."""

    v1_sha256: str
    v2_sha256: str
    added_nodes: dict[str, str] = Field(default_factory=dict)
    removed_nodes: dict[str, str] = Field(default_factory=dict)
    class_changes: list[NodeClassChange] = Field(default_factory=list)
    modified_inputs: list[NodeInputChange] = Field(default_factory=list)

    @property
    def is_identical(self) -> bool:
        """Return True if no nodes, classes, or inputs differ."""
        return (
            not self.added_nodes
            and not self.removed_nodes
            and not self.class_changes
            and not self.modified_inputs
        )

    def format_human(self) -> str:
        """Render a concise human-readable diff report for TDs and supervisors."""
        if self.is_identical:
            return f"Graphs identical (sha256={self.v1_sha256[:12]})"

        lines = [
            f"Graph Diff: {self.v1_sha256[:12]} -> {self.v2_sha256[:12]}",
        ]
        for node_id in sorted(self.added_nodes):
            lines.append(f"  + node {node_id}: {self.added_nodes[node_id]}")
        for node_id in sorted(self.removed_nodes):
            lines.append(f"  - node {node_id}: {self.removed_nodes[node_id]}")
        for cls_change in self.class_changes:
            lines.append(
                f"  ~ node {cls_change.node_id} class_type: "
                f"{cls_change.old_class_type!r} -> {cls_change.new_class_type!r}"
            )
        for inp in self.modified_inputs:
            cls_tag = f" ({inp.class_type})" if inp.class_type else ""
            lines.append(
                f"  ~ node {inp.node_id}{cls_tag}.{inp.input_name}: "
                f"{inp.old_value!r} -> {inp.new_value!r}"
            )
        return "\n".join(lines)


def _node_sort_key(node_id: str) -> tuple[int, int | str]:
    return (0, int(node_id)) if node_id.isdigit() else (1, node_id)


def graph_diff(v1: dict[str, Any], v2: dict[str, Any]) -> GraphDiff:
    """Compute a deterministic diff between two ComfyUI API graph dictionaries."""
    v1_ids = set(v1.keys())
    v2_ids = set(v2.keys())

    added_nodes: dict[str, str] = {}
    for node_id in sorted(v2_ids - v1_ids, key=_node_sort_key):
        node = v2[node_id]
        added_nodes[node_id] = str(node.get("class_type", "<unknown>"))

    removed_nodes: dict[str, str] = {}
    for node_id in sorted(v1_ids - v2_ids, key=_node_sort_key):
        node = v1[node_id]
        removed_nodes[node_id] = str(node.get("class_type", "<unknown>"))

    class_changes: list[NodeClassChange] = []
    modified_inputs: list[NodeInputChange] = []

    for node_id in sorted(v1_ids & v2_ids, key=_node_sort_key):
        node1 = v1[node_id]
        node2 = v2[node_id]
        cls1 = node1.get("class_type")
        cls2 = node2.get("class_type")
        if cls1 != cls2:
            class_changes.append(
                NodeClassChange(
                    node_id=node_id,
                    old_class_type=cls1,
                    new_class_type=cls2,
                )
            )

        inputs1 = node1.get("inputs", {}) if isinstance(node1.get("inputs"), dict) else {}
        inputs2 = node2.get("inputs", {}) if isinstance(node2.get("inputs"), dict) else {}
        all_input_keys = sorted(set(inputs1.keys()) | set(inputs2.keys()))
        for input_name in all_input_keys:
            old_val = inputs1.get(input_name)
            new_val = inputs2.get(input_name)
            if input_name not in inputs1 or input_name not in inputs2 or old_val != new_val:
                modified_inputs.append(
                    NodeInputChange(
                        node_id=node_id,
                        class_type=cls2 or cls1,
                        input_name=input_name,
                        old_value=old_val,
                        new_value=new_val,
                    )
                )

    return GraphDiff(
        v1_sha256=compute_graph_sha256(v1),
        v2_sha256=compute_graph_sha256(v2),
        added_nodes=added_nodes,
        removed_nodes=removed_nodes,
        class_changes=class_changes,
        modified_inputs=modified_inputs,
    )


class MutatedFrameGraph(BaseModel):
    """Concrete mutated ComfyUI graph ready for dispatch on a single frame."""

    frame: int
    seed: int
    graph: dict[str, Any]
    graph_sha256: str
    injected_params: dict[str, Any] = Field(default_factory=dict)
    input_files: dict[str, str] = Field(default_factory=dict)


class GraphMutator:
    """Contract-enforced parameter injector for ComfyUI API workflow graphs."""

    def __init__(self, contract: WorkflowContract, template_graph: dict[str, Any]) -> None:
        self.contract = contract
        self._template_graph = copy.deepcopy(template_graph)
        self._validate_contract_bindings()

    @classmethod
    def from_workflow_dir(cls, workflow_dir: str | Path) -> GraphMutator:
        """Load contract.yaml and its referenced workflow_api.json from a directory."""
        wf_dir = Path(workflow_dir)
        contract_path = wf_dir / "contract.yaml"
        if not contract_path.is_file():
            raise FileNotFoundError(f"Contract file not found: {contract_path}")
        contract = WorkflowContract.from_yaml(contract_path)

        workflow_path = wf_dir / contract.workflow_file
        if not workflow_path.is_file():
            raise FileNotFoundError(f"Workflow graph JSON not found: {workflow_path}")
        template_graph = json.loads(workflow_path.read_text(encoding="utf-8"))
        if not isinstance(template_graph, dict):
            raise GraphMutationError(
                f"Expected JSON object in workflow graph '{workflow_path}'"
            )
        return cls(contract=contract, template_graph=template_graph)

    @property
    def template_graph(self) -> dict[str, Any]:
        """Return a deep copy of the immutable template graph."""
        return copy.deepcopy(self._template_graph)

    @property
    def template_sha256(self) -> str:
        """Canonical SHA-256 of the unmutated template graph."""
        return compute_graph_sha256(self._template_graph)

    def _validate_contract_bindings(self) -> None:
        for param_name, param_spec in self.contract.exposed_params.items():
            node = self._template_graph.get(param_spec.node_id)
            if not isinstance(node, dict):
                raise NodeReferenceError(
                    f"Exposed parameter '{param_name}' references missing node "
                    f"'{param_spec.node_id}'"
                )
            inputs = node.get("inputs")
            if not isinstance(inputs, dict) or param_spec.input_name not in inputs:
                raise NodeReferenceError(
                    f"Exposed parameter '{param_name}' references missing input "
                    f"'{param_spec.input_name}' on node '{param_spec.node_id}'"
                )

        for model in self.contract.required_models:
            if model.node_id is None:
                continue
            node = self._template_graph.get(model.node_id)
            if not isinstance(node, dict):
                raise NodeReferenceError(
                    f"Required model '{model.filename}' references missing node '{model.node_id}'"
                )
            if node.get("class_type") != model.node_class:
                raise NodeReferenceError(
                    f"Required model node '{model.node_id}' has class_type "
                    f"{node.get('class_type')!r}, expected {model.node_class!r}"
                )
            inputs = node.get("inputs")
            if not isinstance(inputs, dict) or model.input_name not in inputs:
                raise NodeReferenceError(
                    f"Required model node '{model.node_id}' missing input '{model.input_name}'"
                )

    def validate_params(
        self,
        overrides: dict[str, Any] | None = None,
        *,
        include_per_frame: bool = False,
    ) -> dict[str, Any]:
        """Validate parameter overrides against contract types and bounds."""
        raw_overrides = dict(overrides or {})
        unknown = sorted(set(raw_overrides) - set(self.contract.exposed_params))
        if unknown:
            raise ParamValidationError(
                f"Unknown parameter(s) not declared in contract: {', '.join(unknown)}"
            )

        resolved: dict[str, Any] = {}
        for name, spec in self.contract.exposed_params.items():
            if spec.per_frame and not include_per_frame and name not in raw_overrides:
                continue

            if name in raw_overrides:
                candidate = raw_overrides[name]
            elif spec.default is not None:
                candidate = spec.default
            elif not spec.required:
                continue
            else:
                raise ParamValidationError(f"Missing required parameter '{name}'")

            try:
                resolved[name] = spec.validate_value(candidate, param_name=name)
            except ValueError as exc:
                raise ParamValidationError(str(exc)) from exc

        return resolved

    def mutate_for_frame(
        self,
        frame: int,
        base_seed: int,
        params: dict[str, Any] | None = None,
        input_files: dict[str, str | Path] | None = None,
        output_prefix: str | None = None,
    ) -> MutatedFrameGraph:
        """Produce a validated, deterministic single-frame ComfyUI graph."""
        merged_overrides = dict(params or {})
        frame_seed = base_seed + frame

        if self.contract.seed_param is not None:
            merged_overrides[self.contract.seed_param] = frame_seed

        aov_to_param = {aov.name: aov.param_name for aov in self.contract.required_aovs}
        resolved_inputs: dict[str, str] = {}
        for key, path_value in (input_files or {}).items():
            path_str = str(path_value)
            param_key = aov_to_param.get(key, key)
            merged_overrides[param_key] = path_str
            resolved_inputs[key] = path_str

        if output_prefix is not None:
            for name, spec in self.contract.exposed_params.items():
                if spec.is_output_prefix:
                    merged_overrides[name] = output_prefix

        validated = self.validate_params(merged_overrides, include_per_frame=True)

        mutated = copy.deepcopy(self._template_graph)
        for param_name, value in validated.items():
            spec = self.contract.exposed_params[param_name]
            mutated[spec.node_id]["inputs"][spec.input_name] = value

        return MutatedFrameGraph(
            frame=frame,
            seed=frame_seed,
            graph=mutated,
            graph_sha256=compute_graph_sha256(mutated),
            injected_params=validated,
            input_files=resolved_inputs,
        )

    def mutate_for_job(
        self,
        job: JobRequest,
        *,
        input_overrides: dict[int, dict[str, str]] | None = None,
    ) -> list[MutatedFrameGraph]:
        """Generate mutated graphs for every frame in a JobRequest."""
        frames: list[MutatedFrameGraph] = []
        for frame in job.frame_range.frames():
            frame_overrides = (input_overrides or {}).get(frame, {})
            frame_inputs = {
                aov_name: frame_overrides.get(
                    aov_name, str(job.resolve_input_frame(aov_name, frame))
                )
                for aov_name in job.input_sequences
            }
            output_prefix = (
                f"{job.show}_{job.sequence}_{job.shot}_{job.pass_name}"
                f"_v{job.version:03d}_{frame:04d}"
            )
            frames.append(
                self.mutate_for_frame(
                    frame=frame,
                    base_seed=job.base_seed,
                    params=job.params,
                    input_files=frame_inputs,
                    output_prefix=output_prefix,
                )
            )
        return frames
