"""unroll_exceeded's data walk, fragment branches (unit altitude).

The handler path never stamps fragment-bearing templates (pinned in
integration/test_recursive_expand.py), so the walk's InlineFragmentNode /
FragmentSpreadNode arms are reachable only through direct calls — this is
the correct altitude for them.
"""

from graphql import parse

from fastapi_mcp_neo.recursive_expand import unroll_exceeded

EDGES = frozenset({"children"})

# four edge levels under the operation's root field "get"
DATA = {
    "get": {
        "name": "root",
        "children": [
            {"name": "a", "children": [{"name": "b", "children": [
                {"name": "c", "children": []}
            ]}]}
        ],
    }
}


class TestInlineFragmentWalk:
    def test_excess_depth_through_inline_fragment_flags(self):
        doc = parse("{ get { ... on Node { children { children { name } } } } }")
        # the fragment's nesting carries the walk two edge levels deep
        assert unroll_exceeded(DATA, doc, EDGES, 1) is True

    def test_within_limit_through_inline_fragment_is_quiet(self):
        doc = parse("{ get { ... on Node { children { children { name } } } } }")
        assert unroll_exceeded(DATA, doc, EDGES, 5) is False


class TestFragmentSpreadWalk:
    def test_excess_depth_through_fragment_spread_flags(self):
        doc = parse(
            "query { get { ...Deep } } "
            "fragment Deep on Node { children { children { name } } }"
        )
        assert unroll_exceeded(DATA, doc, EDGES, 1) is True

    def test_within_limit_through_fragment_spread_is_quiet(self):
        doc = parse(
            "query { get { ...Deep } } "
            "fragment Deep on Node { children { children { name } } }"
        )
        assert unroll_exceeded(DATA, doc, EDGES, 5) is False
