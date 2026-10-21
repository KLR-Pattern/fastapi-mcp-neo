"""Recursive-chain unrolling: recursive fields return their true depth.

GraphQL documents are finite, so a selection on a recursive type silently
truncates the data at the selected depth — and INVISIBLY: ``children: []``
on a leaf looks exactly like a truncated subtree, so an agent cannot tell
a complete answer from a partial one. The route already computed the full
tree (routes are called once; nested resolution is pure in-memory
projection) — the document is the only thing limiting what gets walked.

Unrolling removes that limitation: the selection set where the agent stops
becomes the repeating template, stamped back under the recursive edge up
to ``unroll_limit()`` levels. Data still stops where it stops — empty lists
terminate the walk — so responses carry the tree's true depth while
per-level field filtering stays the agent's own native selections, at any
position in the document (the chain need not start at the root).

The contract is two rules: data no deeper than the limit returns in full
and untouched; data that exceeds the limit is served as deep as the
document goes, with a DEFINITE error naming the limit — truncation is
never silent, and complete data is never flagged. The limit itself is
honest: the deepest walk it can produce stays well inside the process
recursion budget, so it never promises depth the executor cannot deliver
(see ``unroll_limit``). The unrolled document's innermost repetition acts
as a probe — data occupying it proves deeper data exists, which is what
``unroll_exceeded`` reports.
"""

from __future__ import annotations

import sys
from typing import Any

from graphql import GraphQLSchema
from graphql.language import FieldNode
from graphql.language.ast import (
    DocumentNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    NameNode,
    OperationDefinitionNode,
    SelectionSetNode,
)
from graphql.language.visitor import Visitor, visit
from graphql.type.definition import GraphQLList, GraphQLNonNull, GraphQLObjectType
from graphql.utilities import TypeInfo


def unwrap(field_type: Any) -> Any:
    """Strip NonNull/List wrappers down to the named type. Shared with
    type_builder so the edge predicates cannot drift on a private twin."""
    while isinstance(field_type, (GraphQLNonNull, GraphQLList)):
        field_type = field_type.of_type
    return field_type


def is_direct_self_reference(field_type: Any, owning: Any) -> bool:
    """True when a field's type, unwrapped, IS the owning type — the
    direct self-reference edge. ``recursive_edges`` and type_builder's
    schema note both detect it through this one definition."""
    return unwrap(field_type) is owning


