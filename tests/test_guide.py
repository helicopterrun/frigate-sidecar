"""User guide: loader unit tests, endpoints, and the docs-coverage contract.

The coverage tests are the maintenance mechanism the guide was built around:
adding an HTML page or a config section without documenting it in some
guide_content/*.md frontmatter fails here.
"""

from __future__ import annotations

from html import escape as html_escape
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from frigate_sidecar.config import FrigateSection, Settings, SidecarSection
from frigate_sidecar.guide import (
    SECTION_TITLES,
    GuideError,
    GuideRegistry,
    load_guide,
)
from frigate_sidecar.routes.guide import STAT_KEYS
from frigate_sidecar.server import create_app

# Pages that legitimately need no guide topic: legacy redirects into
# /settings, the guide's own pages, and alias routes.
UNDOCUMENTED_OK = {
    "/guide/{slug}",
    "/zones",
    "/devices",
    "/cameras",
    "/cameras2",
}


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


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def registry(app: FastAPI) -> GuideRegistry:
    return app.state.guide  # type: ignore[no-any-return]


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


def _html_page_paths(app: FastAPI) -> set[str]:
    out = set()
    for route in _api_routes(app):
        if "GET" not in route.methods:
            continue
        rc = getattr(route.response_class, "value", route.response_class)
        if isinstance(rc, type) and issubclass(rc, HTMLResponse):
            out.add(route.path)
    return out


# --- Coverage contract ------------------------------------------------------


def test_every_html_page_is_documented(app: FastAPI, registry: GuideRegistry) -> None:
    documented = {r for t in registry.topics.values() for r in t.meta.routes}
    missing = _html_page_paths(app) - documented - UNDOCUMENTED_OK
    assert not missing, (
        f"HTML pages without a guide topic: {sorted(missing)} — add each to some "
        "guide_content/*.md `routes:` frontmatter (or UNDOCUMENTED_OK with a reason)"
    )


def test_every_config_section_is_documented(registry: GuideRegistry) -> None:
    documented = {c for t in registry.topics.values() for c in t.meta.config}
    missing = set(Settings.model_fields) - documented
    assert not missing, (
        f"config sections without a guide topic: {sorted(missing)} — add each to "
        "some guide_content/*.md `config:` frontmatter"
    )


def test_documented_routes_and_stats_exist(app: FastAPI, registry: GuideRegistry) -> None:
    all_paths = {r.path for r in _api_routes(app)}
    for topic in registry.topics.values():
        for path in topic.meta.routes:
            assert path in all_paths, f"{topic.slug}: unknown route {path!r}"
        unknown_stats = topic.stats_used - STAT_KEYS
        assert not unknown_stats, f"{topic.slug}: unknown stats {sorted(unknown_stats)}"


def test_internal_links_resolve(app: FastAPI, registry: GuideRegistry) -> None:
    """Every root-relative markdown link must hit a real route."""
    routes = _api_routes(app)
    exact = {r.path for r in routes}
    for topic in registry.topics.values():
        for link in topic.internal_links:
            if link in exact:
                continue
            if link.startswith("/guide/"):
                assert link.removeprefix("/guide/") in registry.topics, (
                    f"{topic.slug}: broken guide link {link!r}"
                )
                continue
            assert any(r.path_regex.match(link) for r in routes), (
                f"{topic.slug}: link {link!r} matches no route"
            )


def test_sections_valid_and_orders_unique(registry: GuideRegistry) -> None:
    seen: set[tuple[str, int]] = set()
    for topic in registry.topics.values():
        assert topic.meta.section in SECTION_TITLES
        key = (topic.meta.section, topic.meta.order)
        assert key not in seen, f"duplicate order {key} in section"
        seen.add(key)


# --- Endpoints --------------------------------------------------------------


def test_index_lists_every_topic(client: TestClient, registry: GuideRegistry) -> None:
    resp = client.get("/guide")
    assert resp.status_code == 200
    for topic in registry.topics.values():
        assert f"/guide/{topic.slug}" in resp.text


def test_every_topic_page_renders(client: TestClient, registry: GuideRegistry) -> None:
    for topic in registry.topics.values():
        resp = client.get(f"/guide/{topic.slug}")
        assert resp.status_code == 200
        assert html_escape(topic.meta.title) in resp.text


def test_unknown_topic_404s(client: TestClient) -> None:
    assert client.get("/guide/no-such-topic").status_code == 404


def test_stats_json_covers_all_keys(client: TestClient) -> None:
    resp = client.get("/guide/stats.json")
    assert resp.status_code == 200
    stats = resp.json()["stats"]
    assert set(stats) == set(STAT_KEYS)
    assert all(isinstance(v, str) for v in stats.values())
    # Seeded sidecar DB is empty but present: counts are real zeros, not "?".
    assert stats["clusters_total"] == "0"


