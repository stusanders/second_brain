"""Push / merge mechanism (individual -> team) with the two-stage conflict
check. Nothing is written until the pusher confirms the preview."""

import uuid
from dataclasses import dataclass, field

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


@dataclass
class PushPreview:
    id: str
    user_id: str
    team_id: str
    source_page: Page
    target_title: str
    is_new_page: bool
    conflicts: list[str] = field(default_factory=list)
    diff: str = ""


_previews: dict[str, PushPreview] = {}


def prepare_push(page: Page, team_id: str, user: User) -> PushPreview:
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
            target_title, is_new = answer.split(":", 1)[1].strip(), False
        else:
            target_title, is_new = answer.split(":", 1)[-1].strip() or page.title, True
    else:
        target_title, is_new = page.title, True

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

    existing = None if is_new else ab.find_page_by_title(target_title, scope)
    preview = PushPreview(
        id=uuid.uuid4().hex,
        user_id=user.id,
        team_id=team_id,
        source_page=page,
        target_title=target_title,
        is_new_page=existing is None,
        conflicts=conflicts,
    )
    appended = (existing.body if existing else "") + f"\n\n---\n\n### Pushed by {user.name}\n\n"
    if conflicts:
        appended += "".join(f"> **Note:** {c}\n" for c in conflicts) + "\n"
    appended += page.body
    preview.diff = wiki.unified_diff(existing.body if existing else "", appended, target_title)
    _previews[preview.id] = preview
    return preview


def get_preview(preview_id: str, user: User) -> PushPreview | None:
    p = _previews.get(preview_id)
    return p if p and p.user_id == user.id else None


def confirm_push(preview: PushPreview, user: User) -> Page:
    """Pusher confirmed (go decision on any flagged conflicts). Conflict notes
    are written INTO the appended content so future readers see the tension."""
    scope = make_partition_key("team", preview.team_id)
    log = IngestLogEntry(
        partition_key=scope,
        source_type="push",
        source_ref=f"individual page: {preview.source_page.title}",
        mode="manual",
        tier="team",
    )
    note = "; ".join(preview.conflicts) if preview.conflicts else None
    page = wiki.append_to_team_page(
        title=preview.target_title,
        content=preview.source_page.body,
        team_id=preview.team_id,
        user=user,
        conflict_note=note,
        source_ref_id=log.id,
    )
    log.pages_affected = [page.id]
    ab.append_ingest_log(log)
    wiki.regenerate_index(scope, "team", preview.team_id, user.id)
    _previews.pop(preview.id, None)
    return page


def discard_preview(preview_id: str) -> None:
    _previews.pop(preview_id, None)
