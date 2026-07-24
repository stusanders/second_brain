"""Pending review state (manual sessions, changesets awaiting approval) must
outlive the process holding it. Container Apps scales to zero and runs
multiple replicas, so "still in the dict" is not a durability story.

The fakes here stand in for a restart: nothing in these tests keeps a Python
reference to the original object, so a passing assertion means the state came
back from blob.
"""

from app import review_store, wiki
from app.ingest import pipeline
from app.ingest.extractors import ExtractedSource
from app.models import User

ALICE = User(id="alice", name="Alice")
BOB = User(id="bob", name="Bob")


def _source(text: str = "Some source text.") -> ExtractedSource:
    return ExtractedSource(
        source_type="text", source_ref="note.txt", text=text, raw_bytes=b"raw", filename="note.txt"
    )


# ------------------------------------------------------------------ changesets


def _changeset(user: User) -> wiki.Changeset:
    return wiki.build_changeset(
        tier="individual",
        owner=user.id,
        user=user,
        primary_title="Acme Q3",
        primary_body="Revenue rose.",
        log_source_type="text",
        log_source_ref="note.txt",
        log_mode="manual",
        include_cross_page=False,
        include_index=False,
    )


def test_changeset_survives_a_restart(azure):
    cs_id = _changeset(ALICE).id

    restored = wiki.get_changeset(cs_id, ALICE)

    assert restored is not None
    assert restored.items[0].title == "Acme Q3"
    assert restored.items[0].new_body == "Revenue rose."


def test_changeset_is_not_readable_by_another_user(azure):
    cs_id = _changeset(ALICE).id
    assert wiki.get_changeset(cs_id, BOB) is None


def test_changeset_is_gone_after_apply(azure):
    cs = _changeset(ALICE)
    wiki.apply_changeset(cs, None, ALICE.id)
    assert wiki.get_changeset(cs.id, ALICE) is None


def test_changeset_is_gone_after_discard(azure):
    cs = _changeset(ALICE)
    wiki.discard_changeset(cs.id, ALICE.id)
    assert wiki.get_changeset(cs.id, ALICE) is None


def test_append_block_survives_the_round_trip(azure):
    """The team-tier concurrency fix depends on this field surviving
    serialization — without it apply_changeset silently falls back to writing
    the stale captured body."""
    cs = wiki.build_changeset(
        tier="team",
        owner="dev-team",
        user=ALICE,
        primary_title="Acme",
        primary_body="A finding.",
        log_source_type="push",
        log_source_ref="x",
        log_mode="manual",
        include_cross_page=False,
        include_index=False,
    )
    restored = wiki.get_changeset(cs.id, ALICE)
    assert restored.items[0].append_block is not None
    assert "A finding." in restored.items[0].append_block


# -------------------------------------------------------------------- sessions


def test_manual_session_survives_a_restart(azure):
    session_id = pipeline.start_manual_session(_source(), ALICE).id

    restored = pipeline.get_session(session_id, ALICE)

    assert restored is not None
    assert restored.source.source_ref == "note.txt"
    assert len(restored.messages) == 3  # system, opening user turn, assistant reply


def test_session_is_not_readable_by_another_user(azure):
    session_id = pipeline.start_manual_session(_source(), ALICE).id
    assert pipeline.get_session(session_id, BOB) is None


def test_discussion_turns_persist(azure):
    session = pipeline.start_manual_session(_source(), ALICE)
    pipeline.discuss(session, "What about the revenue figure?")

    restored = pipeline.get_session(session.id, ALICE)
    assert any(m["content"] == "What about the revenue figure?" for m in restored.messages)


def test_raw_source_is_stored_immutably_at_session_start(azure):
    """Provenance is a hard requirement, and a dropped source is worth keeping
    even if the discussion is later abandoned."""
    session = pipeline.start_manual_session(_source(), ALICE)

    assert session.source_ref_id in azure.blob.sources
    assert azure.blob.sources[session.source_ref_id] == b"raw"


def test_session_blob_does_not_carry_raw_bytes(azure):
    """Round-tripping the original file through the session blob on every
    discussion turn would be pure waste — it's already in the source store."""
    session = pipeline.start_manual_session(_source(), ALICE)
    raw = azure.blob.read_text(f"individual/alice/_sessions/{session.id}.json")
    assert "raw_bytes" not in raw


def test_discarding_a_session_removes_its_changeset(azure):
    session = pipeline.start_manual_session(_source(), ALICE)
    pipeline.propose_page(session, ALICE)
    changeset_id = pipeline.get_session(session.id, ALICE).changeset_id

    pipeline.discard(pipeline.get_session(session.id, ALICE), ALICE)

    assert pipeline.get_session(session.id, ALICE) is None
    assert wiki.get_changeset(changeset_id, ALICE) is None


def test_expired_state_is_dropped(azure, monkeypatch):
    cs_id = _changeset(ALICE).id
    monkeypatch.setattr(review_store, "MAX_AGE_SECONDS", -1)
    assert wiki.get_changeset(cs_id, ALICE) is None


def test_review_blobs_are_not_indexed_as_pages(azure):
    """Review state lives under "_"-prefixed paths, so it must never surface
    as wiki content."""
    pipeline.start_manual_session(_source(), ALICE)
    _changeset(ALICE)

    assert azure.blob.list_page_paths("individual", "alice") == []
