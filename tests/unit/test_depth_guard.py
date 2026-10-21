"""depth_guard (unit): document-depth measurement and guarded parsing.

P0-1 context: recursive models make query depth unbounded and an MCP caller
is an LLM that can emit pathological documents — the guard must reject them
at the handler's choke point, including depth hidden behind fragment
spreads. Handler-level enforcement lives in integration/test_depth_guard.py.
"""

import pytest
from graphql import parse

from fastapi_mcp_neo.depth_guard import _FragmentCycle, document_depth, parse_guarded


class TestDocumentDepth:
    def test_depth_semantics(self):
        assert document_depth(parse("{ a }")) == 1
        assert document_depth(parse("{ a { b } }")) == 2
        assert document_depth(parse("{ a { b { c { d } } } }")) == 4

    def test_fragment_resolves_without_extra_level(self):
        doc = "query { a { ...F } } fragment F on T { b { c } }"
        assert document_depth(parse(doc)) == 3  # same as inlined { a { b { c } } }

    def test_inline_fragment_is_transparent(self):
        assert document_depth(parse("{ a { ... on T { b } } }")) == 2

    def test_cyclic_fragment_raises(self):
        doc = "query { ...F } fragment F on T { a { ...F } }"
        with pytest.raises(_FragmentCycle):
            document_depth(parse(doc))


class TestParseGuarded:
    def test_within_bounds_returns_document(self):
        error, document = parse_guarded("{ a { b { c } } }", 10)
        assert error is None
        assert document is not None  # caller hands it straight to execute()

    def test_too_deep_returns_error(self):
        error, document = parse_guarded("{ a { b { c { d { e { f } } } } } }", 5)
        assert document is None
        assert "exceeds max_depth=5" in error.message

    def test_syntax_error_surfaces(self):
        error, document = parse_guarded("{ a {", 10)
        assert document is None
        assert error is not None  # same envelope as graphql()


class TestIntrospectionExempt:
    """GraphiQL/codegen ship a fixed deep introspection document (depth
    ~15) — meta-fields must not count toward max_depth, or every standard
    tool breaks out of the box while the data guard stays intact."""

    def test_standard_introspection_query_passes_default_guard(self):
        from graphql.utilities import get_introspection_query

        error, document = parse_guarded(get_introspection_query(), max_depth=10)
        assert error is None
        assert document is not None

    def test_deep_data_selection_still_rejected(self):
        query = (
            "query Deep { t { a { b { c { d { e { f { g { h { i { j { k } } } } } } } } } } } }"
        )
        err, _ = parse_guarded(query, max_depth=10)
        assert err is not None and "exceeds max_depth=10" in err.message
