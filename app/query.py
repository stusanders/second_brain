"""Query operation (build spec: peer to Ingest and Lint, not optional).

Ask a question against a scope's wiki, get a synthesized answer with
citations back to the pages that fed it. The answer can be filed back into
the wiki as a new page — same multi-page changeset review flow as ingest
(build spec: "same diff-review flow as ingest, same cross-page update
logic"), logged with a distinct IngestLog source_type (`query_derived`) so
it's traceable as synthesis rather than source-derived.
"""

from dataclasses import dataclass, field

from app import abstractions as ab
from app import schema, wiki
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


def ask(question: str, tier: str, owner: str) -> QueryAnswer:
    scope = make_partition_key(tier, owner)  # type: ignore[arg-type]
    context = ab.get_context(question, scope)
    system = ANSWER_SYSTEM + "\n\n" + schema.context_block(tier, owner)
    answer = ab.call_model(question, context=context, system=system, max_tokens=900)
    sources = ab.search(question, scope, top_k=5)
    return QueryAnswer(question=question, answer=answer, tier=tier, owner=owner, sources=sources)


def _frontmatter(question: str) -> str:
    escaped = question.replace('"', '\\"')
    return f'---\nquery_derived: true\nquestion: "{escaped}"\n---\n\n'


def prepare_save(qa: QueryAnswer, user: User) -> wiki.Changeset:
    """Draft a standalone page from the Q&A, then build the same multi-page
    changeset ingest uses (cross-page updates + index) — the build spec
    calls for "same diff-review flow as ingest, same cross-page update
    logic" for query-derived saves, not just a single-page diff."""
    schema_ctx = schema.context_block(qa.tier, qa.owner)
    draft = ab.call_model(
        f"Question: {qa.question}\n\nAnswer:\n{qa.answer}",
        system=SAVE_SYSTEM + "\n\n" + schema_ctx,
        max_tokens=1500,
    )
    title, body = wiki.split_title(draft)
    if title == "Untitled":
        title = qa.question.strip().rstrip("?").capitalize()
    body = _frontmatter(qa.question) + body

    return wiki.build_changeset(
        tier=qa.tier,
        owner=qa.owner,
        user=user,
        primary_title=title,
        primary_body=body,
        log_source_type="query_derived",
        log_source_ref=f"query: {qa.question}",
        log_mode="manual",
        schema_ctx=schema_ctx,
    )


def get_changeset(changeset_id: str, user: User) -> wiki.Changeset | None:
    return wiki.get_changeset(changeset_id, user)


def approve(changeset: wiki.Changeset, user: User, selected: set[int] | None) -> list[Page]:
    """Write the selected changeset items (in-place rewrite for individual,
    bottom-append for team — same edit rule each tier already enforces for
    ingest) and log the write as query_derived."""
    scope = make_partition_key(changeset.tier, changeset.owner)  # type: ignore[arg-type]
    pages = wiki.apply_changeset(changeset, selected, user.id)
    if pages:
        log = IngestLogEntry(
            partition_key=scope,
            source_type=changeset.log_source_type,
            source_ref=changeset.log_source_ref,
            mode="manual",
            tier=changeset.tier,  # type: ignore[arg-type]
            pages_affected=[p.id for p in pages],
        )
        ab.append_ingest_log(log)
    return pages


def discard(changeset_id: str) -> None:
    wiki.discard_changeset(changeset_id)
