"""Top-level CLI for frigate-sidecar.

Subcommand groups:
- `fsc serve`                  : run the HTTP server
- `fsc triage sample|record|clear|stats`
- `fsc version`
"""

from __future__ import annotations

import json
import sys

import typer

from frigate_sidecar import __version__
from frigate_sidecar.config import load_settings

app = typer.Typer(
    name="fsc",
    help="frigate-sidecar: triage UI + read-only analysis for Frigate NVR.",
    no_args_is_help=True,
)

triage_app = typer.Typer(help="Sample borderline events and record tp/fp/skip labels.")
app.add_typer(triage_app, name="triage")


@app.command()
def serve() -> None:
    """Run the HTTP server."""
    from frigate_sidecar.server import run

    run()


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@triage_app.command("sample")
def triage_sample(
    days: int = typer.Option(14, help="Look-back window in days."),
    n: int = typer.Option(30, help="Target number of events to sample."),
    camera: str | None = typer.Option(None, help="Restrict to a single camera."),
    label: str | None = typer.Option(None, help="Restrict to a single Frigate label."),
    seed: int | None = typer.Option(None, help="Seed RNG for deterministic output."),
) -> None:
    """Sample borderline events as JSONL on stdout."""
    from frigate_sidecar.triage.sampler import sample

    settings = load_settings()
    selected = sample(
        frigate_db=settings.frigate.db_path,
        sidecar_db=settings.sidecar.db_path,
        api_base_url=settings.frigate.base_url,
        days=days,
        n=n,
        camera=camera,
        label=label,
        seed=seed,
    )
    for ev in selected:
        typer.echo(json.dumps(ev))
    typer.echo(f"# selected {len(selected)} of {n} target", err=True)


@triage_app.command("record")
def triage_record(
    event_id: str = typer.Option(..., "--event-id"),
    label: str = typer.Option(..., "--label", help="fp | tp | skip"),
    note: str | None = typer.Option(None),
    session: str | None = typer.Option(None),
    force: bool = typer.Option(False, "--force/--no-force"),
) -> None:
    """Record a triage label for one event."""
    from frigate_sidecar.triage.recorder import (
        AlreadyLabeledError,
        EventNotFoundError,
        record,
    )

    settings = load_settings()
    try:
        result = record(
            frigate_db=settings.frigate.db_path,
            sidecar_db=settings.sidecar.db_path,
            event_id=event_id,
            label=label,  # type: ignore[arg-type]
            note=note,
            session=session,
            force=force,
        )
    except EventNotFoundError:
        typer.echo(
            json.dumps({"id": event_id, "ok": False, "error": "event not found in frigate.db"}),
            err=True,
        )
        raise typer.Exit(code=3) from None
    except AlreadyLabeledError as exc:
        typer.echo(
            json.dumps(
                {
                    "id": event_id,
                    "ok": False,
                    "before": exc.existing,
                    "error": f"already labeled '{exc.existing}'; use --force to overwrite",
                }
            ),
            err=True,
        )
        raise typer.Exit(code=4) from None
    typer.echo(json.dumps({**result, "ok": True}))


@triage_app.command("clear")
def triage_clear(
    event_id: str = typer.Option(..., "--event-id"),
) -> None:
    """Delete the triage label for one event."""
    from frigate_sidecar.triage.recorder import clear

    settings = load_settings()
    result = clear(sidecar_db=settings.sidecar.db_path, event_id=event_id)
    typer.echo(json.dumps({**result, "ok": True}))


@triage_app.command("stats")
def triage_stats() -> None:
    """Print counts of triage labels."""
    from frigate_sidecar.triage.recorder import stats

    settings = load_settings()
    typer.echo(json.dumps(stats(sidecar_db=settings.sidecar.db_path)))


if __name__ == "__main__":
    app()
