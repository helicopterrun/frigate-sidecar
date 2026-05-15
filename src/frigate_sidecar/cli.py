"""Top-level CLI for frigate-sidecar.

Subcommand groups (`triage`, `analysis`) are wired in their respective modules
and added here. The `serve` command starts the FastAPI server.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="fsc",
    help="frigate-sidecar: triage UI + read-only analysis for Frigate NVR.",
    no_args_is_help=True,
)


@app.command()
def serve() -> None:
    """Run the HTTP server."""
    from frigate_sidecar.server import run

    run()


@app.command()
def version() -> None:
    """Print the installed version."""
    from frigate_sidecar import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
