"""Route-level tests through a real TestClient.

Access control is the reason most of these exist: `_scope_or_403` is one
function every scoped route depends on, and the append-only rule is only as
strong as the routes that refuse to break it.
"""

from app import abstractions as ab
from app import wiki
from app.models import Page, User, make_partition_key

ALICE = "alice"
TEAM = "dev-team"


def _page(owner: str, title: str, body: str = "Body.", tier: str = "individual") -> Page:
    scope = make_partition_key(tier, owner)
    return ab.write_page(
        Page(partition_key=scope, title=title, body=body, tier=tier, owner_id=owner),
        change_type="ingest",
        author_id=owner,
    )


# ------------------------------------------------------------- access control


def test_own_wiki_is_reachable(client):
    c = client(ALICE)
    _page(ALICE, "Acme Q3")
    assert c.get(f"/individual/{ALICE}/").status_code == 200


def test_another_users_wiki_is_forbidden(client):
    """Individual wikis are private by default — no sharing, no cross-user
    visibility."""
    _page(ALICE, "Acme Q3")
    assert client("bob").get(f"/individual/{ALICE}/").status_code == 403


def test_another_users_page_is_forbidden(client):
    page = _page(ALICE, "Acme Q3")
    resp = client("bob").get(f"/individual/{ALICE}/page/{page.id}")
    assert resp.status_code == 403


def test_team_page_is_forbidden_to_non_members(client):
    _page(TEAM, "Team Notes", tier="team")
    outsider = client("carol", teams={"other-team": "Other Team"})
    assert outsider.get(f"/team/{TEAM}/").status_code == 403


def test_team_page_is_reachable_by_members(client):
    _page(TEAM, "Team Notes", tier="team")
    assert client(ALICE).get(f"/team/{TEAM}/").status_code == 200


def test_unknown_tier_is_404(client):
    assert client(ALICE).get("/nonsense/x/").status_code == 404


# ------------------------------------------------------------- append-only


def test_team_pages_cannot_be_edited_directly(client):
    """The append-only substrate has no exceptions; the edit route must not
    become the hole in it."""
    page = _page(TEAM, "Team Notes", tier="team")
    c = client(ALICE)

    assert c.get(f"/team/{TEAM}/page/{page.id}/edit").status_code == 403
    posted = c.post(f"/team/{TEAM}/page/{page.id}/edit", data={"body": "rewritten"})
    assert posted.status_code == 403


def test_team_page_body_is_unchanged_after_a_rejected_edit(client):
    page = _page(TEAM, "Team Notes", body="Original.", tier="team")
    client(ALICE).post(f"/team/{TEAM}/page/{page.id}/edit", data={"body": "rewritten"})

    scope = make_partition_key("team", TEAM)
    assert ab.read_page(page.id, scope).body == "Original."


# -------------------------------------------------------------- manual edit


def test_individual_page_can_be_edited(client):
    page = _page(ALICE, "Notes", body="Version one.")
    c = client(ALICE)

    resp = c.post(
        f"/individual/{ALICE}/page/{page.id}/edit",
        data={"body": "Version two."},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    scope = make_partition_key("individual", ALICE)
    assert ab.read_page(page.id, scope).body == "Version two."


def test_editing_bumps_the_version_and_keeps_the_old_snapshot(azure, client):
    page = _page(ALICE, "Notes", body="Version one.")
    client(ALICE).post(f"/individual/{ALICE}/page/{page.id}/edit", data={"body": "Version two."})

    scope = make_partition_key("individual", ALICE)
    assert ab.read_page(page.id, scope).version == 2
    assert "Version one." in azure.blob.read_text(f"individual/{ALICE}/notes/v1.md")


def test_editing_another_users_page_is_forbidden(client):
    page = _page(ALICE, "Notes")
    resp = client("bob").post(f"/individual/{ALICE}/page/{page.id}/edit", data={"body": "hijacked"})
    assert resp.status_code == 403


def test_edit_records_the_manual_edit_change_type(client):
    page = _page(ALICE, "Notes", body="One.")
    client(ALICE).post(f"/individual/{ALICE}/page/{page.id}/edit", data={"body": "Two."})

    scope = make_partition_key("individual", ALICE)
    assert ab.list_versions(page.id, scope)[0].change_type == "manual_edit"


# ------------------------------------------------------------- history view


def test_history_shows_each_version_with_its_diff(client):
    page = _page(ALICE, "Notes", body="First line.")
    c = client(ALICE)
    c.post(f"/individual/{ALICE}/page/{page.id}/edit", data={"body": "First line.\nSecond line."})

    resp = c.get(f"/individual/{ALICE}/page/{page.id}/history")

    assert resp.status_code == 200
    assert "v2" in resp.text
    assert "Second line." in resp.text  # the diff body, not just a version number


def test_history_is_newest_first(client):
    page = _page(ALICE, "Notes", body="One.")
    c = client(ALICE)
    c.post(f"/individual/{ALICE}/page/{page.id}/edit", data={"body": "Two."})

    scope = make_partition_key("individual", ALICE)
    entries = wiki.page_history(page.id, scope)
    assert [e.version_number for e in entries] == [2, 1]


def test_history_reports_the_triggering_source(azure, client):
    """ "Where did this version come from" is the provenance half of the view."""
    scope = make_partition_key("individual", ALICE)
    user = User(id=ALICE, name="Alice")
    wiki.upsert_individual_page(
        title="Notes",
        body="From a source.",
        user=user,
        change_type="ingest",
        source_ref_id="individual/alice/abc-report.pdf",
    )
    page = ab.find_page_by_title("Notes", scope)

    entries = wiki.page_history(page.id, scope)
    assert entries[0].source_ref == "individual/alice/abc-report.pdf"


def test_history_of_another_users_page_is_forbidden(client):
    page = _page(ALICE, "Notes")
    resp = client("bob").get(f"/individual/{ALICE}/page/{page.id}/history")
    assert resp.status_code == 403


# ------------------------------------------------------------------ reindex


def test_reindex_route_is_scoped(client):
    assert client("bob").post(f"/individual/{ALICE}/reindex").status_code == 403


def test_reindex_route_rebuilds_the_index(azure, client):
    page = _page(ALICE, "Acme Q3")
    azure.cosmos.wipe()

    client(ALICE).post(f"/individual/{ALICE}/reindex", follow_redirects=False)

    scope = make_partition_key("individual", ALICE)
    assert ab.read_page(page.id, scope) is not None
