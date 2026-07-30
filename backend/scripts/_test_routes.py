"""Walk a FastAPI app's routes, descending into included routers.

`include_router` keeps each router as one lazy entry with an empty path
that exposes the router it wrapped as `original_router`, so a flat scan
of `app.routes` silently misses every route a router owns. Any test
asserting over the whole HTTP surface must walk instead.
"""
from __future__ import annotations


def walk_routes(routes):
    """Yield every concrete route reachable from `routes`."""
    for route in routes:
        path = getattr(route, "path", None)
        nested = getattr(route, "original_router", None) or (
            getattr(route, "routes", None) if not path else None
        )
        if not path and nested is not None:
            yield from walk_routes(getattr(nested, "routes", nested))
            continue
        if not path:
            continue
        yield route
