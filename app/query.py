"""Query operation (build spec: peer to Ingest and Lint, not optional).

Ask a question against a scope's wiki, get a synthesized answer with
citations back to the pages that fed it. The answer can be filed back into
the wiki as a new page — same diff-review flow as ingest, logged with a
distinct IngestLog source_type (`query_derived`) so it's traceable as
synthesis rather than source-derived. Save drafts held in process memory,
same tradeoff as manual-mode ingest sessions (see app.ingest.pipeline).
"""

import uuid
from dataclasses import dataclass, field

from app import abstractions as ab
from app import wiki
from app.models import IngestLogEntry, Page, User, make_partition_key

ANSWER_SYSTEM = (
    "You answer questions against a knowledge wiki. You are given retrieved page "
    "content as context. Synthesize a direct answer, and cite the specific pages "
    "you drew on using [[Page Title]] wikilinks inline, next to the claims they "
    "support. If the context doesn't contain the answer, say so plainly rather "
    "than guessing."
)

SAVE_SYSTEM = (
    "Turn this question-and-answer exchange into a standalone wiki page: a "
    "reusable synthesis, not a transcript. First line must be the title as "
    "'# Title'. Use [[wikilinks]] for related concepts. Output only the markdown."
)


@dataclass
class QueryAnswer:
    question: str
    answer: str
    tier: str
    owner: str
    sources: list[dict] = field(default_factory=list)  # [{page_id, title, score}]


@dataclass
class QuerySaveDraft:
    id: str
    user_id: str
    tier: str
    owner: str
    question: str
    proposed_title: str
    proposed_body: str  # includes the query_derived frontmatter block
    diff: str


_drafts: dict[str, QuerySaveDraft] = {}


def ask(question: str, tier: str, owner: str) -> QueryAnswer:
    scope = make_partition_key(tier, owner)  # type: ignore[arg-type]
    context = ab.get_context(question, scope)
    answer = ab.call_model(question, context=context, system=ANSWER_SYSTEM, max_tokens=900)
    sources = ab.search(question, scope, top_k=5)
    return QueryAnswer(question=question, answer=answer, tier=tier, owner=owner, sources=sources)


def _frontmatter(question: str) -> str:
    escaped = question.replace('"', '\\"')
    return f'---\nquery_derived: true\nquestion: "{escaped}"\n---\n\n'


def prepare_save(qa: QueryAnswer, user: User) -> QuerySaveDraft:
    """Draft a standalone page from the Q&A and compute its diff — same
    review step as an ingest proposal, just a different origin."""
    draft = ab.call_model(
        f"Question: {qa.question}\n\nAnswer:\n{qa.answer}",
        system=SAVE_SYSTEM,
        max_tokens=1500,
    )
    title, body = wiki.split_title(draft)
    if title == "Untitled":
        title = qa.question.strip().rstrip("?").capitalize()
    body = _frontmatter(qa.question) + body

    scope = make_partition_key(qa.tier, qa.owner)  # type: ignore[arg-type]
    existing = ab.find_page_by_title(title, scope)
    diff = wiki.unified_diff(existing.body if existing else "", body, title)

    d = QuerySaveDraft(
        id=uuid.uuid4().hex,
        user_id=user.id,
        tier=qa.tier,
        owner=qa.owner,
        question=qa.question,
        proposed_title=title,
        proposed_body=body,
        diff=diff,
    )
    _drafts[d.id] = d
    return d


def get_draft(draft_id: str, user: User) -> QuerySaveDraft | None:
    d = _drafts.get(draft_id)
    return d if d and d.user_id == user.id else None


def approve(draft: QuerySaveDraft, user: User) -> Page:
    """Write the drafted page (in-place rewrite for individual, bottom-append
    for team — same edit rule each tier already enforces for ingest), log it
    as query_derived, and regenerate the index."""
    scope = make_partition_key(draft.tier, draft.owner)  # type: ignore[arg-type]
    log = IngestLogEntry(
        partition_key=scope,
        source_type="query_derived",
        source_ref=f"query: {draft.question}",
        mode="manual",
        tier=draft.tier,  # type: ignore[arg-type]
    )
    if draft.tier == "individual":
        page = wiki.upsert_individual_page(
            title=draft.proposed_title,
            body=draft.proposed_body,
            user=user,
            change_type="ingest",
            source_ref_id=log.id,
        )
    else:
        page = wiki.append_to_team_page(
            title=draft.proposed_title,
            content=draft.proposed_body,
            team_id=draft.owner,
            user=user,
            source_ref_id=log.id,
        )
    log.pages_affected = [page.id]
    ab.append_ingest_log(log)
    wiki.regenerate_index(scope, draft.tier, draft.owner, user.id)
    _drafts.pop(draft.id, None)
    return page


def discard(draft_id: str) -> None:
    _drafts.pop(draft_id, None)
