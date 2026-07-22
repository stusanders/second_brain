"""Lint operation (build spec: peer to Ingest and Query, not optional).

A periodic health-check pass over one scope's wiki. Findings split into two
kinds, per spec:
  - mechanical: missing cross-references, stub pages for repeatedly-
    mentioned-but-missing concepts, orphan-page flags, index regeneration.
    Additive/reversible, so automatic lint mode applies these with no
    review gate; manual lint mode (default) queues them instead.
  - judgment: contradictions between pages, staleness calls, data gaps.
    Always queued for manual review regardless of lint mode — getting these
    wrong writes a confident falsehood future syntheses build on.

Team-tier constraint: automatic lint may only regenerate derived views and
add cross-references, never touch the append-only log. With
`app.derived_views` now built, automatic team-tier lint resolves
`missing_xref` findings by regenerating that page's derived view (the
synthesis step adds the [[wikilink]] there, never in the log itself) and
also uses each automatic pass as the "regenerated on a schedule" cadence
the build spec calls for (bounded — see MAX_DERIVED_VIEW_PAGES — since this
POC has no background scheduler; a lint pass is the closest thing to one).
`orphan` and `stub_candidate` still have no team-tier fix — creating a new
page or resolving an orphan isn't "regenerate a derived view or add a
cross-reference", so those stay queued for manual review on team tier
regardless of lint mode, same as before.

Findings persist as a JSON blob per scope (not a new Cosmos container —
adding one would require another by-hand Azure Portal step per
docs/AZURE_SETUP_GUIDE.md's Cosmos RBAC limitation). blob_store already
excludes "_"-prefixed blob names from the page index (see list_page_paths),
so this queue is never mistaken for wiki content.
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Literal

from app import abstractions as ab
from app import blob_store, derived_views, schema, wiki
from app.models import User, make_partition_key, new_id, now_iso

FindingCategory = Literal["mechanical", "judgment"]
CheckType = Literal[
    "orphan", "missing_xref", "stub_candidate", "contradiction", "stale", "data_gap"
]
FindingStatus = Literal["pending", "applied", "dismissed"]
LintMode = Literal["manual", "automatic"]

MECHANICAL_CHECKS: frozenset[CheckType] = frozenset({"orphan", "missing_xref", "stub_candidate"})

# Cost discipline: judgment checks call the model, so bound how much of the
# wiki they scan per run rather than growing unbounded with wiki size.
MAX_JUDGMENT_PAGES = 15
MAX_STRUCTURAL_PAGES = 200
# Team-tier automatic lint doubles as the derived-view regen "schedule" (no
# background scheduler in this POC) — bound per run for the same reason.
MAX_DERIVED_VIEW_PAGES = 15

CONFLICT_STALE_SYSTEM = (
    "You check two pages from the same knowledge wiki for problems. Answer with "
    "EXACTLY one line: 'OK' if neither issue applies, 'CONTRADICTION: <one-sentence "
    "description>' if they state incompatible facts, or 'STALE: <one sentence naming "
    "which page's claim is superseded by the other, more recent, page>'. Nothing else."
)

DATA_GAP_SYSTEM = (
    "Given a knowledge wiki's page titles and its schema/conventions doc, suggest up "
    "to 3 concrete data gaps worth filling: topics clearly relevant to this wiki that "
    "are missing or under-covered. One per line, format '- <gap>: <a question to "
    "investigate or source to look for>'. If there are no clear gaps, output nothing."
)


@dataclass
class LintFinding:
    id: str = field(default_factory=new_id)
    category: FindingCategory = "mechanical"
    check_type: CheckType = "orphan"
    summary: str = ""
    detail: str = ""
    page_ids: list[str] = field(default_factory=list)
    origin_mode: Literal["manual", "automatic", "n/a"] = "n/a"
    status: FindingStatus = "pending"
    created_at: str = field(default_factory=now_iso)


# ------------------------------------------------------------- persistence


def _queue_path(tier: str, owner: str) -> str:
    return f"{tier}/{owner}/_lint/queue.json"


def _load(tier: str, owner: str) -> dict:
    raw = blob_store.read_text(_queue_path(tier, owner))
    if raw is None:
        return {"lint_mode": "manual", "findings": []}
    return json.loads(raw)


def _save(tier: str, owner: str, data: dict) -> None:
    blob_store.write_text(_queue_path(tier, owner), json.dumps(data, indent=2))


def get_lint_mode(tier: str, owner: str) -> LintMode:
    return _load(tier, owner)["lint_mode"]


def set_lint_mode(tier: str, owner: str, mode: LintMode) -> None:
    data = _load(tier, owner)
    data["lint_mode"] = mode
    _save(tier, owner, data)


def list_findings(
    tier: str,
    owner: str,
    status: FindingStatus | None = None,
    origin_mode: str | None = None,
) -> list[LintFinding]:
    """Automatic-ingest-derived findings sort first by default (build spec:
    the queue that's most prone to silently overflowing gets surfaced
    first), newest first within each group. `origin_mode` filters to just
    one group ("automatic" / "manual" / "n/a")."""
    findings = [LintFinding(**f) for f in _load(tier, owner)["findings"]]
    if status:
        findings = [f for f in findings if f.status == status]
    if origin_mode:
        findings = [f for f in findings if f.origin_mode == origin_mode]
    origin_rank = {"automatic": 0, "manual": 1, "n/a": 2}
    # Two stable sorts (newest-first within group, then group order) beat a
    # single tuple key since the two fields sort in opposite directions.
    findings.sort(key=lambda f: f.created_at, reverse=True)
    findings.sort(key=lambda f: origin_rank[f.origin_mode])
    return findings


def get_finding(tier: str, owner: str, finding_id: str) -> LintFinding | None:
    for f in list_findings(tier, owner):
        if f.id == finding_id:
            return f
    return None


def _append_findings(tier: str, owner: str, findings: list[LintFinding]) -> None:
    if not findings:
        return
    data = _load(tier, owner)
    data["findings"].extend(asdict(f) for f in findings)
    _save(tier, owner, data)


def _set_status(tier: str, owner: str, finding_id: str, status: FindingStatus) -> None:
    data = _load(tier, owner)
    for f in data["findings"]:
        if f["id"] == finding_id:
            f["status"] = status
            break
    _save(tier, owner, data)


def unreviewed_counts(tier: str, owner: str) -> dict[str, int]:
    """Pending findings broken out by the ingest mode that produced the
    content they flag ("automatic" / "manual" / "n/a" for structural
    findings not tied to one ingest) — the visibility mechanism the build
    spec calls for so the queue's failure mode (going unnoticed) can't hide."""
    counts = {"automatic": 0, "manual": 0, "n/a": 0, "total": 0}
    for f in list_findings(tier, owner, status="pending"):
        counts[f.origin_mode] += 1
        counts["total"] += 1
    return counts


def is_automatic_ingest_allowed(tier: str, owner: str) -> bool:
    """Threshold-triggered downgrade: once too many automatic-ingest-derived
    findings sit unreviewed, automatic ingest is disabled until the queue is
    worked down. Skipped while automatic lint mode is on, since mechanical
    findings clear themselves there and nothing accumulates."""
    if get_lint_mode(tier, owner) == "automatic":
        return True
    threshold = schema.automatic_lint_queue_threshold(tier, owner)
    return unreviewed_counts(tier, owner)["automatic"] < threshold


def _origin_mode_for_pages(
    scope: str, page_ids: list[str]
) -> Literal["manual", "automatic", "n/a"]:
    """Best-effort: most recent ingest-log entry touching any of these pages."""
    for entry in ab.list_ingest_log(scope, limit=200):
        if any(pid in entry.pages_affected for pid in page_ids):
            return entry.mode
    return "n/a"


# ------------------------------------------------------- mechanical checks


def _find_orphans(scope: str) -> list[LintFinding]:
    pages = [p for p in ab.list_pages(scope) if p.title != wiki.INDEX_TITLE]
    findings = []
    for p in pages:
        if not wiki.backlinks(p.id, scope):
            findings.append(
                LintFinding(
                    category="mechanical",
                    check_type="orphan",
                    summary=f"'{p.title}' has no inbound links",
                    detail=f"No other page links to '{p.title}'. Consider linking it from a "
                    "related page or folding it into one.",
                    page_ids=[p.id],
                    origin_mode=_origin_mode_for_pages(scope, [p.id]),
                )
            )
    return findings


def _find_missing_xrefs_and_stubs(scope: str) -> tuple[list[LintFinding], list[LintFinding]]:
    """One pass over page bodies: literal-title mentions not yet wikilinked
    (missing_xref), and [[wikilinks]] pointing at pages that don't exist yet,
    mentioned from 2+ distinct pages (stub_candidate)."""
    pages = [p for p in ab.list_pages(scope) if p.title != wiki.INDEX_TITLE][:MAX_STRUCTURAL_PAGES]
    bodies = {p.id: (ab.read_page(p.id, scope) or p).body for p in pages}

    xrefs: list[LintFinding] = []
    for p in pages:
        body = bodies[p.id]
        for other in pages:
            if other.id == p.id or other.title not in body:
                continue
            if f"[[{other.title}]]" in body:
                continue
            xrefs.append(
                LintFinding(
                    category="mechanical",
                    check_type="missing_xref",
                    summary=f"'{p.title}' mentions '{other.title}' without linking it",
                    detail=f"'{other.title}' appears as plain text in '{p.title}'. "
                    "Proposed fix: wrap the first mention as a [[wikilink]].",
                    page_ids=[p.id, other.id],
                    origin_mode=_origin_mode_for_pages(scope, [p.id]),
                )
            )

    mention_counts: dict[str, set[str]] = {}
    for p in pages:
        for title in wiki.WIKILINK_RE.findall(bodies[p.id]):
            title = title.strip()
            if ab.find_page_by_title(title, scope) is None:
                mention_counts.setdefault(title, set()).add(p.id)

    stubs = [
        LintFinding(
            category="mechanical",
            check_type="stub_candidate",
            summary=f"'{title}' is mentioned but has no page",
            detail=f"Referenced as [[{title}]] from {len(mentioning)} pages but no page "
            "with that title exists. Proposed fix: create a stub page.",
            page_ids=sorted(mentioning),
            origin_mode=_origin_mode_for_pages(scope, sorted(mentioning)),
        )
        for title, mentioning in mention_counts.items()
        if len(mentioning) >= 2
    ]
    return xrefs, stubs


# --------------------------------------------------------- judgment checks


def _find_contradictions_and_staleness(scope: str, schema_ctx: str) -> list[LintFinding]:
    """Two-stage, same pattern as push.py's conflict check: vector shortlist
    (cheap) narrows candidates, LLM reasoning pass (expensive) only runs
    against that shortlist — never O(n^2) over the whole wiki."""
    pages = [p for p in ab.list_pages(scope) if p.title != wiki.INDEX_TITLE][:MAX_JUDGMENT_PAGES]
    findings: list[LintFinding] = []
    checked_pairs: set[frozenset[str]] = set()
    for p in pages:
        full = ab.read_page(p.id, scope)
        if not full:
            continue
        hits = ab.search(f"{full.title}\n{full.body[:2000]}", scope, top_k=3)
        for hit in hits:
            if hit["page_id"] == p.id:
                continue
            pair = frozenset({p.id, hit["page_id"]})
            if pair in checked_pairs:
                continue
            checked_pairs.add(pair)
            other = ab.read_page(hit["page_id"], scope)
            if not other or other.title == wiki.INDEX_TITLE:
                continue
            verdict = ab.call_model(
                f"Page A ('{full.title}', updated {full.updated_at[:10]}):\n{full.body[:4000]}\n\n"
                f"Page B ('{other.title}', updated {other.updated_at[:10]}):\n{other.body[:4000]}",
                system=(schema_ctx + "\n\n" if schema_ctx else "") + CONFLICT_STALE_SYSTEM,
                max_tokens=100,
            ).strip()
            if verdict.upper().startswith("CONTRADICTION"):
                findings.append(
                    LintFinding(
                        category="judgment",
                        check_type="contradiction",
                        summary=f"Possible contradiction: '{full.title}' vs '{other.title}'",
                        detail=verdict.split(":", 1)[-1].strip(),
                        page_ids=[p.id, other.id],
                        origin_mode=_origin_mode_for_pages(scope, [p.id, other.id]),
                    )
                )
            elif verdict.upper().startswith("STALE"):
                findings.append(
                    LintFinding(
                        category="judgment",
                        check_type="stale",
                        summary=f"Possible stale claim: '{full.title}' / '{other.title}'",
                        detail=verdict.split(":", 1)[-1].strip(),
                        page_ids=[p.id, other.id],
                        origin_mode=_origin_mode_for_pages(scope, [p.id, other.id]),
                    )
                )
    return findings


def _find_data_gaps(scope: str, schema_ctx: str) -> list[LintFinding]:
    pages = [p for p in ab.list_pages(scope) if p.title != wiki.INDEX_TITLE]
    if not pages:
        return []
    titles = "\n".join(f"- {p.title}" for p in pages)
    reply = ab.call_model(
        titles,
        system=(schema_ctx + "\n\n" if schema_ctx else "") + DATA_GAP_SYSTEM,
        max_tokens=400,
    ).strip()
    findings = []
    for line in reply.splitlines():
        line = line.strip().lstrip("- ").strip()
        if line:
            findings.append(
                LintFinding(
                    category="judgment",
                    check_type="data_gap",
                    summary=line[:100],
                    detail=line,
                    origin_mode="n/a",
                )
            )
    return findings


# ---------------------------------------------------------- apply / run


def _apply_mechanical(scope: str, tier: str, owner: str, f: LintFinding, author_id: str) -> None:
    """Perform the concrete fix a mechanical finding proposes.

    Team tier: only `missing_xref` has a fix path, and it never touches the
    append log — it regenerates the mentioning page's derived view instead
    (the synthesis step adds the [[wikilink]] there). `orphan` and
    `stub_candidate` have no team-tier fix (see module docstring) and are
    no-ops here."""
    if tier == "team":
        if f.check_type == "missing_xref" and f.page_ids:
            page = ab.read_page(f.page_ids[0], scope)
            if page:
                derived_views.regenerate_derived_view(page, schema.context_block(tier, owner))
        return
    if f.check_type == "missing_xref" and len(f.page_ids) == 2:
        page = ab.read_page(f.page_ids[0], scope)
        other = ab.read_page(f.page_ids[1], scope)
        if page and other and f"[[{other.title}]]" not in page.body:
            new_body = page.body.replace(other.title, f"[[{other.title}]]", 1)
            wiki.upsert_individual_page(
                title=page.title,
                body=new_body,
                user=User(id=author_id, name="Lint"),
                change_type="derived_view_regen",
            )
    elif f.check_type == "stub_candidate":
        title = f.summary.split("'")[1]
        mentioning = [ab.read_page(pid, scope) for pid in f.page_ids]
        mentions = ", ".join(f"[[{m.title}]]" for m in mentioning if m)
        stub_body = f"_Stub — mentioned from: {mentions}. Expand this page._"
        wiki.upsert_individual_page(
            title=title,
            body=stub_body,
            user=User(id=author_id, name="Lint"),
            change_type="ingest",
        )
    # "orphan" has no mechanical fix beyond the flag itself.


def apply_finding(tier: str, owner: str, finding_id: str, user: User) -> None:
    """Manual review: user approves a still-pending mechanical finding, so
    apply its fix now. Judgment findings have no auto-fix — approving one
    just acknowledges it (see run_lint docstring: deciding *how* to resolve
    a contradiction or staleness call is the human's job, not automated
    here). Team-tier `orphan`/`stub_candidate` findings still have no fix
    path (see `_apply_mechanical`) — apply is a no-op there; only dismiss is
    meaningful, which the UI enforces by not offering Apply on those."""
    scope = make_partition_key(tier, owner)  # type: ignore[arg-type]
    f = get_finding(tier, owner, finding_id)
    if not f:
        return
    if f.category == "mechanical" and tier == "team" and f.check_type != "missing_xref":
        return
    if f.category == "mechanical":
        _apply_mechanical(scope, tier, owner, f, user.id)
        wiki.regenerate_index(scope, tier, owner, user.id)
    _set_status(tier, owner, finding_id, "applied")


def dismiss_finding(tier: str, owner: str, finding_id: str) -> None:
    _set_status(tier, owner, finding_id, "dismissed")


def run_lint(tier: str, owner: str, mode: LintMode, user: User) -> dict:
    """Run every check, then either apply mechanical findings directly
    (automatic mode) or queue everything for review (manual mode, or any
    judgment finding regardless of mode).

    Automatic mode, team tier: `missing_xref` findings are applied via
    derived-view regeneration (never the log); `orphan`/`stub_candidate`
    still have no fix path and stay queued. This run also doubles as the
    build spec's "regenerated on a schedule" cadence for derived views —
    bounded to MAX_DERIVED_VIEW_PAGES pages per pass, prioritizing pages a
    missing_xref finding didn't already cover this run."""
    scope = make_partition_key(tier, owner)  # type: ignore[arg-type]
    schema_ctx = schema.context_block(tier, owner)

    orphans = _find_orphans(scope)
    xrefs, stubs = _find_missing_xrefs_and_stubs(scope)
    mechanical = orphans + xrefs + stubs
    judgment = _find_contradictions_and_staleness(scope, schema_ctx) + _find_data_gaps(
        scope, schema_ctx
    )

    applied = 0
    if mode == "automatic":
        if tier == "individual":
            for f in mechanical:
                if f.check_type != "orphan":
                    _apply_mechanical(scope, tier, owner, f, user.id)
                    f.status = "applied"
                    applied += 1
            if applied:
                wiki.regenerate_index(scope, tier, owner, user.id)
        else:  # team
            regenerated_page_ids: set[str] = set()
            for f in mechanical:
                if f.check_type == "missing_xref":
                    _apply_mechanical(scope, tier, owner, f, user.id)
                    f.status = "applied"
                    applied += 1
                    if f.page_ids:
                        regenerated_page_ids.add(f.page_ids[0])
                # orphan / stub_candidate: no team-tier fix, stay pending.
            remaining = MAX_DERIVED_VIEW_PAGES - len(regenerated_page_ids)
            if remaining > 0:
                for p in ab.list_pages(scope):
                    if remaining <= 0:
                        break
                    if p.title == wiki.INDEX_TITLE or p.id in regenerated_page_ids:
                        continue
                    full = ab.read_page(p.id, scope)
                    if full:
                        derived_views.regenerate_derived_view(full, schema_ctx)
                        remaining -= 1
            wiki.regenerate_index(scope, tier, owner, user.id)

    _append_findings(tier, owner, mechanical + judgment)
    return {
        "new_findings": len(mechanical) + len(judgment),
        "applied": applied,
        "queued": len(mechanical) + len(judgment) - applied,
    }
