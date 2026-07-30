"""Top-level CLI for frigate-sidecar."""

from __future__ import annotations

import json

import typer

from frigate_sidecar import __version__
from frigate_sidecar.config import load_settings
from frigate_sidecar.tables import render_table

app = typer.Typer(
    name="fsc",
    help="frigate-sidecar: triage UI + read-only analysis for Frigate NVR.",
    no_args_is_help=True,
)

triage_app = typer.Typer(help="Sample borderline events and record tp/fp/skip labels.")
analysis_app = typer.Typer(help="Read-only analyses over Frigate's DB and live API.")
faces_app = typer.Typer(help="Score + curate Frigate's auto-saved face training crops.")
scrub_app = typer.Typer(help="Uniform-cadence scrub-cache generation (sprite sheets).")
app.add_typer(triage_app, name="triage")
app.add_typer(analysis_app, name="analysis")
app.add_typer(faces_app, name="faces")
app.add_typer(scrub_app, name="scrub")


@app.command()
def serve() -> None:
    """Run the HTTP server."""
    from frigate_sidecar.server import run

    run()


@app.command()
def watchdog() -> None:
    """Run the Frigate health watchdog (restarts the container on a hung backend)."""
    from frigate_sidecar.watchdog import run_watchdog

    raise typer.Exit(code=run_watchdog(load_settings()))


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


# ----- Triage subcommands -----


@triage_app.command("sample")
def triage_sample(
    days: int = typer.Option(14),
    n: int = typer.Option(30),
    camera: str | None = typer.Option(None),
    label: str | None = typer.Option(None),
    seed: int | None = typer.Option(None),
) -> None:
    """Sample borderline events as JSONL on stdout."""
    from frigate_sidecar.triage.sampler import sample

    s = load_settings()
    selected = sample(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        api_base_url=s.frigate.base_url,
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

    s = load_settings()
    try:
        result = record(
            frigate_db=s.frigate.db_path, sidecar_db=s.sidecar.db_path,
            event_id=event_id, label=label,  # type: ignore[arg-type]
            note=note, session=session, force=force,
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
                    "id": event_id, "ok": False, "before": exc.existing,
                    "error": f"already labeled '{exc.existing}'; use --force to overwrite",
                }
            ),
            err=True,
        )
        raise typer.Exit(code=4) from None
    typer.echo(json.dumps({**result, "ok": True}))


@triage_app.command("clear")
def triage_clear(event_id: str = typer.Option(..., "--event-id")) -> None:
    """Delete the triage label for one event."""
    from frigate_sidecar.triage.recorder import clear

    s = load_settings()
    result = clear(sidecar_db=s.sidecar.db_path, event_id=event_id)
    typer.echo(json.dumps({**result, "ok": True}))


@triage_app.command("stats")
def triage_stats() -> None:
    """Print counts of triage labels."""
    from frigate_sidecar.triage.recorder import stats

    s = load_settings()
    typer.echo(json.dumps(stats(sidecar_db=s.sidecar.db_path)))


# ----- Faces subcommands -----


@faces_app.command("scan")
def faces_scan() -> None:
    """Score new train/ crops and (if face.auto_promote) promote eligible ones."""
    from frigate_sidecar.faces import scorer

    s = load_settings()
    try:
        summary = scorer.scan(s)
    except scorer.FacesUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    typer.echo(json.dumps(summary))


@faces_app.command("stats")
def faces_stats(bins: int = typer.Option(10, min=2, max=50)) -> None:
    """Print the face-crop quality histogram + decision/recognition counts."""
    from frigate_sidecar.faces import scorer

    s = load_settings()
    result = scorer.histogram(s, bins=bins)
    typer.echo(json.dumps(result, indent=2))


# ----- Analysis subcommands -----


