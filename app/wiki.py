"""Wiki page services: wikilink resolution, index maintenance, edit rules.

Individual tier: pages are editable in place (incremental refinement).
Team tier: append-only, no exceptions — enforced here, not by prompt.

Also home to the multi-page changeset machinery (build spec: "Cross-page
updates" — a single ingested source is expected to touch many pages, and
the diff-review UI must present that as one reviewable unit, never narrowed
to a single destination). `build_changeset()` / `apply_changeset()` are the
one write path shared by ingest, push, and query-save.
"""

import difflib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field

from app import abstractions as ab
from app.models import IngestMode, Page, User, make_partition_key, new_id, now_iso

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)\]\]")
INDEX_TITLE = "Index"

# Cost discipline: cap how many existing pages a single changeset build will
# consider for cross-page updates, even though the build spec's "10-15 pages
# is normal for one source" figure includes the primary page + index.
MAX_CROSS_PAGE_CANDIDATES = 5

CROSS_PAGE_DECIDE_SYSTEM = (
    "You maintain a knowledge wiki. A new or updated page is about to be written. "
    "Given its content and one existing related page, decide whether the existing "
    "page should also be updated as a result (e.g. it should now cross-reference "
    "the new page, one of its claims is extended or contradicted by it, or it "
    "mentions a concept the new page now covers in more depth). Answer with "
    "EXACTLY one word: 'YES' or 'NO'."
)

CROSS_PAGE_DRAFT_SYSTEM = (
    "You maintain a knowledge wiki. Rewrite the existing page below to account for "
    "a newly written related page — add a [[wikilink]] to it where relevant, and "
    "extend or note tension with any claim it affects. Preserve everything in the "
    "existing page that's still accurate; make the smallest edit that captures the "
    "connection. Output the complete new page body only, no preamble, no title line."
)

CROSS_PAGE_NOTE_SYSTEM = (
    "You maintain a team knowledge wiki whose pages are append-only logs. Given an "
    "existing team page and a newly pushed/ingested related page, write a short "
    "(2-4 sentence) note to append to the existing page: how the new page relates, "
    "with a [[wikilink]] to it. Output only the note text, no preamble, no heading."
)


def split_title(markdown: str) -> tuple[str, str]:
    """First line '# Title' becomes the title; the rest is the body. Shared by
    every model-drafted page (ingest, manual discussion, query-derived save)."""
    lines = markdown.strip().splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        return lines[0].lstrip("# ").strip(), "\n".join(lines[1:]).strip()
    return "Untitled", markdown.strip()


def resolve_links(body: str, scope: str) -> list[str]:
    """Map [[Title]] links in a body to existing page ids."""
    ids = []
    for title in WIKILINK_RE.findall(body):
        page = ab.find_page_by_title(title.strip(), scope)
        if page and page.id not in ids:
            ids.append(page.id)
    return ids


def upsert_individual_page(
    *, title: str, body: str, user: User, change_type: str, source_ref_id: str | None = None
) -> Page:
    scope = make_partition_key("individual", user.id)
    page = ab.find_page_by_title(title, scope)
    if page:
        page.body = body
        page.version += 1
    else:
        page = Page(
            partition_key=scope, title=title, body=body, tier="individual", owner_id=user.id
        )
    if source_ref_id and source_ref_id not in page.source_refs:
        page.source_refs.append(source_ref_id)
    page.links_out = resolve_links(body, scope)
    return ab.write_page(page, change_type=change_type, author_id=user.id)


def _team_append_body(
    existing_body: str, content: str, author_name: str, conflict_note: str | None = None
) -> str:
    """Compute the full new body for a team-page append, without writing
    anything — shared by append_to_team_page and build_changeset so a
    changeset item's `new_body` and the eventual write agree exactly."""
    block_lines = [f"\n\n---\n\n### Pushed by {author_name} — {now_iso()[:10]}\n"]
    if conflict_note:
        block_lines.append(f"> **Note:** {conflict_note}\n")
    block_lines.append(content.strip())
    block = "\n".join(block_lines)
    return existing_body + block if existing_body else block.lstrip("\n-— ")


