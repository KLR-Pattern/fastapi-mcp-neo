"""naming: field names from endpoint function names."""

from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_mcp_neo.naming import field_name_for


class Out(BaseModel):
    id: int


def make_route(app: FastAPI, path: str, func_name: str):
    """Register a GET route whose endpoint carries ``func_name``."""

    def endpoint() -> Out:
        return Out(id=1)

    endpoint.__name__ = func_name
    app.get(path, response_model=Out)(endpoint)
    return next(r for r in app.routes if getattr(r, "path", None) == path)


class TestFieldNameFor:
    def test_function_name_used_verbatim(self):
        app = FastAPI()
        route = make_route(app, "/users/{user_id}", "get_user_by_id")
        assert field_name_for(route) == "get_user_by_id"

    def test_no_verb_prefix_or_param_suffix(self):
        app = FastAPI()
        route = make_route(app, "/items", "list")
        assert field_name_for(route) == "list"

    def test_private_style_names_allowed(self):
        app = FastAPI()
        route = make_route(app, "/x", "_internal")
        assert field_name_for(route) == "_internal"
