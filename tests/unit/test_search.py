"""Unit tests for the BM25 FieldSearchIndex (no MCP layer involved)."""

from __future__ import annotations

from fastapi_mcp_neo.search import FieldDoc, FieldSearchIndex


def docs() -> list[FieldDoc]:
    return [
        FieldDoc(
            name="get_invoice", type="Invoice",
            description="Fetch one invoice by id",
            args=[{"name": "invoice_id", "type": "Int!", "description": None}],
        ),
        FieldDoc(
            name="export_invoices", type="String",
            description="Export invoices to CSV",
        ),
        FieldDoc(
            name="create_payment", type="Payment",
            description="Record a payment against an invoice",
            operation="mutation",
        ),
        FieldDoc(
            name="list_shipments", type="[Shipment!]",
            description="List shipments with tracking",
        ),
        FieldDoc(
            name="audit_roles", type="AuditLog",
            description="Audit role assignments",
        ),
    ]


class TestTokenize:
    def test_splits_snake_case(self):
        from fastapi_mcp_neo.search import _tokenize

        assert _tokenize("get_invoice late") == ["get", "invoice", "late"]

    def test_casefold_and_nfkc(self):
        from fastapi_mcp_neo.search import _tokenize

        assert _tokenize("Invoice ＡＢＣ") == ["invoice", "abc"]

    def test_drops_single_chars_and_punctuation(self):
        from fastapi_mcp_neo.search import _tokenize

        # single chars and punctuation dropped; d_e -> one snake-split handled by caller
        assert _tokenize("a-b (c) d_e") == []


class TestSearch:
    def test_exact_name_match_ranks_first(self):
        idx = FieldSearchIndex(docs())
        results = idx.search("invoice", top_k=5)
        assert results[0][0].name in ("get_invoice", "export_invoices")

    def test_multi_token_query(self):
        idx = FieldSearchIndex(docs())
        results = idx.search("shipment tracking", top_k=3)
        assert results[0][0].name == "list_shipments"

    def test_description_terms_match(self):
        idx = FieldSearchIndex(docs())
        results = idx.search("CSV export", top_k=3)
        assert any(d.name == "export_invoices" for d, _ in results)

    def test_no_match_returns_empty(self):
        idx = FieldSearchIndex(docs())
        assert idx.search("banana") == []

    def test_empty_query_returns_empty(self):
        idx = FieldSearchIndex(docs())
        assert idx.search("") == []

    def test_top_k_bounds(self):
        idx = FieldSearchIndex(docs())
        assert len(idx.search("invoice", top_k=1)) == 1
        assert len(idx.search("invoice", top_k=100)) <= 5  # capped by corpus

    def test_rare_terms_score_higher_than_common(self):
        """A term appearing in one doc should rank that doc above a term
        appearing in all docs."""
        corpus = [
            FieldDoc(name="list_a", type="A", description="alpha beta"),
            FieldDoc(name="list_b", type="B", description="alpha gamma"),
            FieldDoc(name="list_c", type="C", description="alpha gamma"),
        ]
        idx = FieldSearchIndex(corpus)
        results = idx.search("beta", top_k=3)
        assert results[0][0].name == "list_a"
        assert results[0][1] > 0

    def test_zero_score_excluded(self):
        """Docs that match no query token must not appear."""
        idx = FieldSearchIndex(docs())
        results = idx.search("payment", top_k=10)
        assert all(d.name == "create_payment" for d, _ in results)

    def test_empty_corpus(self):
        idx = FieldSearchIndex([])
        assert idx.search("anything") == []