def append_to_team_page(
    *,
    title: str,
    content: str,
    team_id: str,
    user: User,
    conflict_note: str | None = None,
    source_ref_id: str | None = None,
) -> Page:
    """The ONLY write path for team pages. Existing text is never modified;
    new content lands as a dated, attributed, bottom-appended section."""
    scope = make_partition_key("team", team_id)
    page = ab.find_page_by_title(title, scope)
    new_body = _team_append_body(page.body if page else "", content, user.name, conflict_note)
    if page:
        page.body = new_body
        page.version += 1
    else:
        page = Page(partition_key=scope, title=title, body=new_body, tier="team", owner_id=team_id)
    if source_ref_id and source_ref_id not in page.source_refs:
        page.source_refs.append(source_ref_id)
    page.links_out = resolve_links(page.body, scope)
    return ab.write_page(page, change_type="merge_append", author_id=user.id)


def _index_body(scope: str, tier: str, owner: str, extra_titles: list[str] | None = None) -> str:
    """Deterministic contents page — a plain catalogue of [[links]] grouped by
    category, with NO LLM prose (build spec: "Just links, no LLM-generated
    prose summary"). Categories come from the cached knowledge-map communities
    (link-graph, not embeddings) so Contents and the Map tell the same story;
    the only model calls involved are the community-naming ones already cached
    in the map artifact, not one per index regeneration.

    `extra_titles` lets a changeset's index item reflect pages the changeset is
    about to add/update but that aren't in Cosmos yet (nothing is written until
    approval) — otherwise the index diff shown for review would omit the very
    page being approved. Such not-yet-clustered pages appear under "Recently
    added" until the next map regeneration places them.

    Before a map has ever been built, falls back to a flat alphabetical
    catalogue — still deterministic, still no model call."""
    from app import knowledge_map

    pages = [p for p in ab.list_pages(scope) if p.title != INDEX_TITLE]
    current_titles = {p.title for p in pages}
    title_by_id = {p.id: p.title for p in pages}
    extras = [t for t in (extra_titles or []) if t != INDEX_TITLE]

    def _links(titles: list[str]) -> str:
        return "\n".join(f"- [[{t}]]" for t in sorted(titles))

    wiki_map = knowledge_map.load_map(tier, owner)
    if not wiki_map:
        all_titles = current_titles | set(extras)
        return _links(list(all_titles)) if all_titles else "_No pages yet._"

    sections: list[str] = []
    placed: set[str] = set()
    for community in wiki_map.get("communities", []):
        members = [title_by_id[i] for i in community["page_ids"] if i in title_by_id]
        if not members:
            continue
        placed.update(members)
        sections.append(f"## {community['name']}\n{_links(members)}")

    recently_added = [t for t in extras if t not in placed]
    if recently_added:
        placed.update(recently_added)
        sections.append(f"## Recently added\n{_links(recently_added)}")

    unlinked = [t for t in current_titles if t not in placed]
    if unlinked:
        sections.append(f"## Unlinked\n{_links(unlinked)}")

    return "\n\n".join(sections) if sections else "_No pages yet._"


def regenerate_index(scope: str, tier: str, owner: str, author_id: str) -> Page:
    """Rebuild the generated contents page, catalogued by category via a
    short structured model call. Standalone entry point used by lint (index
    regen after a mechanical fix); ingest/push/query route index regen
    through the changeset's own index item instead (see build_changeset)."""
    body = _index_body(scope, tier, owner)
    page = ab.find_page_by_title(INDEX_TITLE, scope)
    if page:
        page.body = body
        page.version += 1
    else:
        page = Page(partition_key=scope, title=INDEX_TITLE, body=body, tier=tier, owner_id=owner)  # type: ignore[arg-type]
    page.links_out = resolve_links(body, scope)
    return ab.write_page(page, change_type="ingest", author_id=author_id)


def backlinks(page_id: str, scope: str) -> list[Page]:
    return [p for p in ab.list_pages(scope) if page_id in p.links_out]


def unified_diff(old: str, new: str, title: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{title} (current)",
            tofile=f"{title} (proposed)",
        )
    )


def _diff_summary(old: str, new: str, is_new: bool) -> str:
    """One-line per-page summary for the collapsed changeset view (build
    spec: "Client X — 2 lines added", "Index — 1 entry")."""
    if is_new:
        return f"new page — {len(new.splitlines())} lines"
    added = removed = 0
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    bits = []
    if added:
        bits.append(f"{added} line{'s' if added != 1 else ''} added")
    if removed:
        bits.append(f"{removed} removed")
    return ", ".join(bits) if bits else "no textual change"


