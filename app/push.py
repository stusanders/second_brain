"""Push / merge mechanism (individual -> team) with the two-stage conflict
check. Nothing is written until the pusher confirms the changeset review.

Placement (append vs. new page) and conflict detection stay push-specific;
the resulting proposal is handed to wiki.build_changeset so push shares the
same multi-page changeset review component as ingest and query-save (build
spec: "Diff/review UI ... used for manual-mode ingest approval, push-to-team
confirmation, query-derived page saves"). Cross-page updates are switched
off here (`include_cross_page=False`): push already ran its own placement
step to pick one target page, and the build spec's breadth requirement
("10-15 pages is normal for one source") is stated for ingest, not push —
this is a deliberate narrowing, not an oversight.
"""

from app import abstractions as ab
from app import wiki
from app.models import IngestLogEntry, Page, User, make_partition_key

PLACEMENT_SYSTEM = (
    "You place incoming content into a team wiki. Given the pushed page and a "
    "list of candidate team page titles, answer with EXACTLY one line: either "
    "'APPEND: <existing title>' or 'NEW: <proposed new title>'. Nothing else."
)

CONFLICT_SYSTEM = (
    "You check whether pushed content factually conflicts with an existing team "
    "wiki page (contradicting statements, incompatible figures, superseded "
    "decisions). Answer with EXACTLY one line: 'NO_CONFLICT' or "
    "'CONFLICT: <one-sentence description>'. Nothing else."
)


def prepare_push(page: Page, team_id: str, user: User) -> wiki.Changeset:
    scope = make_partition_key("team", team_id)

    # Stage 0: LLM decides placement — append to a related page or create new.
    hits = ab.search(f"{page.title}\n{page.body[:2000]}", scope, top_k=5)
    candidates = [h["title"] for h in hits if h["title"] != wiki.INDEX_TITLE]
    if candidates:
        answer = ab.call_model(
            f"Pushed page title: {page.title}\n\nPushed content:\n{page.body[:4000]}\n\n"
            f"Candidate team pages: {', '.join(candidates)}",
            system=PLACEMENT_SYSTEM,
            max_tokens=50,
        ).strip()
        if answer.upper().startswith("APPEND:"):
            target_title = answer.split(":", 1)[1].strip()
        else:
            target_title = answer.split(":", 1)[-1].strip() or page.title
    else:
        target_title = page.title

    # Two-stage conflict check: vector shortlist (cheap) -> LLM pass per
    # shortlisted page (expensive, scoped down). Never O(n^2).
    conflicts: list[str] = []
    for hit in hits:
        team_page = ab.read_page(hit["page_id"], scope)
        if not team_page or team_page.title == wiki.INDEX_TITLE:
            continue
        verdict = ab.call_model(
            f"Existing team page '{team_page.title}':\n{team_page.body[:6000]}\n\n"
            f"Pushed content:\n{page.body[:6000]}",
            system=CONFLICT_SYSTEM,
            max_tokens=80,
        ).strip()
        if verdict.upper().startswith("CONFLICT"):
            desc = verdict.split(":", 1)[-1].strip()
            conflicts.append(
                f"this may conflict with an earlier statement on [[{team_page.title}]] "
                f"(updated {team_page.updated_at[:10]}): {desc}"
            )

    note = "; ".join(conflicts) if conflicts else None
    changeset = wiki.build_changeset(
        tier="team",
        owner=team_id,
        user=user,
        primary_title=target_title,
        primary_body=page.body,
        log_source_type="push",
        log_source_ref=f"individual page: {page.title}",
        log_mode="manual",
        conflict_note=note,
        include_cross_page=False,
    )
    changeset.conflicts = conflicts
    return changeset


def get_changeset(changeset_id: str, user: User) -> wiki.Changeset | None:
    return wiki.get_changeset(changeset_id, user)


def confirm_push(changeset: wiki.Changeset, user: User, selected: set[int] | None) -> list[Page]:
    """Pusher confirmed (go decision on any flagged conflicts). Conflict notes
    were already written INTO the appended content at build time, so future
    readers see the tension regardless of which items end up selected."""
    scope = make_partition_key("team", changeset.owner)
    pages = wiki.apply_changeset(changeset, selected, user.id)
    if pages:
        log = IngestLogEntry(
            partition_key=scope,
            source_type=changeset.log_source_type,
            source_ref=changeset.log_source_ref,
            mode="manual",
            tier="team",
            pages_affected=[p.id for p in pages],
        )
        ab.append_ingest_log(log)
    return pages


def discard_changeset(changeset_id: str) -> None:
    wiki.discard_changeset(changeset_id)
