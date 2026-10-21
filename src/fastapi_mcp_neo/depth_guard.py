"""Reject overly deep GraphQL documents before execution.

Recursive models (a Node whose children are Nodes) make query depth
unbounded: graphql-core happily executes a thousand nested selections, and
an MCP caller is an LLM that can emit such a document. The guard measures
selection-set nesting (the root selection set is depth 1) with fragments
resolved inline and cycles rejected — spreading a deep query across
fragments cannot hide it.

Introspection meta-fields (``__``-prefixed) do not count: standard tooling
(GraphiQL, codegen, IDE plugins) ships a fixed, deep introspection document
— depth ~15 — whose shape is bounded by the schema itself, not by the
caller. The guard targets runaway DATA selections.
"""

from __future__ import annotations

from graphql import GraphQLError
from graphql.language import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    parse,
)


class _FragmentCycle(Exception):
    """A fragment spread referencing itself (illegal GraphQL anyway)."""


def document_depth(document: DocumentNode) -> int:
    """Deepest selection-set nesting across all operations (root = 1).

    Fragment spreads resolve into their definition's selection set without
    adding a level of their own (``{ ...F }`` with ``fragment F on T
    { a { b } }`` is depth 2, same as the inlined form); a spread cycle
    raises, since the real document would recurse forever.
    """
    fragments = {
        d.name.value: d
        for d in document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }

    def selection_depth(sel_set: SelectionSetNode, active: frozenset[str]) -> int:
        best = 1
        for sel in sel_set.selections:
            if isinstance(sel, FieldNode):
                if sel.name.value.startswith("__"):
                    # introspection meta-fields don't count toward depth —
                    # see module docstring
                    continue
                if sel.selection_set is not None:
                    best = max(best, 1 + selection_depth(sel.selection_set, active))
            elif isinstance(sel, InlineFragmentNode):
                if sel.selection_set is not None:
                    best = max(best, selection_depth(sel.selection_set, active))
            elif isinstance(sel, FragmentSpreadNode):
                name = sel.name.value
                if name in active:
                    raise _FragmentCycle
                frag = fragments.get(name)
                if frag is not None:
                    best = max(best, selection_depth(frag.selection_set, active | {name}))
        return best

    deepest = 0
    for defn in document.definitions:
        if isinstance(defn, OperationDefinitionNode) and defn.selection_set is not None:
            deepest = max(deepest, selection_depth(defn.selection_set, frozenset()))
    return deepest


def parse_guarded(
    query: str, max_depth: int
) -> tuple[GraphQLError | None, DocumentNode | None]:
    """Parse the document once and depth-check it in the same pass.

    Returns ``(error, document)``: an error is set (syntax error, cyclic
    fragment spreads, or too deep) when the document was rejected — and then
    there is no document; otherwise the parsed document is handed back for
    graphql-core to consume directly (``execute`` accepts a pre-parsed
    ``DocumentNode``) — the caller must NOT parse the query string again.
    """
    try:
        document = parse(query)
    except GraphQLError as exc:
        return exc, None
    try:
        depth = document_depth(document)
    except _FragmentCycle:
        return (
            GraphQLError(f"Document has cyclic fragment spreads (max_depth={max_depth})."),
            None,
        )
    if depth > max_depth:
        return (
            GraphQLError(f"Query depth {depth} exceeds max_depth={max_depth}."),
            None,
        )
    return None, document
