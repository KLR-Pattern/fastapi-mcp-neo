"""Recursive-chain unrolling: recursive fields return their true depth.

The route already computed the full tree; unrolling removes the document's
depth limitation. Selecting a recursive field means "the whole subtree" —
the selection shape where the agent stops repeats to whatever depth the
data has, at any position in the document.
"""

from typing import Any

from fastapi import FastAPI
from graphql import GraphQLError, ValidationRule
from pydantic import BaseModel

from fastapi_mcp_neo.handler import RouterGraphQLHandler


class Node(BaseModel):
    name: str
    children: list["Node"] = []


Node.model_rebuild()


def chain(depth: int, name: str) -> Node:
    if depth == 1:
        return Node(name=name, children=[])
    return Node(name=name, children=[chain(depth - 1, f"{name}.{depth}")])


def deepest(node: dict) -> int:
    return 1 + (deepest(node["children"][0]) if node.get("children") else 0)


class TestTrueDepth:
    async def test_shallow_selection_returns_full_depth(self):
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(6, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ t { tree { name children { name } } } }")
        assert "errors" not in result, result
        assert deepest(result["data"]["t"]["tree"]) == 6

    async def test_field_filtering_repeats_per_level(self):
        """The template is the STOPPING level's selection, applied to every
        descendant. A rich model (extra fields the query never selects below
        the top) makes a filtering regression observable: if unrolling lost
        the template, description/price/sku would leak into descendants."""

        class RichNode(BaseModel):
            name: str
            description: str = "must not leak into descendants"
            price_cents: int = 999
            sku: str = "SKU-X"
            children: list["RichNode"] = []

        RichNode.model_rebuild()

        def rich_chain(depth: int, name: str) -> RichNode:
            if depth == 1:
                return RichNode(name=name)
            return RichNode(
                name=name, children=[rich_chain(depth - 1, f"{name}.{depth}")]
            )

        app = FastAPI()

        @app.get("/rich", response_model=RichNode, tags=["t"])
        async def rich() -> RichNode:
            return rich_chain(5, "root")

        handler = RouterGraphQLHandler(app)
        # top level selects {name, description}; the STOPPING level (inside
        # children) selects only {name}: descendants must carry exactly that.
        result = await handler.execute(
            "{ t { rich { name description children { name } } } }"
        )
        assert "errors" not in result, result
        root = result["data"]["t"]["rich"]
        assert set(root.keys()) == {"name", "description", "children"}

        def walk_descendants(node: dict) -> None:
            for child in node["children"]:
                assert set(child.keys()) == {"name", "children"}, sorted(child.keys())
                walk_descendants(child)

        walk_descendants(root)
        assert deepest(root) == 5  # filtering AND full depth together

    async def test_manual_deeper_selection_still_complete(self):
        """Hand-writing more levels than needed is legal GraphQL; the chain
        end still stamps the remainder, so the result is the same tree."""
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(5, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { tree { children { children { name } } } } }"
        )
        assert "errors" not in result, result
        assert deepest(result["data"]["t"]["tree"]) == 5

    async def test_chain_mid_document_siblings_untouched(self):
        """The recursive type enters below variants.parent — the chain is
        detected at any position and only the chain itself deepens."""
        class Variant(BaseModel):
            sku: str
            parent: Node

        class Product(BaseModel):
            title: str
            variants: list[Variant]

        app = FastAPI()

        @app.get("/products", response_model=list[Product], tags=["t"])
        async def list_products():
            return [
                Product(title="p1", variants=[Variant(sku="v1", parent=chain(6, "n"))])
            ]

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { list_products { title variants { sku parent { name"
            " children { name } } } } } }"
        )
        assert "errors" not in result, result
        product = result["data"]["t"]["list_products"][0]
        assert product["title"] == "p1" and product["variants"][0]["sku"] == "v1"
        assert deepest(product["variants"][0]["parent"]) == 6

    async def test_multiple_chains_independent(self):
        app = FastAPI()

        @app.get("/a", response_model=Node, tags=["t"])
        async def a():
            return chain(3, "a")

        @app.get("/b", response_model=Node, tags=["t"])
        async def b():
            return chain(5, "b")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { a { name children { name } } b { name children { name } } } }"
        )
        assert "errors" not in result, result
        assert deepest(result["data"]["t"]["a"]) == 3
        assert deepest(result["data"]["t"]["b"]) == 5

    async def test_nested_chains_both_unroll(self):
        """A chain end nested inside another chain's template unrolls too.
        Stamping the inner chain makes visit() rebuild the outer chain-end
        node, so detection must not depend on node identity surviving the
        pass — it is decided from each node itself, bottom-up."""

        class B(BaseModel):
            tag: str
            xs: list["B"] = []

        B.model_rebuild()

        class NestedA(BaseModel):
            name: str
            children: list["NestedA"] = []
            b: "B | None" = None

        NestedA.model_rebuild()

        def bchain(d: int, tag: str) -> B:
            if d == 1:
                return B(tag=tag)
            return B(tag=tag, xs=[bchain(d - 1, f"{tag}.{d}")])

        app = FastAPI()

        @app.get("/nested", response_model=NestedA, tags=["t"])
        async def nested() -> NestedA:
            leaf = NestedA(name="leaf", b=bchain(3, "bx"))
            mid = NestedA(name="mid", children=[leaf])
            return NestedA(name="root", children=[mid])

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { nested { children { name b { tag xs { tag } } } } } }"
        )
        assert "errors" not in result, result
        # the outer chain reaches its leaf instead of stopping at mid
        leaf_node = result["data"]["t"]["nested"]["children"][0]["children"][0]
        assert leaf_node["name"] == "leaf"
        assert leaf_node["children"] == []
        # the inner chain hanging off that leaf is complete too
        b = leaf_node["b"]
        assert b["tag"] == "bx"
        assert b["xs"][0]["tag"] == "bx.3"
        assert b["xs"][0]["xs"][0]["tag"] == "bx.3.2"
        assert b["xs"][0]["xs"][0]["xs"] == []


