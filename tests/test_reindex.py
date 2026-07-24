"""reindex() is the stated recovery path for a wiped or drifted Cosmos index.
These tests wipe the index and assert what survives — identity, version
history, provenance, and the link graph the knowledge map is built from.
"""

from app import abstractions as ab
from app.models import Page, make_partition_key

SCOPE = make_partition_key("individual", "dev-user")


def _write(azure, title, body="Body.", source_refs=None):
    return ab.write_page(
        Page(
            partition_key=SCOPE,
            title=title,
            body=body,
            tier="individual",
            owner_id="dev-user",
            source_refs=source_refs or [],
        ),
        change_type="ingest",
        author_id="dev-user",
    )


def test_page_ids_survive_a_wipe(azure):
    """Ids must be stable: links_out stores page ids, so new ids on every
    rebuild would orphan every cross-reference in the wiki."""
    original = _write(azure, "Acme Q3")
    azure.cosmos.wipe()

    ab.reindex("individual", "dev-user")

    restored = ab.read_page(original.id, SCOPE)
    assert restored is not None
    assert restored.title == "Acme Q3"


def test_provenance_survives_a_wipe(azure):
    """The build spec calls provenance a hard requirement; source refs live
    only in the index, so frontmatter is the only thing that can restore them."""
    page = _write(azure, "Acme Q3", source_refs=["individual/dev-user/abc-acme.pdf"])
    azure.cosmos.wipe()

    ab.reindex("individual", "dev-user")

    assert ab.read_page(page.id, SCOPE).source_refs == ["individual/dev-user/abc-acme.pdf"]


def test_created_at_survives_a_wipe(azure):
    page = _write(azure, "Acme Q3")
    azure.cosmos.wipe()

    ab.reindex("individual", "dev-user")

    assert ab.read_page(page.id, SCOPE).created_at == page.created_at


def test_version_is_not_reset(azure):
    """Resetting to v1 would make the next write overwrite the v1 snapshot
    with present-day content, destroying the history it claims to keep."""
    page = _write(azure, "Acme Q3", body="First.")
    page.body = "Second."
    page.version += 1
    ab.write_page(page, change_type="manual_edit", author_id="dev-user")
    azure.cosmos.wipe()

    ab.reindex("individual", "dev-user")

    assert ab.read_page(page.id, SCOPE).version == 2


def test_version_snapshots_are_not_overwritten(azure):
    page = _write(azure, "Acme Q3", body="First.")
    v1_before = azure.blob.read_text(f"individual/dev-user/{page.slug}/v1.md")
    page.body = "Second."
    page.version += 1
    ab.write_page(page, change_type="manual_edit", author_id="dev-user")

    azure.cosmos.wipe()
    ab.reindex("individual", "dev-user")

    assert azure.blob.read_text(f"individual/dev-user/{page.slug}/v1.md") == v1_before


def test_version_rows_are_restored_from_snapshots(azure):
    page = _write(azure, "Acme Q3", body="First.")
    page.body = "Second."
    page.version += 1
    ab.write_page(page, change_type="manual_edit", author_id="dev-user")
    azure.cosmos.wipe()

    ab.reindex("individual", "dev-user")

    versions = ab.list_versions(page.id, SCOPE)
    assert [v.version_number for v in versions] == [2, 1]


def test_links_out_are_rebuilt(azure):
    """The knowledge map is built entirely from links_out, so losing it here
    collapses the map without any other visible symptom."""
    target = _write(azure, "Acme")
    source = _write(azure, "Q3 Review", body="See [[Acme]] for background.")
    azure.cosmos.wipe()

    ab.reindex("individual", "dev-user")

    assert ab.read_page(source.id, SCOPE).links_out == [target.id]


def test_links_to_missing_pages_are_dropped(azure):
    source = _write(azure, "Q3 Review", body="See [[Nonexistent]].")
    azure.cosmos.wipe()

    ab.reindex("individual", "dev-user")

    assert ab.read_page(source.id, SCOPE).links_out == []


def test_reindex_is_idempotent(azure):
    """Re-running must converge, not duplicate: the original implementation
    minted a fresh uuid per page per run, doubling the index each time."""
    _write(azure, "Acme Q3")
    _write(azure, "Hiring Plan")
    azure.cosmos.wipe()

    ab.reindex("individual", "dev-user")
    first = {p.id for p in ab.list_pages(SCOPE)}
    ab.reindex("individual", "dev-user")
    second = {p.id for p in ab.list_pages(SCOPE)}

    assert first == second
    assert len(second) == 2


def test_reindex_does_not_create_new_versions(azure):
    page = _write(azure, "Acme Q3")
    azure.cosmos.wipe()

    ab.reindex("individual", "dev-user")
    ab.reindex("individual", "dev-user")

    assert ab.read_page(page.id, SCOPE).version == 1
    assert len(ab.list_versions(page.id, SCOPE)) == 1


def test_legacy_page_without_frontmatter_gets_a_stable_id(azure):
    """Pages written before frontmatter existed still have to reindex, and
    still have to land on the same id every run."""
    azure.blob.wiki["individual/dev-user/legacy-note.md"] = b"# Legacy Note\n\nOld content."

    ab.reindex("individual", "dev-user")
    first = {p.id: p.title for p in ab.list_pages(SCOPE)}
    ab.reindex("individual", "dev-user")
    second = {p.id: p.title for p in ab.list_pages(SCOPE)}

    assert first == second
    assert "Legacy Note" in first.values()


def test_schema_and_metadata_blobs_are_not_indexed_as_pages(azure):
    """ "_"-prefixed blobs are app metadata (schema doc, lint queue, map cache)
    and must never be swept into the page index."""
    _write(azure, "Acme Q3")
    azure.blob.wiki["individual/dev-user/_schema.md"] = b"---\n---\n# Schema"
    azure.cosmos.wipe()

    ab.reindex("individual", "dev-user")

    assert [p.title for p in ab.list_pages(SCOPE)] == ["Acme Q3"]


def test_dry_run_writes_nothing(azure):
    _write(azure, "Acme Q3")
    azure.cosmos.wipe()

    summary = ab.reindex("individual", "dev-user", dry_run=True)

    assert summary["pages"] == 1
    assert summary["dry_run"] is True
    assert ab.list_pages(SCOPE) == []
