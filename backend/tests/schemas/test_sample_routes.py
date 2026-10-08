"""Every sample a provider writes says which feed it came from.

The route is what keeps two feeds of one provider apart once they share a data source
(a Garmin watch's workout trace and its all-day monitoring), and what ranks them on a
collision. A writer that forgets it stores rows nobody can attribute and that rank
lowest, so the first time a new provider path forgot it would be the first time its data
lost a second to another feed. This test reads the source rather than a provider's
output, so it covers every path, including ones no other test exercises.
"""

import ast
from pathlib import Path

from app.schemas.enums.sample_route import (
    ROUTE_KIND_RANK,
    SAMPLE_ROUTE_DEFINITIONS,
    RouteKind,
    SampleRoute,
    route_rank,
)

APP = Path(__file__).resolve().parents[2] / "app"
SAMPLE_CLASSES = {"TimeSeriesSampleCreate", "HeartRateSampleCreate", "StepSampleCreate"}
# Fixture generators for a dev database; their rows are not anyone's data.
NOT_A_WRITER = {APP / "services" / "seed_data", APP / "schemas"}


def _sample_constructions() -> list[tuple[Path, ast.Call]]:
    found = []
    for path in APP.rglob("*.py"):
        if any(path.is_relative_to(skip) for skip in NOT_A_WRITER):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name in SAMPLE_CLASSES:
                found.append((path, node))
    return found


def test_every_sample_construction_states_its_route() -> None:
    constructions = _sample_constructions()
    assert len(constructions) > 50, "the scan found too few writers to be reading the app"
    missing = [
        f"{path.relative_to(APP.parent)}:{call.lineno}"
        for path, call in constructions
        # A ``**sample.model_dump()`` re-wrap carries the route of the sample it copies.
        if not any(kw.arg == "route" or kw.arg is None for kw in call.keywords)
    ]
    assert not missing, f"sample writers with no route: {missing}"


def test_every_route_has_one_stable_id_and_a_kind() -> None:
    ids = [route_id for route_id, _, _ in SAMPLE_ROUTE_DEFINITIONS]
    routes = [route for _, route, _ in SAMPLE_ROUTE_DEFINITIONS]
    assert len(ids) == len(set(ids))
    assert sorted(routes) == sorted(SampleRoute)
    assert all(route_id > 0 for route_id in ids)


def test_kinds_rank_from_a_workout_trace_down_to_a_summary() -> None:
    order = [RouteKind.WORKOUT, RouteKind.INTRADAY, RouteKind.WINDOW, RouteKind.SUMMARY]
    ranks = [ROUTE_KIND_RANK[kind] for kind in order]
    assert ranks == sorted(ranks, reverse=True)
    assert route_rank(None) < min(ranks)
    assert route_rank(32_000) == route_rank(None)