class TestBoundaries:
    async def test_max_depth_still_guards_the_written_document(self):
        """The runaway guard sees the AGENT's document; unrolling happens
        after it and is a system behavior, not an injection bypass."""
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(2, "root")

        handler = RouterGraphQLHandler(app, max_depth=5)
        query = (
            "{ t { tree { children { children { children { name } } } } } }"
        )
        result = await handler.execute(query)
        assert "data" not in result
        assert "exceeds max_depth=5" in result["errors"][0]["message"]

    async def test_fragment_in_template_left_unexpanded(self):
        """Fragments cannot express recursion (spec forbids the cycle), so a
        template carrying one degrades honestly to the written document —
        selected depth only."""
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(4, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { tree { name children { ... on Node { name } } } } }"
        )
        assert "errors" not in result, result
        # unexpanded: the selected depth is what comes back
        node = result["data"]["t"]["tree"]["children"][0]
        assert set(node.keys()) == {"name"}

    async def test_aliased_response_key_skips_chain(self):
        """``children: name`` reserves the response key the stamp would use.
        Both cannot coexist in one selection set, so the chain degrades to
        the written document — the agent's own key choice wins."""
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(4, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { tree { children { name children: name } } } }"
        )
        assert "errors" not in result, result
        # unexpanded: the alias holds the scalar, not a stamped subtree
        node = result["data"]["t"]["tree"]["children"][0]
        assert node["name"] == "root.4"
        assert node["children"] == "root.4"

    async def test_alias_skip_keeps_normal_unrolling(self):
        """The response-key check must not over-skip: without the colliding
        alias the same schema still unrolls to true depth."""
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(6, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { tree { name children { name } } } }"
        )
        assert "errors" not in result, result
        assert deepest(result["data"]["t"]["tree"]) == 6

    async def test_outer_alias_still_unrolls(self):
        """Aliasing the back-edge itself (``kids: children``) does not
        collide with the stamp's response key — unrolling proceeds; only
        the first level answers under the alias, stamped levels under the
        edge's own name."""
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(5, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { tree { kids: children { name } } } }"
        )
        assert "errors" not in result, result

        def stamped_depth(node: dict, key: str) -> int:
            kids = node.get(key) or []
            return 1 + (stamped_depth(kids[0], "children") if kids else 0)

        assert stamped_depth(result["data"]["t"]["tree"], "kids") == 5

    async def test_mutual_recursion_untouched(self):
        """A.b: B / B.a: A is a cycle without a direct self-edge — not
        unrolled (v1 boundary), and building/querying still works."""
        class B(BaseModel):
            tag: str
            a: "A | None" = None

        class A(BaseModel):
            name: str
            b: B | None = None

        A.model_rebuild()
        B.model_rebuild()

        app = FastAPI()

        @app.get("/pair", response_model=A, tags=["t"])
        async def pair():
            return A(name="x", b=B(tag="y"))

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ t { pair { name b { tag } } } }")
        assert "errors" not in result, result
        assert result["data"]["t"]["pair"]["b"] == {"tag": "y"}

    async def test_subselectionless_edge_rejected_cleanly(self):
        """A recursive field selected without a subselection is invalid
        GraphQL: the standard validation error comes back (exactly like
        any other field), never a crash on the missing template."""
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(3, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ t { tree { name children } } }")
        assert "data" not in result, result
        messages = [e["message"] for e in result["errors"]]
        assert any(
            "must have a selection of subfields" in m for m in messages
        ), messages

    async def test_custom_rules_see_the_written_document(self):
        """Caller-supplied validation rules judge the document the agent
        WROTE — unrolling is a system behavior that would inflate any
        field/complexity count and misfire cost guards on reasonable
        recursive selections."""

        class MaxFields(ValidationRule):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.count = 0

            def enter_field(self, node, *a):
                self.count += 1
                if self.count > 50:
                    self.report_error(
                        GraphQLError("cost guard: more than 50 fields")
                    )

        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(3, "root")

        handler = RouterGraphQLHandler(app, validation_rules=[MaxFields])
        result = await handler.execute(
            "{ t { tree { name children { name } } } }"
        )
        assert "errors" not in result, result
        assert deepest(result["data"]["t"]["tree"]) == 3

        # the guard still fires on a genuinely fat WRITTEN document
        fat = RouterGraphQLHandler(
            app, validation_rules=[MaxFields], max_depth=None
        )
        wide = "{ t { tree { name " + "children { name " * 30 + "}" * 33
        result = await fat.execute(wide)
        messages = [e["message"] for e in result.get("errors", [])]
        assert any("cost guard" in m for m in messages), messages