def unroll_limit() -> int:
    """How deep the unrolled document may go — a function of the process's
    recursion budget, not a fixed number.

    Measured end-to-end under Python's default recursion limit (1000
    frames): serving dies between 82 and 83 levels — about 12 frames per
    served level through the real path (FastAPI serialization plus
    graphql-core projection). ``recursionlimit // 16`` puts the deepest
    walk the limit can produce (~limit + 1 probe levels) at roughly three
    quarters of that ceiling, so the limit never promises depth the
    executor cannot deliver, and it scales up automatically when an
    operator raises ``sys.setrecursionlimit`` to serve deeper trees.
    """
    return max(16, sys.getrecursionlimit() // 16)


def unroll_exceeded(
    data: Any,
    document: DocumentNode,
    edge_names: frozenset[str],
    limit: int,
) -> bool:
    """True when the response proves data continues past ``limit`` edge
    levels — the data exceeded the unroll limit and the response is
    incomplete.

    The walk is GATED BY THE DOCUMENT: keys are examined only where the
    (expanded) document actually selects them, selection set by selection
    set. Edge-named keys inside a JSON passthrough payload are invisible
    to the document and never count — the response shape is the
    whitelist, so a payload whose keys collide with edge names cannot
    flag its query. The innermost stamped repetition is the probe: data
    occupying it proves deeper data exists; a true leaf leaves it empty
    (``children: []``, ``next: null``), so complete data never triggers.
    Edge links count list items and single-object links alike. Iterative
    on purpose: the whole point is to inspect a tree as deep as the
    limit without spending recursion on it."""
    fragments = {
        d.name.value: d.selection_set
        for d in document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }
    deepest = 0
    # Every operation's selections start at the root; only the executed
    # one's keys exist in the data, the rest fall through the key lookup.
    stack: list[tuple[Any, SelectionSetNode, int]] = [
        (data, op.selection_set, 0)
        for op in document.definitions
        if isinstance(op, OperationDefinitionNode)
    ]
    while stack:
        value, sel, depth = stack.pop()
        if not isinstance(value, dict):
            continue
        for node in sel.selections:
            if isinstance(node, FieldNode):
                key = node.alias.value if node.alias else node.name.value
                if key not in value:
                    continue
                child = value[key]
                is_edge = (
                    node.selection_set is not None
                    and node.name.value in edge_names
                    and bool(child)  # an empty list / null is a true leaf:
                    # the probe must stay empty for complete data
                )
                d = depth + 1 if is_edge else depth
                if d > deepest:
                    deepest = d
                if node.selection_set is not None and isinstance(
                    child, (list, dict)
                ):
                    if isinstance(child, list):
                        for item in child:
                            stack.append((item, node.selection_set, d))
                    else:
                        stack.append((child, node.selection_set, d))
            elif isinstance(node, InlineFragmentNode):
                stack.append((value, node.selection_set, depth))
            elif isinstance(node, FragmentSpreadNode):
                frag = fragments.get(node.name.value)
                if frag is not None:
                    stack.append((value, frag, depth))
    return deepest > limit


def recursive_edges(schema: GraphQLSchema) -> frozenset[tuple[str, str]]:
    """(type name, field name) pairs whose field type IS the owning type —
    direct self-reference (``Node.children: [Node!]``).

    Introspection meta-types are excluded; mutual cycles (``A.b: B``,
    ``B.a: A``) are deliberately not marked — template stamping is only
    defined for direct self-reference (v1 boundary)."""
    return frozenset(
        (t.name, fname)
        for t in schema.type_map.values()
        if isinstance(t, GraphQLObjectType) and not t.name.startswith("__")
        for fname, field in t.fields.items()
        if is_direct_self_reference(field.type, t)
    )


def expand_recursive_chains(
    document: DocumentNode,
    schema: GraphQLSchema,
    edges: frozenset[tuple[str, str]],
    depth: int,
) -> tuple[DocumentNode, bool]:
    """Return ``document`` with every recursive chain unrolled to ``depth``,
    plus whether any chain was stamped — the caller skips excess detection
    for unstamped documents (only an unrolled chain can cut data at the
    limit, so anything else must not pay the walk, or be flagged by it).

    A chain end is the deepest selection of a back-edge field along a path
    (detected anywhere in the document); its selection set becomes the
    template repeated under the back-edge. Chains whose template carries
    fragments are left untouched — GraphQL forbids self-referencing
    fragments, so nobody expresses recursion that way, and degrading to the
    unexpanded document is the honest fallback. The same fallback applies
    when a template selection already reserves the back-edge's response key
    (``children: name``): the stamp would collide with it at validation,
    and the agent's own key choice wins.

    Detection and stamping share one bottom-up pass: chain-end-ness is
    decidable from the node itself (its type context and its own
    selections), so no marking state has to survive the pass. That matters
    because stamping an inner chain end makes ``visit`` rebuild its
    ancestors — an identity-keyed marking table would silently miss an
    outer chain end nested above an inner one."""
    if not edges or depth <= 0:
        return document, False

    class Expander(Visitor):
        def __init__(self, type_info: TypeInfo) -> None:
            Visitor.__init__(self)
            self.type_info = type_info
            self.stamped = False

        def enter(self, node: Any, *_args: Any) -> None:
            self.type_info.enter(node)

        def leave(self, node: Any, *_args: Any) -> Any:
            try:
                if not isinstance(node, FieldNode):
                    return None
                parent = self.type_info.get_parent_type()
                field_def = self.type_info.get_field_def()
                if parent is None or field_def is None:
                    return None
                if (parent.name, node.name.value) not in edges:
                    return None
                if node.selection_set is None:
                    # A subselection-less back-edge selection is invalid
                    # GraphQL — leave it untouched so standard validation
                    # rejects it ("must have a selection of subfields")
                    # instead of crashing on the missing template.
                    return None
                own = unwrap(field_def.type).name
                selections = node.selection_set.selections
                if any(
                    isinstance(s, FieldNode) and (own, s.name.value) in edges
                    for s in selections
                ):
                    return None  # a deeper edge selection exists: not the chain end
                if any(not isinstance(s, FieldNode) for s in selections):
                    return None  # fragments inside the template: skip this chain
                if node.name.value in {
                    (s.alias or s.name).value
                    for s in selections
                    if isinstance(s, FieldNode)
                }:
                    return None  # response key taken by an alias: stamp would collide
                template = node.selection_set
                self.stamped = True
                current = template
                for _ in range(depth):
                    inner = FieldNode(
                        name=NameNode(value=node.name.value), selection_set=current
                    )
                    # AST child collections must be tuples, not lists.
                    current = SelectionSetNode(
                        selections=(*template.selections, inner)
                    )
                return FieldNode(
                    alias=node.alias,
                    name=node.name,
                    arguments=node.arguments,
                    directives=node.directives,
                    selection_set=current,
                )
            finally:
                self.type_info.leave(node)

    expander = Expander(TypeInfo(schema))
    expanded = visit(document, expander)
    assert isinstance(expanded, DocumentNode)
    return expanded, expander.stamped
