"""The team tier's substrate rule, stated in the build spec as "append-only,
no exceptions" and "enforced here, not by prompt". It is the invariant the
whole team-tier design rests on: attribution, the derived-view layer, and
conflict flagging all assume existing text is never rewritten.
"""

from app import abstractions as ab
from app import wiki
from app.models import User, make_partition_key

TEAM = "dev-team"
SCOPE = make_partition_key("team", TEAM)
ALICE = User(id="alice", name="Alice", teams={TEAM: "Dev Team"})
BOB = User(id="bob", name="Bob", teams={TEAM: "Dev Team"})


def test_first_append_creates_the_page(azure):
    page = wiki.append_to_team_page(title="Acme", content="First entry.", team_id=TEAM, user=ALICE)
    assert "First entry." in page.body
    assert page.tier == "team"


def test_second_append_preserves_the_first(azure):
    wiki.append_to_team_page(title="Acme", content="First entry.", team_id=TEAM, user=ALICE)
    page = wiki.append_to_team_page(title="Acme", content="Second entry.", team_id=TEAM, user=BOB)

    assert "First entry." in page.body
    assert "Second entry." in page.body
    assert page.body.index("First entry.") < page.body.index("Second entry.")


def test_appends_are_dated_and_attributed(azure):
    page = wiki.append_to_team_page(title="Acme", content="A finding.", team_id=TEAM, user=ALICE)
    assert "Pushed by Alice" in page.body


def test_conflict_notes_are_written_into_the_content(azure):
    """Build spec: a flagged conflict is written into the appended content, not
    resolved automatically — future readers must see the tension."""
    page = wiki.append_to_team_page(
        title="Acme",
        content="Revenue was 4.2m.",
        team_id=TEAM,
        user=ALICE,
        conflict_note="The Q3 page states 3.9m.",
    )
    assert "The Q3 page states 3.9m." in page.body
    assert "Revenue was 4.2m." in page.body


def test_every_append_is_a_new_version(azure):
    wiki.append_to_team_page(title="Acme", content="One.", team_id=TEAM, user=ALICE)
    page = wiki.append_to_team_page(title="Acme", content="Two.", team_id=TEAM, user=BOB)

    versions = ab.list_versions(page.id, SCOPE)
    assert [v.version_number for v in versions] == [2, 1]
    assert all(v.change_type == "merge_append" for v in versions)


def test_earlier_snapshots_keep_only_what_existed_then(azure):
    """The version trail has to be honest: v1 must not retroactively contain
    text appended at v2."""
    wiki.append_to_team_page(title="Acme", content="First.", team_id=TEAM, user=ALICE)
    wiki.append_to_team_page(title="Acme", content="Second.", team_id=TEAM, user=BOB)

    v1 = ab.read_page_body(f"team/{TEAM}/acme/v1.md")
    assert "First." in v1
    assert "Second." not in v1


def test_changeset_apply_is_purely_additive(azure):
    wiki.append_to_team_page(title="Acme", content="Existing entry.", team_id=TEAM, user=ALICE)
    before = ab.find_page_by_title("Acme", SCOPE).body

    cs = wiki.build_changeset(
        tier="team",
        owner=TEAM,
        user=BOB,
        primary_title="Acme",
        primary_body="New entry.",
        log_source_type="push",
        log_source_ref="x",
        log_mode="manual",
        include_cross_page=False,
        include_index=False,
    )
    wiki.apply_changeset(cs, None, BOB.id)

    after = ab.find_page_by_title("Acme", SCOPE).body
    assert after.startswith(before)
    assert "New entry." in after


def test_source_refs_accumulate_rather_than_replace(azure):
    """Provenance is additive too — a page contributed to by two sources must
    trace back to both."""
    wiki.append_to_team_page(
        title="Acme", content="One.", team_id=TEAM, user=ALICE, source_ref_id="team/a.pdf"
    )
    page = wiki.append_to_team_page(
        title="Acme", content="Two.", team_id=TEAM, user=BOB, source_ref_id="team/b.pdf"
    )

    assert set(page.source_refs) == {"team/a.pdf", "team/b.pdf"}
