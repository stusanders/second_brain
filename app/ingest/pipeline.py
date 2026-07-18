"""Ingest pipeline: manual (discussion + approval) and automatic modes.

Batch drops are always processed per-source, never combined (build spec).
Manual-mode sessions are held in process memory — acceptable for a
single-replica POC; a session dies with the container, nothing durable is
lost (the source can simply be re-dropped).
"""

import uuid
from dataclasses import dataclass, field

from app import abstractions as ab
from app import blob_store, schema, wiki
from app.ingest.extractors import ExtractedSource
from app.models import IngestLogEntry, User, make_partition_key


def _store_raw_source(source: ExtractedSource, user: User) -> str:
    """Persist the source verbatim in blob (immutable, never edited or
    deleted) and return its blob path — used as the IngestLogEntry's
    source_ref so provenance is always traceable back to the original."""
    return blob_store.write_raw_source(
        "individual", user.id, source.filename or source.source_ref, source.raw_bytes
    )


SUMMARY_SYSTEM = (
    "You maintain a user's personal knowledge wiki. Given a source document, "
    "write a concise wiki page in markdown capturing its key takeaways, facts, "
    "figures, and any decisions or open questions. Reference related concepts "
    "as [[wikilinks]] where natural. First line must be a page title as "
    "'# Title'. No preamble."
)

DISCUSSION_SYSTEM = (
    "You maintain a user's personal knowledge wiki. You have just read a source "
    "document, shown below. Lead an open-ended discussion with the user about its "
    "key takeaways: ask what matters to them about it, probe for context the "
    "document doesn't contain, and share what you found notable. Keep turns short. "
    "When the user is done discussing, they will ask you to draft the page."
)


# ------------------------------------------------------------ automatic mode


def ingest_automatic(source: ExtractedSource, user: User) -> list[str]:
    """One independent summary per source, written directly — no approval
    gate. Still builds the full multi-page changeset (breadth is a
    requirement, not just a manual-mode nicety — the build spec doesn't
    scope "10-15 pages is normal for one source" to manual mode only) and
    applies every item immediately. Returns affected page titles."""
    scope = make_partition_key("individual", user.id)
    schema_ctx = schema.context_block("individual", user.id)
    draft = ab.call_model(
        "Summarize this source into a wiki page.",
        context=source.text,
        system=SUMMARY_SYSTEM + "\n\n" + schema_ctx,
    )
    title, body = wiki.split_title(draft)
    source_ref_id = _store_raw_source(source, user)

    changeset = wiki.build_changeset(
        tier="individual",
        owner=user.id,
        user=user,
        primary_title=title,
        primary_body=body,
        log_source_type=source.source_type,
        log_source_ref=source_ref_id,
        log_mode="automatic",
        schema_ctx=schema_ctx,
        source_ref_id=source_ref_id,
    )
    pages = wiki.apply_changeset(changeset, None, user.id)  # None = approve-all

    log = IngestLogEntry(
        partition_key=scope,
        source_type=source.source_type,
        source_ref=source_ref_id,
        mode="automatic",
        tier="individual",
        pages_affected=[p.id for p in pages],
    )
    ab.append_ingest_log(log)
    return [p.title for p in pages]


# --------------------------------------------------------------- manual mode


@dataclass
class ManualSession:
    id: str
    user_id: str
    source: ExtractedSource
    messages: list[dict] = field(default_factory=list)
    proposed_title: str | None = None
    proposed_body: str | None = None
    changeset: wiki.Changeset | None = None


_sessions: dict[str, ManualSession] = {}


def start_manual_session(source: ExtractedSource, user: User) -> ManualSession:
    session = ManualSession(id=uuid.uuid4().hex, user_id=user.id, source=source)
    session.messages = [
        {
            "role": "system",
            "content": DISCUSSION_SYSTEM
            + "\n\n"
            + schema.context_block("individual", user.id)
            + "\n\n<source>\n"
            + source.text
            + "\n</source>",
        },
        {"role": "user", "content": "I've just dropped this source. What did you find notable?"},
    ]
    reply = ab.call_model_chat(session.messages, max_tokens=600)
    session.messages.append({"role": "assistant", "content": reply})
    _sessions[session.id] = session
    return session


def get_session(session_id: str, user: User) -> ManualSession | None:
    session = _sessions.get(session_id)
    return session if session and session.user_id == user.id else None


def discuss(session: ManualSession, user_message: str) -> str:
    session.messages.append({"role": "user", "content": user_message})
    reply = ab.call_model_chat(session.messages, max_tokens=600)
    session.messages.append({"role": "assistant", "content": reply})
    return reply


def propose_page(session: ManualSession, user: User) -> ManualSession:
    """Draft the primary page from source + discussion, then build the full
    multi-page changeset (cross-page updates + index) shown for review as
    one unit (build spec: "must present a multi-page changeset as one
    reviewable unit")."""
    session.messages.append(
        {
            "role": "user",
            "content": (
                "Please draft the wiki page now, incorporating our discussion. "
                "First line must be the title as '# Title'. Use [[wikilinks]] "
                "for related concepts. Output only the markdown."
            ),
        }
    )
    draft = ab.call_model_chat(session.messages, max_tokens=2000)
    session.messages.append({"role": "assistant", "content": draft})
    title, body = wiki.split_title(draft)
    session.proposed_title, session.proposed_body = title, body

    schema_ctx = schema.context_block("individual", user.id)
    source_ref_id = _store_raw_source(session.source, user)
    session.changeset = wiki.build_changeset(
        tier="individual",
        owner=user.id,
        user=user,
        primary_title=title,
        primary_body=body,
        log_source_type=session.source.source_type,
        log_source_ref=source_ref_id,
        log_mode="manual",
        schema_ctx=schema_ctx,
        source_ref_id=source_ref_id,
    )
    return session


def approve(session: ManualSession, user: User, selected: set[int] | None) -> list[str]:
    """On approval: write the selected changeset items (None = approve-all,
    empty set = reject-all handled by the caller before reaching here) and
    append one ingest log entry covering every page actually written."""
    assert session.changeset is not None
    scope = make_partition_key("individual", user.id)
    pages = wiki.apply_changeset(session.changeset, selected, user.id)
    if pages:
        log = IngestLogEntry(
            partition_key=scope,
            source_type=session.changeset.log_source_type,
            source_ref=session.changeset.log_source_ref,
            mode="manual",
            tier="individual",
            pages_affected=[p.id for p in pages],
        )
        ab.append_ingest_log(log)
    _sessions.pop(session.id, None)
    return [p.title for p in pages]


def discard(session_id: str) -> None:
    session = _sessions.pop(session_id, None)
    if session and session.changeset:
        wiki.discard_changeset(session.changeset.id)
