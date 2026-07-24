"""Ingest pipeline: manual (discussion + approval) and automatic modes.

Batch drops are always processed per-source, never combined (build spec).
Manual-mode sessions are persisted through `app.review_store` (non-indexed
blobs), so a discussion survives a container restart or a request landing on
a different replica — both of which the Container Apps deployment shape makes
routine.
"""

import uuid
from dataclasses import asdict, dataclass, field

from app import abstractions as ab
from app import blob_store, review_store, schema, wiki
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
        system=f"{schema_ctx}\n\n{SUMMARY_SYSTEM}",
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
    source_ref_id: str = ""  # blob path of the immutable raw source
    messages: list[dict] = field(default_factory=list)
    proposed_title: str | None = None
    proposed_body: str | None = None
    changeset_id: str | None = None


def _session_payload(session: ManualSession) -> dict:
    """`source.raw_bytes` is deliberately dropped: the verbatim original is
    already in the immutable sources container (see start_manual_session), and
    round-tripping megabytes of base64 through the session blob on every
    discussion turn would be pure waste."""
    payload = asdict(session)
    payload["source"] = {**payload["source"], "raw_bytes": b""}
    payload["source"].pop("raw_bytes")
    return payload


def _save_session(session: ManualSession) -> None:
    review_store.save("sessions", session.user_id, session.id, _session_payload(session))


def start_manual_session(source: ExtractedSource, user: User) -> ManualSession:
    # Store the raw source now rather than at proposal time. The build spec
    # makes the raw store immutable and provenance a hard requirement, and a
    # source the user actually dropped is worth keeping even if the discussion
    # is later abandoned. It also keeps the persisted session small.
    session = ManualSession(
        id=uuid.uuid4().hex,
        user_id=user.id,
        source=source,
        source_ref_id=_store_raw_source(source, user),
    )
    session.messages = [
        {
            "role": "system",
            # Schema block first (stable, cacheable), then the operation
            # instruction, then the volatile source text last.
            "content": schema.context_block("individual", user.id)
            + "\n\n"
            + DISCUSSION_SYSTEM
            + "\n\n<source>\n"
            + source.text
            + "\n</source>",
        },
        {"role": "user", "content": "I've just dropped this source. What did you find notable?"},
    ]
    reply = ab.call_model_chat(session.messages, max_tokens=600)
    session.messages.append({"role": "assistant", "content": reply})
    _save_session(session)
    return session


def get_session(session_id: str, user: User) -> ManualSession | None:
    """Ownership is structural — the blob path is built from the caller's own
    user id, so another user's session is unreachable, not merely rejected."""
    payload = review_store.load("sessions", user.id, session_id)
    if payload is None:
        return None
    return ManualSession(
        source=ExtractedSource(**payload.pop("source")),
        **payload,
    )


def get_changeset(session: ManualSession, user: User) -> wiki.Changeset | None:
    if not session.changeset_id:
        return None
    return wiki.get_changeset(session.changeset_id, user)


def discuss(session: ManualSession, user_message: str) -> str:
    session.messages.append({"role": "user", "content": user_message})
    reply = ab.call_model_chat(session.messages, max_tokens=600)
    session.messages.append({"role": "assistant", "content": reply})
    _save_session(session)
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
    changeset = wiki.build_changeset(
        tier="individual",
        owner=user.id,
        user=user,
        primary_title=title,
        primary_body=body,
        log_source_type=session.source.source_type,
        log_source_ref=session.source_ref_id,
        log_mode="manual",
        schema_ctx=schema_ctx,
        source_ref_id=session.source_ref_id,
    )
    session.changeset_id = changeset.id
    _save_session(session)
    return session


def approve(session: ManualSession, user: User, selected: set[int] | None) -> list[str]:
    """On approval: write the selected changeset items (None = approve-all,
    empty set = reject-all handled by the caller before reaching here) and
    append one ingest log entry covering every page actually written."""
    changeset = get_changeset(session, user)
    assert changeset is not None
    scope = make_partition_key("individual", user.id)
    pages = wiki.apply_changeset(changeset, selected, user.id)
    if pages:
        log = IngestLogEntry(
            partition_key=scope,
            source_type=changeset.log_source_type,
            source_ref=changeset.log_source_ref,
            mode="manual",
            tier="individual",
            pages_affected=[p.id for p in pages],
        )
        ab.append_ingest_log(log)
    review_store.delete("sessions", user.id, session.id)
    return [p.title for p in pages]


def discard(session: ManualSession, user: User) -> None:
    """Takes a resolved session rather than a bare id, so the caller has
    already passed the ownership check in get_session()."""
    if session.changeset_id:
        wiki.discard_changeset(session.changeset_id, user.id)
    review_store.delete("sessions", user.id, session.id)
