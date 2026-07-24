"""Frontmatter is the substrate reindex() recovers from, so its round-trip and
its tolerance of files it didn't write are both load-bearing."""

from app import abstractions as ab
from app import frontmatter
from app.models import Page, body_hash, make_partition_key

SCOPE = make_partition_key("individual", "dev-user")


def test_round_trip():
    meta = {"id": "abc", "title": "Acme Q3", "version": 3, "source_refs": ["a.pdf", "b.docx"]}
    parsed, body = frontmatter.parse(frontmatter.serialize(meta, "# Acme Q3\n\nText."))

    assert parsed["id"] == "abc"
    assert parsed["title"] == "Acme Q3"
    assert parsed["version"] == "3"  # scalars come back as strings; callers coerce
    assert parsed["source_refs"] == ["a.pdf", "b.docx"]
    assert body == "# Acme Q3\n\nText."


def test_body_without_frontmatter_is_untouched():
    """A page hand-edited in a markdown editor must keep working — the storage
    discipline rule cuts both ways."""
    raw = "# Just a heading\n\nNo frontmatter here."
    assert frontmatter.parse(raw) == ({}, raw)


def test_unterminated_block_is_treated_as_body():
    raw = "---\nid: abc\nno closing delimiter"
    meta, body = frontmatter.parse(raw)
    assert meta == {}
    assert body == raw


def test_title_containing_a_colon_survives():
    meta, _ = frontmatter.parse(frontmatter.serialize({"title": "Q3: the reckoning"}, "x"))
    assert meta["title"] == "Q3: the reckoning"


def test_empty_values_are_omitted_not_blank():
    out = frontmatter.serialize({"id": "a", "source_refs": [], "created_at": ""}, "body")
    assert "source_refs" not in out
    assert "created_at" not in out


# ------------------------------------------------- integration with the store


def test_frontmatter_is_in_blob_but_not_in_page_body(azure):
    page = ab.write_page(
        Page(
            partition_key=SCOPE,
            title="Acme Q3",
            body="# Acme Q3\n\nRevenue rose.",
            tier="individual",
            owner_id="dev-user",
            source_refs=["individual/dev-user/x-acme.pdf"],
        ),
        change_type="ingest",
        author_id="dev-user",
    )

    raw = azure.blob.read_text(page.blob_path)
    assert raw.startswith("---\n")
    assert page.id in raw
    assert "individual/dev-user/x-acme.pdf" in raw

    # The runtime body must stay clean, or every diff and render would show it.
    assert ab.read_page(page.id, SCOPE).body == "# Acme Q3\n\nRevenue rose."


def test_embedding_sees_the_clean_body(azure):
    """If frontmatter leaked into the embedded text, every stored vector would
    shift and retrieval quality would silently change."""
    ab.write_page(
        Page(
            partition_key=SCOPE,
            title="Acme Q3",
            body="Revenue rose.",
            tier="individual",
            owner_id="dev-user",
        ),
        change_type="ingest",
        author_id="dev-user",
    )
    embedded = [c["text"] for c in azure.model.calls if c["kind"] == "embed"]
    assert embedded == ["Acme Q3\n\nRevenue rose."]


def test_body_sha256_tracks_the_clean_body(azure):
    page = ab.write_page(
        Page(
            partition_key=SCOPE,
            title="Acme Q3",
            body="Revenue rose.",
            tier="individual",
            owner_id="dev-user",
        ),
        change_type="ingest",
        author_id="dev-user",
    )
    assert page.body_sha256 == body_hash("Revenue rose.")
