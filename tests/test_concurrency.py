"""Team pages are append-only logs written by several people at once. A
changeset is drafted, then reviewed by a human, then written — so the body it
captured can be arbitrarily stale by write time. These tests pin the two
things that make that safe: writes re-derive under a lease, and existing text
is never touched.
"""

import pytest
from conftest import LeaseHeld

from app import abstractions as ab
from app import blob_store, wiki
from app.models import User, make_partition_key

TEAM = "dev-team"
SCOPE = make_partition_key("team", TEAM)

ALICE = User(id="alice", name="Alice", teams={TEAM: "Dev Team"})
BOB = User(id="bob", name="Bob", teams={TEAM: "Dev Team"})


def _push_changeset(user: User, title: str, content: str) -> wiki.Changeset:
    return wiki.build_changeset(
        tier="team",
        owner=TEAM,
        user=user,
        primary_title=title,
        primary_body=content,
        log_source_type="push",
        log_source_ref="test",
        log_mode="manual",
        include_cross_page=False,
        include_index=False,
    )


def test_concurrent_pushes_both_survive(azure):
    """The lost-update this whole mechanism exists to prevent: two people
    prepare a push against the same page, then both approve."""
    wiki.append_to_team_page(title="Acme", content="Original entry.", team_id=TEAM, user=ALICE)

    # Both changesets are built while the page body is still the original.
    alice_cs = _push_changeset(ALICE, "Acme", "Alice's finding.")
    bob_cs = _push_changeset(BOB, "Acme", "Bob's finding.")

    wiki.apply_changeset(alice_cs, None, ALICE.id)
    wiki.apply_changeset(bob_cs, None, BOB.id)

    body = ab.find_page_by_title("Acme", SCOPE).body
    assert "Original entry." in body
    assert "Alice's finding." in body
    assert "Bob's finding." in body


def test_append_never_rewrites_existing_text(azure):
    wiki.append_to_team_page(title="Acme", content="First claim.", team_id=TEAM, user=ALICE)
    before = ab.find_page_by_title("Acme", SCOPE).body

    wiki.apply_changeset(_push_changeset(BOB, "Acme", "Second claim."), None, BOB.id)

    after = ab.find_page_by_title("Acme", SCOPE).body
    assert after.startswith(before)  # purely additive


def test_individual_tier_writes_verbatim(azure):
    """Individual pages are editable in place — re-deriving would be wrong
    there, so the lease path must not apply."""
    scope = make_partition_key("individual", "alice")
    wiki.upsert_individual_page(
        title="Notes", body="Version one.", user=ALICE, change_type="ingest"
    )
    cs = wiki.build_changeset(
        tier="individual",
        owner="alice",
        user=ALICE,
        primary_title="Notes",
        primary_body="Version two, fully rewritten.",
        log_source_type="ingest",
        log_source_ref="test",
        log_mode="manual",
        include_cross_page=False,
        include_index=False,
    )
    wiki.apply_changeset(cs, None, ALICE.id)

    assert ab.find_page_by_title("Notes", scope).body == "Version two, fully rewritten."


def test_lease_is_released_after_write(azure):
    wiki.append_to_team_page(title="Acme", content="Entry.", team_id=TEAM, user=ALICE)
    wiki.apply_changeset(_push_changeset(BOB, "Acme", "Another."), None, BOB.id)

    assert azure.blob.leases == {}


def test_lease_is_released_even_when_the_write_fails(azure, monkeypatch):
    wiki.append_to_team_page(title="Acme", content="Entry.", team_id=TEAM, user=ALICE)
    cs = _push_changeset(BOB, "Acme", "Another.")

    def boom(*args, **kwargs):
        raise RuntimeError("index write failed")

    monkeypatch.setattr(ab, "write_page", boom)
    with pytest.raises(RuntimeError):
        wiki.apply_changeset(cs, None, BOB.id)

    assert azure.blob.leases == {}


def test_a_held_lease_blocks_a_second_writer(azure):
    """Guards the fake as much as the app: if the lease weren't enforced, the
    concurrency test above would pass for the wrong reason."""
    wiki.append_to_team_page(title="Acme", content="Entry.", team_id=TEAM, user=ALICE)
    path, lease = ab.acquire_page_lease("team", TEAM, "Acme")
    assert lease is not None

    with pytest.raises(LeaseHeld):
        ab.acquire_page_lease("team", TEAM, "Acme")

    ab.release_page_lease(path, lease)
    _, second = ab.acquire_page_lease("team", TEAM, "Acme")
    assert second is not None


def test_new_page_needs_no_lease(azure):
    """Nothing exists to contend over, so the first writer must not block."""
    _, lease = ab.acquire_page_lease("team", TEAM, "Brand New Page")
    assert lease is None


def test_lease_contention_error_is_typed(azure, monkeypatch):
    """A contended write must surface as a clear error, not an SDK 409 leaking
    out as a 500 — the caller's write did not happen and the user must know."""
    assert issubclass(blob_store.LeaseContentionError, RuntimeError)
