"""Link integrity: every href in a template must resolve to a real route."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from frigate_sidecar.config import FrigateSection, Settings, SidecarSection
from frigate_sidecar.server import create_app

TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "frigate_sidecar" / "templates"
)

_HREF_RE = re.compile(r'<a\b[^>]*\bhref="([^"]*)"', re.IGNORECASE)
_ID_RE = re.compile(r'\bid="([^"]*)"')


@pytest.fixture
def app(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> FastAPI:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return create_app(settings)


def _api_routes(app: FastAPI) -> list[APIRoute]:
    """All APIRoutes, flattening included-router wrappers."""
    out: list[APIRoute] = []
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        if isinstance(route, APIRoute):
            out.append(route)
            continue
        # FastAPI may wrap included routers (_IncludedRouter) instead of
        # flattening them into app.routes; reach through to the real router.
        inner = getattr(route, "original_router", None)
        stack.extend(getattr(inner, "routes", []) or getattr(route, "routes", []))
    return out


def test_template_links_resolve(app: FastAPI) -> None:
    routes = _api_routes(app)
    exact_paths = {r.path for r in routes}
    errors: list[str] = []

    tpl_paths = sorted(TEMPLATES_DIR.glob("*.html"))
    tpl_texts = {tpl: tpl.read_text() for tpl in tpl_paths}
    # base.html is a Jinja layout every page `{% extends %}`; a fragment
    # link defined there (e.g. the skip-to-content link) legitimately
    # resolves against an id supplied by whichever child template fills
    # `{% block body %}`/`{% block page_heading %}` at render time, so its
    # ids can't be found via a single-file regex scan. Widen its id pool to
    # every id across all templates rather than special-casing one anchor.
    all_ids = {i for text in tpl_texts.values() for i in _ID_RE.findall(text)}

    for tpl, text in tpl_texts.items():
        ids_in_file = all_ids if tpl.name == "base.html" else set(_ID_RE.findall(text))

        for href in _HREF_RE.findall(text):
            if "{{" in href:
                continue
            if href.startswith("/static") or href.startswith("http"):
                continue

            if href.startswith("#"):
                anchor = href[1:]
                if anchor and anchor not in ids_in_file:
                    errors.append(f"{tpl.name}: fragment {href!r} has no matching id")
                continue

            path = href.split("?")[0].split("#")[0]
            if not path.startswith("/"):
                continue
            if path in exact_paths:
                continue
            if any(r.path_regex.match(path) for r in routes):
                continue
            errors.append(f"{tpl.name}: href {path!r} matches no route")

    assert not errors, "Dead links in templates:\n" + "\n".join(errors)