@analysis_app.command("score-histogram")
def analysis_score_histogram(
    days: int = typer.Option(14),
    camera: str | None = typer.Option(None),
    label: str | None = typer.Option(None),
    min_samples: int = typer.Option(30),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Score distribution + min_score / threshold suggestions per (camera,label)."""
    from frigate_sidecar.analysis import score_histogram

    s = load_settings()
    result = score_histogram.analyze(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        days=days, camera=camera, label=label, min_samples=min_samples,
    )
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    headers = [
        "camera", "label", "n", "n_tp", "n_fp",
        "median_score", "p10_top", "p25_top", "p50_top", "p75_top",
        "suggested_min_score", "suggested_threshold", "confidence",
    ]
    typer.echo(render_table(headers, result["rows"]))


@analysis_app.command("motion-rate")
def analysis_motion_rate(
    days: int = typer.Option(14),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Per-camera event rate + spikiness + suggestions."""
    from frigate_sidecar.analysis import motion_rate

    s = load_settings()
    rows = motion_rate.analyze(frigate_db=s.frigate.db_path, days=days)
    if output_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    headers = [
        "camera", "events_total", "events_per_hr_avg", "events_per_hr_p95",
        "peak_hour_count", "spikiness", "night_ratio", "suggestion",
    ]
    typer.echo(render_table(headers, rows))


@analysis_app.command("fps-budget")
def analysis_fps_budget(output_json: bool = typer.Option(False, "--json")) -> None:
    """Detector inference budget vs configured demand."""
    from frigate_sidecar.analysis import fps_budget

    s = load_settings()
    result = fps_budget.analyze(frigate_base_url=s.frigate.base_url)
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo("## Detectors")
    typer.echo(
        render_table(
            ["name", "inference_ms", "implied_fps_per_detector", "thermal_flag"],
            result["detectors"],
        )
    )
    typer.echo("\n## Per-camera demand")
    typer.echo(
        render_table(
            [
                "camera", "configured_detect_fps", "observed_detection_fps",
                "observed_skipped_fps", "gap_pct",
            ],
            result["cameras"],
        )
    )
    typer.echo(
        f"\n**Budget:** {result['total_budget_fps']} fps  "
        f"**Demand:** {result['total_demand_fps']} fps  "
        f"**Util:** {result['utilization_pct']}%  "
        f"**Headroom:** {result['headroom_fps']} fps"
    )
    for rec in result["recommendations"]:
        typer.echo(f"- {rec}")


@analysis_app.command("motion-active")
def analysis_motion_active(
    days: int = typer.Option(14),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Per-camera raw motion activity and yield."""
    from frigate_sidecar.analysis import motion_active

    s = load_settings()
    result = motion_active.analyze(frigate_base_url=s.frigate.base_url, days=days)
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    headers = [
        "camera", "class", "mu_per_hr", "events_per_hr", "yield_per_kmu",
        "obs_det_fps", "cfg_det_fps", "motion_threshold", "hours_with_data",
    ]
    typer.echo(render_table(headers, result["rows"]))


@analysis_app.command("motion-compare")
def analysis_motion_compare(
    baseline: str = typer.Option(..., "--baseline"),
    target: str = typer.Option(..., "--target"),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """A/B motion comparison across two date ranges."""
    from frigate_sidecar.analysis import motion_compare

    s = load_settings()
    result = motion_compare.analyze(
        frigate_base_url=s.frigate.base_url, baseline=baseline, target=target
    )
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(f"# baseline {result['baseline']} vs target {result['target']}\n")
    headers = [
        "camera", "class", "base_mu_per_hr", "tgt_mu_per_hr", "ratio",
        "base_yield_per_kmu", "tgt_yield_per_kmu", "motion_threshold", "suggestion",
    ]
    typer.echo(render_table(headers, result["rows"]))


@analysis_app.command("zone-hits")
def analysis_zone_hits(
    days: int = typer.Option(30),
    camera: str | None = typer.Option(None),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Per-camera zone hit-map + mask candidates."""
    from frigate_sidecar.analysis import zone_hits

    s = load_settings()
    result = zone_hits.analyze(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        days=days, camera=camera,
    )
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(f"## Zone hits (last {result['days']} days)\n")
    typer.echo(
        render_table(["camera", "zone", "label", "n", "fp_in_triage"], result["hits"])
    )
    typer.echo("\n## Possible mask candidates\n")
    if not result["mask_candidates"]:
        typer.echo("_none_")
    else:
        typer.echo(
            render_table(
                ["camera", "label", "cluster_size", "centroid_x", "centroid_y",
                 "sample_event_id", "reason"],
                result["mask_candidates"],
            )
        )


@analysis_app.command("pull-events")
def analysis_pull_events(
    days: int = typer.Option(14),
    camera: str | None = typer.Option(None),
    label: str | None = typer.Option(None),
) -> None:
    """Dump events as JSONL on stdout."""
    from frigate_sidecar.analysis import pull_events

    s = load_settings()
    n = 0
    for ev in pull_events.pull(
        frigate_db=s.frigate.db_path, days=days, camera=camera, label=label
    ):
        typer.echo(json.dumps(ev))
        n += 1
    typer.echo(f"# wrote {n} events", err=True)


@analysis_app.command("annotation-offset")
def analysis_annotation_offset(
    days: int = typer.Option(7),
    camera: str | None = typer.Option(None),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Measured detect.annotation_offset (ms) per camera via template matching.

    Requires the `[annotation]` extra: `pip install "frigate-sidecar[annotation]"`.
    """
    from frigate_sidecar.analysis import annotation_offset

    s = load_settings()
    try:
        result = annotation_offset.analyze(
            frigate_db=s.frigate.db_path,
            frigate_base_url=s.frigate.base_url,
            days=days,
            camera=camera,
        )
    except annotation_offset.AnnotationOffsetUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    headers = [
        "camera", "n_contributing_events", "n_qualifying_events",
        "p25_ms", "median_offset_ms", "p75_ms", "iqr_ms",
        "suggested_offset_ms", "confidence",
    ]
    typer.echo(render_table(headers, result))


# ----- Scrub subcommands (docs/scrub-cache-and-proxy-spec.md §5.7) -----


@scrub_app.command("generate")
def scrub_generate(
    camera: str | None = typer.Option(None, "--camera"),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run one generation cycle now (forward edge). Mirrors what the in-process
    ~60s loop does, for a systemd-timer deployment (§5.4 option (b)) or manual
    backfill kickstart."""
    import asyncio

    from frigate_sidecar.scrub.generator import generate_cycle

    s = load_settings()
    if camera:
        s = s.model_copy(update={"scrub": s.scrub.model_copy(update={"cameras": [camera]})})
    results = asyncio.run(generate_cycle(s))
    if output_json:
        typer.echo(json.dumps(results))
        return
    for r in results:
        typer.echo(json.dumps(r))


@scrub_app.command("backfill")
def scrub_backfill(
    camera: str = typer.Option(..., "--camera"),
    days: int = typer.Option(4, help="Capped at scrub.retention_days regardless of this value."),
) -> None:
    """One-time history fill for `camera`, repeatedly cycling until the
    generator catches up to `now` (§5.7). Cadence is still the same
    interval-verified generator -- this just loops it instead of waiting on
    the 60s timer."""
    import asyncio
    import time as _time

    from frigate_sidecar.scrub.generator import generate_cycle

    s = load_settings()
    days = min(days, s.scrub.retention_days)
    s = s.model_copy(
        update={
            "scrub": s.scrub.model_copy(
                update={"cameras": [camera], "retention_days": s.scrub.retention_days}
            )
        }
    )
    start = _time.time()
    cutoff = start - days * 86400
    total_frames = 0
    while True:
        results = asyncio.run(generate_cycle(s))
        new_frames = sum(r.get("new_frames", 0) for r in results)
        total_frames += new_frames
        if new_frames == 0:
            break
    typer.echo(json.dumps({"camera": camera, "since": cutoff, "new_frames": total_frames}))


@scrub_app.command("prune")
def scrub_prune(camera: str | None = typer.Option(None, "--camera")) -> None:
    """Drop sheets/buckets past scrub.retention_days, oldest-first."""
    from frigate_sidecar.scrub.generator import prune

    s = load_settings()
    typer.echo(json.dumps(prune(s, camera=camera)))


@scrub_app.command("coverage")
def scrub_coverage_cmd(camera: str = typer.Option(..., "--camera")) -> None:
    """Print what's generated for `camera` (debug)."""
    import time as _time

    from frigate_sidecar import db

    s = load_settings()
    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        buckets = db.list_scrub_buckets(conn, camera, 0, _time.time())
        generated_through = db.latest_generated_through(conn, camera)
    finally:
        conn.close()
    typer.echo(
        json.dumps(
            {"camera": camera, "buckets": buckets, "generated_through": generated_through},
            default=str,
        )
    )


if __name__ == "__main__":
    app()
