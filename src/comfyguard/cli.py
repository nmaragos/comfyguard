"""Typer CLI entrypoint for ComfyGuard."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="comfyguard",
    help="Deterministic ComfyUI Pipeline Broker & Contract Gatekeeper.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """ComfyGuard CLI command group."""


@app.command()
def version() -> None:
    """Print the installed ComfyGuard version."""
    from comfyguard import __version__

    typer.echo(f"comfyguard {__version__}")


if __name__ == "__main__":
    app()
