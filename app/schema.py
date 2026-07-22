"""The per-user/team schema document (build spec: "The schema document
(per-user, co-evolved)") — the equivalent of CLAUDE.md/AGENTS.md in the
original pattern. Not application code: page-structure conventions,
personalization, and operating parameters (like the lint-queue downgrade
threshold) live here, editable by the owner directly.

Stored as a markdown file in Blob Storage, alongside the wiki it governs,
versioned like any other page (full snapshots, no delta) — but never
indexed in Cosmos and never listed as a wiki page; it's governance, not
content. blob_store.list_page_paths() already excludes "_"-prefixed names
for this reason.

Read at the start of every ingest, query, and lint operation via
context_block() and folded into the model's system prompt.
"""

import re

from app import blob_store

DEFAULT_SCHEMA = """\
---
automatic_lint_queue_threshold: 20
---

# Schema

This document governs how the agent maintains this wiki. Edit it freely —
it is yours, not application code. The agent reads it before every ingest,
query, and lint pass.

## Page structure conventions
- Prefer extending an existing page over creating a near-duplicate one.
- Create a new entity/concept page when a topic is referenced from two or
  more other pages and doesn't yet have a home.
- Keep summaries concise: key facts, figures, decisions, and open questions
  — not a transcript of the source.

## Conflicting sources
- When two sources disagree, note both claims and which source is newer,
  rather than silently picking one.

## Emphasis
- (Add domain-specific guidance here as you learn what you care about.)

## How this person thinks
- (Add reasoning patterns, preferred framing, or recurring concerns here —
  this section is read alongside every query and ingest as personalization.)

## Operating parameters
- `automatic_lint_queue_threshold` (frontmatter above): once this many
  automatic-ingest-derived findings sit unreviewed in the lint queue,
  automatic ingest mode is disabled (forced to manual) until the queue is
  worked down. Does not apply while automatic lint mode is on, since
  mechanical findings clear themselves in that mode.
"""

_THRESHOLD_RE = re.compile(r"^automatic_lint_queue_threshold:\s*(\d+)\s*$", re.MULTILINE)


def _current_path(tier: str, owner: str) -> str:
    return f"{tier}/{owner}/_schema.md"


def _version_prefix(tier: str, owner: str) -> str:
    return f"{tier}/{owner}/_schema_versions/"


def read_schema(tier: str, owner: str) -> str:
    """Current schema doc, or the shipped default if the owner hasn't
    customized one yet. Never auto-written on read — it stays theirs to
    create the moment they actually save an edit."""
    body = blob_store.read_text(_current_path(tier, owner))
    return body if body is not None else DEFAULT_SCHEMA


def write_schema(tier: str, owner: str, body: str) -> None:
    """Save a new version: snapshot the current content (if any) before
    overwriting — same full-snapshot versioning as wiki pages, without a
    Cosmos version index entry (governance doc, not a wiki page)."""
    path = _current_path(tier, owner)
    existing = blob_store.read_text(path)
    if existing is not None:
        n = len(blob_store.list_paths(_version_prefix(tier, owner))) + 1
        blob_store.write_text(f"{_version_prefix(tier, owner)}v{n}.md", existing)
    blob_store.write_text(path, body)


def context_block(tier: str, owner: str) -> str:
    """Formatted for inclusion in a model's system prompt — the
    read-at-start-of-every-operation step the build spec requires for
    ingest, query, and lint."""
    return f"<schema_doc>\n{read_schema(tier, owner)}\n</schema_doc>"


def system_prefix(op_system: str, tier: str, owner: str) -> str:
    """Assemble a system prompt with the owner-stable schema block FIRST and
    the operation-specific instruction after it (build spec: prompt-cache
    discipline). The schema doc is identical across ingest/query/lint for a
    given owner, so leading with it lets those operations share one cacheable
    prefix; the op-specific text — which differs per operation — comes last.
    Any volatile per-call content (source text, the question) must be appended
    by the caller AFTER this prefix, never spliced into it."""
    return f"{context_block(tier, owner)}\n\n{op_system}"


DEFAULT_AUTOMATIC_LINT_QUEUE_THRESHOLD = 20


def automatic_lint_queue_threshold(tier: str, owner: str) -> int:
    """Parse `automatic_lint_queue_threshold: <int>` from the schema doc's
    frontmatter. Not full YAML (no new dependency for one integer) — just
    the one user-configurable operating parameter this doc currently needs
    (see app.lint's threshold-triggered automatic-ingest downgrade)."""
    match = _THRESHOLD_RE.search(read_schema(tier, owner))
    return int(match.group(1)) if match else DEFAULT_AUTOMATIC_LINT_QUEUE_THRESHOLD
