# Project Specification: ComfyGuard (Deterministic ComfyUI Pipeline Broker)

## 1. Objective & Engineering Philosophy
Build **ComfyGuard**, a headless Python/Rez-packaged pipeline service that treats ComfyUI as an untrusted, non-deterministic render engine. It enforces strict pre-flight and post-flight contracts, deterministic graph mutation, EXR metadata provenance, and automatic quarantine before AI-generated passes enter VFX shot directories.

## 2. Environment & Tooling Constraints
- **Python Environment:** Python 3.11+, managed via `uv` (`pyproject.toml`). Keep global system PATH clean.
- **Packaging:** Include a valid Rez `package.py` exposing the `comfyguard` CLI and library.
- **Core Libraries:** `pydantic>=2.0`, `aiohttp`, `websockets`, `typer`, `pyyaml`, `numpy`, `OpenImageIO` (with graceful fallback/adapter if OIIO isn't installed in dev environments).
- **Testing & Linting:** `pytest`, `pytest-asyncio`, `ruff`.

## 3. Repository Layout
```text
comfyguard/
├── package.py                      # Rez package definition
├── pyproject.toml                  # uv / pytest / ruff config
├── SPEC.md                         # This architecture & execution blueprint
├── src/
│   └── comfyguard/
│       ├── __init__.py
│       ├── contracts/
│       │   ├── schema.py           # WorkflowContract, JobRequest, ExposedParam
│       │   └── manifest.py         # LineageManifest & ValidationReport
│       ├── broker/
│       │   ├── client.py           # Async /prompt, /history, /object_info, WS monitor
│       │   ├── pool.py             # Multi-worker queue, OOM detection, retries
│       │   └── mutator.py          # Typed param injection, per-frame seeds, graph_diff
│       ├── validation/
│       │   ├── preflight.py        # Resolution alignment, frame continuity, model checks
│       │   └── postflight.py       # Buffer/NaN checks, edge drift IoU, temporal delta
│       ├── publish/
│       │   └── publisher.py        # Versioned naming, EXR header stamping, quarantine
│       ├── dcc/
│       │   └── submitter.py        # Lightweight Nuke/Houdini/Deadline submission wrapper
│       └── cli.py                  # Typer CLI: validate, submit, worker, diff, inspect
├── workflows/
│   └── lookdev_relight_v001/
│       ├── workflow_api.json       # Sample ComfyUI API workflow JSON
│       └── contract.yaml           # Parameter bounds, required models, alignment rules
├── tests/
│   ├── fixtures/
│   ├── test_mutator.py
│   ├── test_preflight.py
│   └── test_postflight.py
└── demo/
    └── run_portfolio_demo.py       # End-to-end 3-scenario portfolio showcase
```

## 4. Phased Implementation Plan & Verification Gates

### Phase 1: Contracts, Graph Mutator & Pre-Flight Gate
- Implement `WorkflowContract`, `JobRequest`, `LineageManifest` in `contracts/`.
- Implement `mutator.py`: load `workflow_api.json` + `contract.yaml`, validate parameter types/min/max bounds, inject per-frame file paths and deterministic seeds (`base_seed + frame`), compute canonical `graph_sha256`, and implement `graph_diff(v1, v2)`.
- Implement `preflight.py`: verify input sequence continuity, resolution divisibility (e.g., 32px), and required model hashes/availability against `/object_info`.
- **Verification Gate 1:** `pytest tests/test_mutator.py tests/test_preflight.py` passes offline with mock `/object_info` payloads.

### Phase 2: Async ComfyUI Client & Worker Pool
- Implement `client.py`: open `ws://<host>:<port>/ws?clientId=<uuid>`, `POST /prompt`, track `executing`/`progress`/`execution_error`/`executed` events, fetch `/history/<prompt_id>`, and support prompt cancellation (`/interrupt`).
- Implement `pool.py`: manage 1..N ComfyUI workers, handle CUDA OOM errors with configurable retry/backoff, and enforce job timeouts.
- **Verification Gate 2:** Async integration tests against a mock WebSocket/HTTP ComfyUI server verifying normal completion, mid-run cancellation, and OOM capture.

### Phase 3: Post-Flight Gate & Quarantine Publisher
- Implement `postflight.py`: validate output resolution/channels, detect NaN/Inf/flat frames, compute structural edge drift against input depth/matte passes, and measure inter-frame temporal delta.
- Implement `publisher.py`: write approved outputs with embedded EXR attributes (`comfyguard:graph_hash`, `comfyguard:seed`, `comfyguard:colorspace`) + `LineageManifest`; route failed outputs to `_quarantine/<job_id>/`.
- **Verification Gate 3:** `pytest tests/test_postflight.py` confirms corrupt/drifted frames are quarantined and valid frames publish with manifests.

### Phase 4: CLI, Rez Package & Portfolio Demo
- Build `typer` CLI (`validate`, `submit`, `diff`, `inspect`), `dcc/submitter.py`, and `package.py`.
- Build `demo/run_portfolio_demo.py` demonstrating: (1) pre-flight rejection of unaligned `1920x1080` input, (2) post-flight quarantine of high edge-drift frame, and (3) clean publish of `1920x1088` pass with `graph_diff`.
- **Verification Gate 4:** Full test suite passes and `run_portfolio_demo.py` runs end-to-end cleanly.

