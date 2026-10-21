"""schema_builder internals: contracts pinned through its private seams.

These tests intentionally reach into ``_arguments`` with hand-built
RouteInfo/ParamInfo records — the fastest way to pin argument-collision
semantics without assembling a whole FastAPI app for one code path.
"""

import pytest

from fastapi_mcp_neo.scanner import ParamInfo, RouteInfo
from fastapi_mcp_neo.schema_builder import DuplicateArgError, _arguments
from fastapi_mcp_neo.type_builder import TypeBuilder


class TestPrivateArgumentsContract:
    def test_duplicate_arg_names(self):
        route = RouteInfo(
            route=None,  # type: ignore[arg-type]
            method="GET",
            path="/x",
            field_name="get_x",
            path_params=(ParamInfo("id", int, True),),
            query_params=(ParamInfo("id", str, False, "v"),),
        )
        with pytest.raises(DuplicateArgError, match="argument 'id'"):
            _arguments(route, TypeBuilder())