# ---------------------------------------------------------- changeset review


@dataclass
class ChangesetItem:
    title: str
    is_new: bool
    old_body: str
    new_body: str
    diff: str
    summary: str


@dataclass
class Changeset:
    """A multi-page unit of proposed writes, reviewed as one whole (build
    spec: "must present a multi-page changeset as one reviewable unit").
    `items[0]` is always the primary page the caller drafted; any further
    items are cross-page updates or the regenerated index, in that order.
    `log_*` carries what the caller needs to write an IngestLogEntry once
    the (possibly partial) set of approved pages is known."""

    id: str
    tier: str
    owner: str
    user_id: str
    items: list[ChangesetItem] = field(default_factory=list)
    log_source_type: str = ""
    log_source_ref: str = ""
    log_mode: IngestMode = "manual"
    source_ref_id: str | None = None  # raw-source blob path — provenance, every item gets it
    conflicts: list[str] = field(default_factory=list)  # push-only: flagged conflict notes to show


_changesets: dict[str, Changeset] = {}


def get_changeset(changeset_id: str, user: User) -> Changeset | None:
    cs = _changesets.get(changeset_id)
    return cs if cs and cs.user_id == user.id else None


def discard_changeset(changeset_id: str) -> None:
    _changesets.pop(changeset_id, None)


def _mk_item(title: str, old_body: str, new_body: str, is_new: bool) -> ChangesetItem:
    return ChangesetItem(
        title=title,
        is_new=is_new,
        old_body=old_body,
        new_body=new_body,
        diff=unified_diff(old_body, new_body, title),
        summary=_diff_summary(old_body, new_body, is_new),
    )


def _cross_page_candidates(scope: str, primary_title: str, primary_body: str) -> list[Page]:
    hits = ab.search(
        f"{primary_title}\n{primary_body[:2000]}", scope, top_k=MAX_CROSS_PAGE_CANDIDATES + 2
    )
    candidates = []
    for hit in hits:
        if hit["title"] in (primary_title, INDEX_TITLE):
            continue
        page = ab.read_page(hit["page_id"], scope)
        if page:
            candidates.append(page)
        if len(candidates) >= MAX_CROSS_PAGE_CANDIDATES:
            break
    return candidates


def build_changeset(
    *,
    tier: str,
    owner: str,
    user: User,
    primary_title: str,
    primary_body: str,
    log_source_type: str,
    log_source_ref: str,
    log_mode: str,
    schema_ctx: str = "",
    conflict_note: str | None = None,
    source_ref_id: str | None = None,
    include_cross_page: bool = True,
    include_index: bool = True,
) -> Changeset:
    """Draft the full proposed write as one reviewable, multi-page unit:
    the primary page, then (for individual/team ingest and query-derived
    saves) any existing related pages the model decides should also change,
    then the regenerated index. Nothing is written — see apply_changeset."""
    scope = make_partition_key(tier, owner)  # type: ignore[arg-type]
    items: list[ChangesetItem] = []

    existing = ab.find_page_by_title(primary_title, scope)
    if tier == "team":
        new_body = _team_append_body(
            existing.body if existing else "", primary_body, user.name, conflict_note
        )
    else:
        new_body = primary_body
    items.append(_mk_item(primary_title, existing.body if existing else "", new_body, not existing))

    if include_cross_page:
        for candidate in _cross_page_candidates(scope, primary_title, primary_body):
            decision = ab.call_model(
                f"New/updated page '{primary_title}':\n{primary_body[:3000]}\n\n"
                f"Existing related page '{candidate.title}':\n{candidate.body[:3000]}",
                system=(schema_ctx + "\n\n" if schema_ctx else "") + CROSS_PAGE_DECIDE_SYSTEM,
                max_tokens=10,
            ).strip()
            if not decision.upper().startswith("YES"):
                continue
            if tier == "team":
                note = ab.call_model(
                    f"New/updated page '{primary_title}':\n{primary_body[:3000]}\n\n"
                    f"Existing team page '{candidate.title}':\n{candidate.body[:3000]}",
                    system=(schema_ctx + "\n\n" if schema_ctx else "") + CROSS_PAGE_NOTE_SYSTEM,
                    max_tokens=250,
                )
                cand_new_body = _team_append_body(candidate.body, note, user.name)
            else:
                cand_new_body = ab.call_model(
                    f"New/updated page '{primary_title}':\n{primary_body[:3000]}\n\n"
                    f"Existing page to update, '{candidate.title}':\n{candidate.body}",
                    system=(schema_ctx + "\n\n" if schema_ctx else "") + CROSS_PAGE_DRAFT_SYSTEM,
                    max_tokens=2000,
                )
            items.append(_mk_item(candidate.title, candidate.body, cand_new_body, is_new=False))

    if include_index:
        index_page = ab.find_page_by_title(INDEX_TITLE, scope)
        # Include this changeset's own item titles — nothing is written yet,
        # so without this the index diff shown for review would omit the
        # very pages the user is about to approve.
        new_index_body = _index_body(
            scope, tier, owner, extra_titles=[item.title for item in items]
        )
        items.append(
            _mk_item(
                INDEX_TITLE, index_page.body if index_page else "", new_index_body, not index_page
            )
        )

    cs = Changeset(
        id=new_id(),
        tier=tier,
        owner=owner,
        user_id=user.id,
        items=items,
        log_source_type=log_source_type,
        log_source_ref=log_source_ref,
        log_mode=log_mode,  # type: ignore[arg-type]
        source_ref_id=source_ref_id,
    )
    _changesets[cs.id] = cs
    return cs