def test_search_json_indexes_every_topic(
    client: TestClient, registry: GuideRegistry
) -> None:
    resp = client.get("/guide/search.json")
    assert resp.status_code == 200
    entries = {t["slug"]: t for t in resp.json()["topics"]}
    assert set(entries) == set(registry.topics)
    for entry in entries.values():
        assert entry["text"], f"{entry['slug']}: empty search text"
        assert entry["number"].count(".") == 1


def test_topic_page_has_anchors_and_onpage_toc(
    client: TestClient, registry: GuideRegistry
) -> None:
    topic = next(t for t in registry.topics.values() if len(t.headings) >= 2)
    resp = client.get(f"/guide/{topic.slug}")
    assert "On this page" in resp.text
    for anchor, _text in topic.headings:
        assert f'id="{anchor}"' in resp.text
        assert f'href="#{anchor}"' in resp.text


def test_sidebar_marks_current_topic(client: TestClient, registry: GuideRegistry) -> None:
    slug = next(iter(registry.topics))
    resp = client.get(f"/guide/{slug}")
    assert 'class="current"' in resp.text
    numbers = registry.numbers()
    assert numbers[slug] in resp.text


def test_walkthrough_renders_checklist(client: TestClient, registry: GuideRegistry) -> None:
    walk_topics = [t for t in registry.topics.values() if t.walkthrough_steps]
    assert walk_topics, "expected at least one walkthrough in the guide"
    resp = client.get(f"/guide/{walk_topics[0].slug}")
    assert 'class="walkthrough"' in resp.text
    assert 'data-step="0"' in resp.text


# --- Loader unit tests ------------------------------------------------------


def _write_topic(tmp_path: Path, name: str, text: str) -> Path:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_loader_rejects_missing_frontmatter(tmp_path: Path) -> None:
    _write_topic(tmp_path, "bad.md", "no frontmatter here\n")
    with pytest.raises(GuideError, match="frontmatter"):
        load_guide(tmp_path)


def test_loader_rejects_unknown_section(tmp_path: Path) -> None:
    _write_topic(
        tmp_path, "bad.md", "---\ntitle: X\nsection: nonsense\norder: 1\n---\nbody\n"
    )
    with pytest.raises(GuideError, match="unknown section"):
        load_guide(tmp_path)


def test_loader_rejects_empty_walkthrough(tmp_path: Path) -> None:
    _write_topic(
        tmp_path,
        "bad.md",
        "---\ntitle: X\nsection: sidecar\norder: 1\n---\n```walkthrough\nnothing\n```\n",
    )
    with pytest.raises(GuideError, match="walkthrough"):
        load_guide(tmp_path)


def test_loader_renders_stats_and_walkthroughs(tmp_path: Path) -> None:
    _write_topic(
        tmp_path,
        "ok.md",
        "---\ntitle: X\nsection: sidecar\norder: 1\n---\n"
        "Count: {{stat:clusters_total}}\n\n"
        "```walkthrough\n- step one\n- step <two>\n```\n"
        "[link](/triage)\n",
    )
    topic = load_guide(tmp_path).topics["ok"]
    assert topic.stats_used == {"clusters_total"}
    assert 'data-stat="clusters_total"' in topic.html
    assert topic.walkthrough_steps == 1
    assert "step &lt;two&gt;" in topic.html
    assert topic.internal_links == {"/triage"}


def test_loader_extracts_headings_and_search_text(tmp_path: Path) -> None:
    _write_topic(
        tmp_path,
        "ok.md",
        "---\ntitle: X\nsection: sidecar\norder: 1\n---\n"
        "intro words\n\n## Using it\n\nbody\n\n## Using it\n\nagain\n",
    )
    topic = load_guide(tmp_path).topics["ok"]
    assert topic.headings == (("using-it", "Using it"), ("using-it-1", "Using it"))
    assert 'id="using-it"' in topic.html and 'id="using-it-1"' in topic.html
    assert "intro words" in topic.search_text and "<" not in topic.search_text


def test_neighbors_follow_section_order(tmp_path: Path) -> None:
    for i, name in enumerate(["a.md", "b.md"]):
        _write_topic(
            tmp_path, name, f"---\ntitle: T{i}\nsection: sidecar\norder: {i}\n---\nbody\n"
        )
    reg = load_guide(tmp_path)
    prev_t, next_t = reg.neighbors("a")
    assert prev_t is None and next_t is not None and next_t.slug == "b"
    assert reg.neighbors("missing") == (None, None)
