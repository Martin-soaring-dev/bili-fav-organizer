from fastapi import FastAPI
from fastapi.testclient import TestClient

from bili_fav.api.registry import ROUTES, register_legacy_routes


def test_registry_has_unique_routes_and_feature_tags():
    assert len(ROUTES) == 92
    assert len({(method, path) for method, path, _, _ in ROUTES}) == len(ROUTES)
    assert all(feature for _, _, _, feature in ROUTES)


def test_registry_registers_real_handlers_in_original_order():
    app = FastAPI()
    calls = {}
    namespace = {}
    for _, _, name, _ in ROUTES:
        def handler(name=name):
            return {"handler": name}
        namespace[name] = handler
    register_legacy_routes(app, namespace)
    actual = [(next(iter(route.methods)), route.path, route.endpoint.__name__)
              for route in app.routes if hasattr(route, "methods")]
    assert [(method, path) for method, path, _ in actual] == [
        (method, path) for method, path, _, _ in ROUTES
    ]
    assert ClientRouteCount(app) == 92


def ClientRouteCount(app):
    return len([route for route in app.routes if getattr(route, "path", "").startswith("/") and
                getattr(route, "methods", None)])