class TestSchemaNote:
    async def test_contract_note_once_per_type_not_per_field(self):
        """The true-depth contract rides the recursive TYPE's description —
        one copy however many self-referencing fields the type carries (the
        SDL is token-priced; per-field copies would be redundant), placed
        where agents read the field list they select into."""
        class Family(BaseModel):  # two self-referencing fields on one type
            name: str
            siblings: list["Family"] = []
            cousins: list["Family"] = []

        Family.model_rebuild()
        app = FastAPI()

        @app.get("/family", response_model=Family, tags=["t"])
        async def family():
            return Family(name="x", siblings=[], cousins=[])

        handler = RouterGraphQLHandler(app)
        sdl = handler.get_sdl()
        assert sdl.count("full subtree at true depth") == 1  # once per type
        assert sdl.count("repeats per level") == 1
        assert "siblings: [Family!]" in sdl and "cousins: [Family!]" in sdl


class TestUnrollLimit:
    async def test_limit_tracks_recursion_budget(self, monkeypatch):
        from fastapi_mcp_neo.recursive_expand import unroll_limit

        monkeypatch.setattr("sys.setrecursionlimit", lambda n: None)
        monkeypatch.setattr("sys.getrecursionlimit", lambda: 1000)
        assert unroll_limit() == 62  # honest: the deepest walk (~limit + 1
        # levels) stays under the measured 82-level ceiling at 1000 frames

        monkeypatch.setattr("sys.getrecursionlimit", lambda: 20000)
        assert unroll_limit() == 1250  # raised budget scales up with it

    async def test_floor_hit_reports_error_not_silent(self, monkeypatch):
        """Data deeper than the scaffold must never vanish silently: the
        errors channel carries a definite error while the data stays."""
        TREE = {"name": "n", "children": [
            {"name": "n", "children": [
                {"name": "n", "children": [
                    {"name": "n", "children": [
                        {"name": "n", "children": []},
                    ]},
                ]},
            ]},
        ]}  # 5 levels against a scaffold of 3

        monkeypatch.setattr("fastapi_mcp_neo.handler.unroll_limit", lambda: 3)
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            raise RuntimeError("invoker patched")

        handler = RouterGraphQLHandler(app)

        async def fake_invoke(*a, **k):
            return TREE

        handler._invoker.invoke = fake_invoke
        result = await handler.execute("{ t { tree { name children { name } } } }")
        assert result["data"]["t"]["tree"] is not None  # data stays
        messages = [e["message"] for e in result.get("errors", [])]
        assert any("exceeded the unroll limit (3 levels)" in m for m in messages), messages
        assert any("response is incomplete" in m for m in messages)

    async def test_no_floor_error_below_limit(self, monkeypatch):
        monkeypatch.setattr("fastapi_mcp_neo.handler.unroll_limit", lambda: 10)
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(3, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ t { tree { name children { name } } } }")
        assert "errors" not in result, result

    async def test_raised_budget_serves_deep_tree(self):
        """The adaptive limit in action: raise the process budget and a
        200-level tree serves in full, no floor error."""
        import sys

        old = sys.getrecursionlimit()
        sys.setrecursionlimit(10000)
        try:
            TREE = {"name": "n", "children": []}
            node = TREE
            for _ in range(199):
                node["children"] = [{"name": "n", "children": []}]
                node = node["children"][0]

            app = FastAPI()

            @app.get("/tree", response_model=Node, tags=["t"])
            async def tree():
                raise RuntimeError("invoker patched")

            handler = RouterGraphQLHandler(app)

            async def fake_invoke(*a, **k):
                return TREE

            handler._invoker.invoke = fake_invoke
            result = await handler.execute(
                "{ t { tree { name children { name } } } }"
            )
            assert "errors" not in result, result
            assert deepest(result["data"]["t"]["tree"]) == 200
        finally:
            sys.setrecursionlimit(old)

    async def test_data_at_exact_limit_is_complete(self, monkeypatch):
        """The probe level separates complete from truncated. Stamping
        ``limit`` times projects one probe level beyond it, so data up to
        ``limit + 1`` levels serves COMPLETE and silent; deeper data
        occupies the probe and errors definitely. Complete data is never
        flagged."""
        monkeypatch.setattr("fastapi_mcp_neo.handler.unroll_limit", lambda: 5)
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            raise RuntimeError("invoker patched")

        handler = RouterGraphQLHandler(app)
        box: dict[str, Any] = {}

        async def fake_invoke(*a, **k):
            return box["tree"]

        handler._invoker.invoke = fake_invoke

        box["tree"] = chain(6, "root")  # probe level exactly reached
        result = await handler.execute("{ t { tree { name children { name } } } }")
        assert "errors" not in result, result
        assert deepest(result["data"]["t"]["tree"]) == 6

        box["tree"] = chain(7, "root")  # one past the probe: truncated
        result = await handler.execute("{ t { tree { name children { name } } } }")
        messages = [e["message"] for e in result.get("errors", [])]
        assert any("exceeded the unroll limit (5 levels)" in m for m in messages), messages
        assert result["data"]["t"]["tree"] is not None  # partial data stays

    async def test_single_object_edge_excess_is_loud(self, monkeypatch):
        """Single-object recursive edges (next: LNode | None) count as edge
        links in the excess walk: a truncated chain-link list errors, a
        complete one stays silent."""

        class LNode(BaseModel):
            name: str
            next: "LNode | None" = None

        LNode.model_rebuild()

        def lchain(d: int, name: str) -> LNode:
            node = LNode(name=f"{name}.{d}")
            for i in range(d - 1, 0, -1):
                node = LNode(name=f"{name}.{i}", next=node)
            return node

        monkeypatch.setattr("fastapi_mcp_neo.handler.unroll_limit", lambda: 3)
        app = FastAPI()

        @app.get("/ll", response_model=LNode, tags=["t"])
        async def ll():
            raise RuntimeError("invoker patched")

        handler = RouterGraphQLHandler(app)
        box: dict[str, Any] = {}

        async def fake_invoke(*a, **k):
            return box["node"]

        handler._invoker.invoke = fake_invoke

        box["node"] = lchain(10, "n")  # 10 links against a limit of 3
        result = await handler.execute("{ t { ll { name next { name } } } }")
        messages = [e["message"] for e in result.get("errors", [])]
        assert any("exceeded the unroll limit (3 levels)" in m for m in messages), messages

        box["node"] = lchain(3, "n")  # exactly at the limit: complete
        result = await handler.execute("{ t { ll { name next { name } } } }")
        assert "errors" not in result, result

    async def test_unstamped_document_never_flagged(self):
        """Only a stamped document can truncate at the limit: a query that
        touches no recursive field skips the excess walk entirely, so a
        JSON passthrough payload whose keys merely collide with edge names
        never attracts an error."""

        class Audit(BaseModel):
            payload: dict[str, Any]

        def nested(key: str, depth: int) -> dict[str, Any]:
            node: dict[str, Any] = {}
            for _ in range(depth):
                node = {key: node}
            return node

        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(2, "root")

        @app.get("/audit", response_model=Audit, tags=["t"])
        async def audit():
            return Audit(payload=nested("children", 150))

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ t { audit { payload } } }")
        assert "errors" not in result, result

    async def test_mixed_document_payload_never_flagged(self):
        """The excess walk is gated by the document: in a query selecting
        BOTH a (shallow) recursive tree and a JSON passthrough payload whose
        keys collide with edge names, the payload's nesting is invisible to
        the document and cannot flag the response."""

        class Audit(BaseModel):
            payload: dict[str, Any]

        def nested(key: str, depth: int) -> dict[str, Any]:
            node: dict[str, Any] = {}
            for _ in range(depth):
                node = {key: node}
            return node

        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(3, "root")

        @app.get("/audit", response_model=Audit, tags=["t"])
        async def audit():
            return Audit(payload=nested("children", 150))

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { tree { name children { name } } audit { payload } } }"
        )
        assert "errors" not in result, result
        assert deepest(result["data"]["t"]["tree"]) == 3

    async def test_limit_pinned_per_instance(self):
        """The limit is read once at construction: raising the process
        budget later serves deeper trees only on NEW handlers — an old one
        keeps its baked-in documents and ERRORS on data beyond its pinned
        limit instead of truncating silently."""
        import sys

        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            raise RuntimeError("invoker patched")

        handler = RouterGraphQLHandler(app)  # pins the default-budget limit
        pinned = handler._unroll_limit
        box: dict[str, Any] = {}

        async def fake_invoke(*a, **k):
            return box["tree"]

        handler._invoker.invoke = fake_invoke

        def wide(depth: int) -> dict[str, Any]:
            node: dict[str, Any] = {"name": "leaf", "children": []}
            for _ in range(depth - 1):
                node = {"name": "n", "children": [node]}
            return node

        box["tree"] = chain(3, "root")
        warm = await handler.execute("{ t { tree { name children { name } } } }")
        assert "errors" not in warm, warm  # compiled + cached at pinned limit

        old = sys.getrecursionlimit()
        sys.setrecursionlimit(10000)
        try:
            # same query string → cache hit; deep data now exceeds the PINNED
            # limit → definite error, never a silent shallow serve
            box["tree"] = wide(pinned + 50)
            result = await handler.execute(
                "{ t { tree { name children { name } } } }"
            )
            messages = [e["message"] for e in result.get("errors", [])]
            assert any(
                f"exceeded the unroll limit ({pinned} levels)" in m
                for m in messages
            ), messages
            assert result["data"]["t"]["tree"] is not None  # data stays

            # a NEW handler picks up the raised budget and serves it in full
            handler2 = RouterGraphQLHandler(app)
            handler2._invoker.invoke = fake_invoke
            full = await handler2.execute(
                "{ t { tree { name children { name } } } }"
            )
            assert "errors" not in full, full
            assert deepest(full["data"]["t"]["tree"]) == pinned + 50
        finally:
            sys.setrecursionlimit(old)

    async def test_deep_data_errors_cleanly_at_default_budget(self):
        """The honest limit in action: data deeper than the limit —
        including depths between the old floor (100) and the real ceiling
        (~82 inverted: depths the old code died on) — serves partial data
        with a definite error instead of blowing the stack."""
        from fastapi_mcp_neo.recursive_expand import unroll_limit

        limit = unroll_limit()
        box: dict[str, Any] = {}
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            raise RuntimeError("invoker patched")

        handler = RouterGraphQLHandler(app)

        async def fake_invoke(*a, **k):
            return box["tree"]

        handler._invoker.invoke = fake_invoke

        def wide(depth: int) -> dict[str, Any]:
            node: dict[str, Any] = {"name": "leaf", "children": []}
            for _ in range(depth - 1):
                node = {"name": "n", "children": [node]}
            return node

        # data exactly at the limit: complete, silent
        box["tree"] = wide(limit)
        result = await handler.execute("{ t { tree { name children { name } } } }")
        assert "errors" not in result, result
        assert deepest(result["data"]["t"]["tree"]) == limit

        # data 20 beyond the limit (the old crash band): partial data +
        # definite error, no stack blowup
        box["tree"] = wide(limit + 20)
        result = await handler.execute("{ t { tree { name children { name } } } }")
        assert result["data"]["t"]["tree"] is not None
        messages = [e["message"] for e in result.get("errors", [])]
        assert any(
            f"exceeded the unroll limit ({limit} levels)" in m for m in messages
        ), messages
        assert not any("maximum recursion" in m for m in messages), messages