def apply_changeset(changeset: Changeset, selected: set[int] | None, author_id: str) -> list[Page]:
    """Write every selected item's `new_body` verbatim (approve-all: pass
    `selected=None`; reject-all: pass an empty set — nothing is written).
    Each item was already fully drafted (including the team append block)
    at build time, so this is a plain write, not a re-derivation."""
    scope = make_partition_key(changeset.tier, changeset.owner)  # type: ignore[arg-type]
    written: list[Page] = []
    change_type = "merge_append" if changeset.tier == "team" else "ingest"
    for i, item in enumerate(changeset.items):
        if selected is not None and i not in selected:
            continue
        page = ab.find_page_by_title(item.title, scope)
        if page:
            page.body = item.new_body
            page.version += 1
        else:
            page = Page(
                partition_key=scope,
                title=item.title,
                body=item.new_body,
                tier=changeset.tier,  # type: ignore[arg-type]
                owner_id=changeset.owner,
            )
        if changeset.source_ref_id and changeset.source_ref_id not in page.source_refs:
            page.source_refs.append(changeset.source_ref_id)
        page.links_out = resolve_links(page.body, scope)
        written.append(ab.write_page(page, change_type=change_type, author_id=author_id))
    _changesets.pop(changeset.id, None)
    return written


# --------------------------------------------------------------------- export


def export_wiki(tier: str, owner: str) -> bytes:
    """Build spec: "A `export_wiki()` operation that produces a downloadable
    archive of the markdown files plus a manifest (links, metadata, source
    refs)." One markdown file per page (readable in any editor, matching the
    blob-storage discipline rule) plus a single `manifest.json` — the
    user-facing backup/portability story, cheap since blob already holds
    files in this shape."""
    scope = make_partition_key(tier, owner)  # type: ignore[arg-type]
    manifest: dict = {
        "tier": tier,
        "owner": owner,
        "exported_at": now_iso(),
        "pages": [],
    }
    buf = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for summary in ab.list_pages(scope):
            page = ab.read_page(summary.id, scope)
            if not page:
                continue
            filename = f"{page.slug or new_id()}.md"
            if filename in used_names:  # slug collision guard — keeps the archive valid
                filename = f"{page.slug}-{page.id[:8]}.md"
            used_names.add(filename)
            zf.writestr(filename, page.body)
            manifest["pages"].append(
                {
                    "id": page.id,
                    "title": page.title,
                    "filename": filename,
                    "tier": page.tier,
                    "created_at": page.created_at,
                    "updated_at": page.updated_at,
                    "version": page.version,
                    "links_out": page.links_out,
                    "source_refs": page.source_refs,
                }
            )
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buf.getvalue()
