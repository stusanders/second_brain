"""Ingest pipeline: manual (discussion + approval) and automatic modes.

Batch drops are always processed per-source, never combined (build spec).
Manual-mode sessions are held in process memory — acceptable for a
single-replica POC; a session dies with the container, nothing durable is
lost (the source can simply be re-dropped).
"""

import uuid
from dataclasses import dataclass, field

from app import abstractions as ab
from app import blob_store, wiki
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


def _split_title(markdown: str) -> tuple[str, str]:
    lines = markdown.strip().splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        return lines[0].lstrip("# ").strip(), "\n".join(lines[1:]).strip()
    return "Untitled", markdown.strip()


# ------------------------------------------------------------ automatic mode


def ingest_automatic(source: ExtractedSource, user: User) -> list[str]:
    """One independent summary per source, written directly — no approval gate.
    Returns affected page titles."""
    scope = make_partition_key("individual", user.id)
    draft = ab.call_model(
        "Summarize this source into a wiki page.",
        context=source.text,
        system=SUMMARY_SYSTEM,
    )
    title, body = _split_title(draft)

    log = IngestLogEntry(
        partition_key=scope,
        source_type=source.source_type,
        source_ref=_store_raw_source(source, user),
        mode="automatic",
        tier="individual",
    )
    page = wiki.upsert_individual_page(
        title=title, body=body, user=user, change_type="ingest", source_ref_id=log.id
    )
    log.pages_affected = [page.id]
    ab.append_ingest_log(log)
    wiki.regenerate_index(scope, "individual", user.id, user.id)
    return [page.title]


# --------------------------------------------------------------- manual mode


@dataclass
class ManualSession:
    id: str
    user_id: str
    source: ExtractedSource
    messages: list[dict] = field(default_factory=list)
    proposed_title: str | None = None
    proposed_body: str | None = None
    diff: str | None = None


_sessions: dict[str, ManualSession] = {}


def start_manual_session(source: ExtractedSource, user: User) -> ManualSession:
    session = ManualSession(id=uuid.uuid4().hex, user_id=user.id, source=source)
    session.messages = [
        {"role": "system", "content": DISCUSSION_SYSTEM + "\n\n<source>\n" + source.text + "\n</source>"},
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
    """Draft the page from source + discussion, and compute the diff shown
    for approval (against the existing page if the title already exists)."""
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
    title, body = _split_title(draft)
    session.proposed_title, session.proposed_body = title, body

    scope = make_partition_key("individual", user.id)
    existing = ab.find_page_by_title(title, scope)
    session.diff = wiki.unified_diff(existing.body if existing else "", body, title)
    return session


def approve(session: ManualSession, user: User) -> list[str]:
    """On approval: write page, update index, append log entry."""
    assert session.proposed_title and session.proposed_body is not None
    scope = make_partition_key("individual", user.id)
    log = IngestLogEntry(
        partition_key=scope,
        source_type=session.source.source_type,
        source_ref=_store_raw_source(session.source, user),
        mode="manual",
        tier="individual",
    )
    page = wiki.upsert_individual_page(
        title=session.proposed_title,
        body=session.proposed_body,
        user=user,
        change_type="ingest",
        source_ref_id=log.id,
    )
    log.pages_affected = [page.id]
    ab.append_ingest_log(log)
    wiki.regenerate_index(scope, "individual", user.id, user.id)
    _sessions.pop(session.id, None)
    return [page.title]


def discard(session_id: str) -> None:
    _sessions.pop(session_id, None)
