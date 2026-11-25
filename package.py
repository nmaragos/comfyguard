name = "comfyguard"

version = "0.1.0"

description = "Deterministic ComfyUI Pipeline Broker & Contract Gatekeeper"

authors = ["Nikolaos Maragkos"]

requires = ["python-3.11+"]

tools = ["comfyguard"]

build_command = "python -m pip install --no-deps --target={install_path}/python {root}"


def commands():
    env.PYTHONPATH.append("{root}/python")
    env.PYTHONPATH.append("{root}/src")
    env.PATH.append("{root}/bin")
    env.COMFYGUARD_WORKFLOW_ROOT.set("{root}/workflows")
