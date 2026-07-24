"""Proves the fakes in conftest are faithful enough to exercise the real
storage layer — if these fail, every other test built on them is meaningless."""

from app import abstractions as ab
from app.models import Page, make_partition_key

SCOPE = make_partition_key("individual", "dev-user")


def _page(title: str, body: str = "Body text.") -> Page:
    return Page(partition_key=SCOPE, title=title, body=body, tier="individual", owner_id="dev-user")


def test_write_page_puts_body_in_blob_and_metadata_in_cosmos(azure):
    page = ab.write_page(_page("Acme Q3"), change_type="ingest", author_id="dev-user")

    # Blob is canonical: the body lives there, keyed by the page's blob_path.
    assert azure.blob.read_text(page.blob_path) is not None
    # Cosmos holds metadata only — no body field anywhere in the index row.
    row = azure.cosmos.get_container("page_index").read_item(page.id, SCOPE)
    assert row["title"] == "Acme Q3"
    assert "body" not in row


def test_round_trip_through_read_page(azure):
    written = ab.write_page(
        _page("Acme Q3", "The revenue figure."), change_type="ingest", author_id="u"
    )
    read = ab.read_page(written.id, SCOPE)
    assert read is not None
    assert read.title == "Acme Q3"
    assert read.body == "The revenue figure."


def test_version_snapshot_is_written(azure):
    page = ab.write_page(_page("Acme Q3"), change_type="ingest", author_id="u")
    assert f"individual/dev-user/{page.slug}/v1.md" in azure.blob.wiki


def test_partitions_isolate_tenants(azure):
    ab.write_page(_page("Mine"), change_type="ingest", author_id="dev-user")
    other = make_partition_key("individual", "someone-else")
    assert ab.list_pages(other) == []
    assert len(ab.list_pages(SCOPE)) == 1


def test_search_orders_by_relevance(azure):
    ab.write_page(_page("Revenue", "quarterly revenue growth"), change_type="ingest", author_id="u")
    ab.write_page(
        _page("Hiring", "engineering headcount plan"), change_type="ingest", author_id="u"
    )

    hits = ab.search("quarterly revenue", SCOPE)
    assert hits[0]["title"] == "Revenue"


def test_model_calls_are_recorded(azure):
    ab.write_page(_page("Acme Q3"), change_type="ingest", author_id="u")
    # write_page embeds the body; nothing else should have called the chat model.
    assert any(c["kind"] == "embed" for c in azure.model.calls)
    assert not any(c["kind"].startswith("call_model") for c in azure.model.calls)


def test_cosmos_wipe_leaves_blob_intact(azure):
    """The precondition reindex() depends on: Cosmos is disposable, blob isn't."""
    page = ab.write_page(_page("Acme Q3"), change_type="ingest", author_id="u")
    azure.cosmos.wipe()

    assert ab.read_page(page.id, SCOPE) is None  # index gone
    assert azure.blob.read_text(page.blob_path) is not None  # content safe
