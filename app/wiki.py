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
import zipfile
from dataclasses import asdict, dataclass, field

from app import abstractions as ab
from app import review_store
from app.models import (
    WIKILINK_RE,
    IngestMode,
    Page,
    User,
    make_partition_key,
    new_id,
    now_iso,
)

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
    return ab.write_page(
        page, change_type=change_type, author_id=user.id, source_ref=source_ref_id or ""
    )


def _team_append_block(content: str, author_name: str, conflict_note: str | None = None) -> str:
    """The dated, attributed section a push adds to a team page. Kept separate
    from the concatenation below so a changeset can carry the exact reviewed
    block and re-apply it to whatever the page body says at write time — see
    apply_changeset. Without that split, the body read at build time would be
    baked in and a concurrent push would be silently overwritten."""
    block_lines = [f"\n\n---\n\n### Pushed by {author_name} — {now_iso()[:10]}\n"]
    if conflict_note:
        block_lines.append(f"> **Note:** {conflict_note}\n")
    block_lines.append(content.strip())
    return "\n".join(block_lines)


def _team_append_body(
    existing_body: str, content: str, author_name: str, conflict_note: str | None = None
) -> str:
    """Compute the full new body for a team-page append, without writing
    anything — shared by append_to_team_page and build_changeset so a
    changeset item's `new_body` and the eventual write agree exactly."""
    block = _team_append_block(content, author_name, conflict_note)
    return _apply_append_block(existing_body, block)


def _apply_append_block(existing_body: str, block: str) -> str:
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
    return ab.write_page(
        page, change_type="merge_append", author_id=user.id, source_ref=source_ref_id or ""
    )


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


@dataclass
class HistoryEntry:
    version_number: int
    timestamp: str
    change_type: str
    author_id: str
    source_ref: str
    summary: str
    diff: str


def page_history(page_id: str, scope: str) -> list[HistoryEntry]:
    """The individual tier's read-side answer to "what did I know about X
    before repeated rewrites smoothed it over".

    The build spec accepts that contextual rewriting loses the chronological
    trail the team tier gets for free from its append log, and prescribes this
    as the fix: a derived history view, each version with its date, triggering
    source, and what changed. Computed on demand from the version snapshots
    already in blob — no second write path, no new storage.
    """
    versions = sorted(ab.list_versions(page_id, scope), key=lambda v: v.version_number)
    entries: list[HistoryEntry] = []
    previous = ""
    for version in versions:
        body = ab.read_page_body(version.blob_path) or ""
        entries.append(
            HistoryEntry(
                version_number=version.version_number,
                timestamp=version.timestamp,
                change_type=version.change_type,
                author_id=version.author_id,
                source_ref=version.source_ref,
                summary=_diff_summary(previous, body, is_new=not previous),
                diff=unified_diff(previous, body, f"v{version.version_number}"),
            )
        )
        previous = body
    entries.reverse()  # newest first, like every other history view in the app
    return entries


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
    # Team tier only: the append block on its own, so apply_changeset can
    # re-apply it to the page body as it stands at write time rather than to
    # the (possibly stale) body captured when the changeset was built.
    append_block: str | None = None


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


def _store_changeset(cs: Changeset) -> None:
    review_store.save("changesets", cs.user_id, cs.id, asdict(cs))


def get_changeset(changeset_id: str, user: User) -> Changeset | None:
    """Ownership is structural: the blob path is built from the caller's own
    user id, so another user's changeset is unreachable rather than merely
    rejected."""
    payload = review_store.load("changesets", user.id, changeset_id)
    if payload is None:
        return None
    items = [ChangesetItem(**item) for item in payload.pop("items", [])]
    return Changeset(items=items, **payload)


def discard_changeset(changeset_id: str, user_id: str) -> None:
    review_store.delete("changesets", user_id, changeset_id)


def _mk_item(
    title: str, old_body: str, new_body: str, is_new: bool, append_block: str | None = None
) -> ChangesetItem:
    return ChangesetItem(
        title=title,
        is_new=is_new,
        old_body=old_body,
        new_body=new_body,
        diff=unified_diff(old_body, new_body, title),
        summary=_diff_summary(old_body, new_body, is_new),
        append_block=append_block,
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
    primary_block = None
    if tier == "team":
        primary_block = _team_append_block(primary_body, user.name, conflict_note)
        new_body = _apply_append_block(existing.body if existing else "", primary_block)
    else:
        new_body = primary_body
    items.append(
        _mk_item(
            primary_title,
            existing.body if existing else "",
            new_body,
            not existing,
            append_block=primary_block,
        )
    )

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
                cand_block = _team_append_block(note, user.name)
                cand_new_body = _apply_append_block(candidate.body, cand_block)
            else:
                cand_block = None
                cand_new_body = ab.call_model(
                    f"New/updated page '{primary_title}':\n{primary_body[:3000]}\n\n"
                    f"Existing page to update, '{candidate.title}':\n{candidate.body}",
                    system=(schema_ctx + "\n\n" if schema_ctx else "") + CROSS_PAGE_DRAFT_SYSTEM,
                    max_tokens=2000,
                )
            items.append(
                _mk_item(
                    candidate.title,
                    candidate.body,
                    cand_new_body,
                    is_new=False,
                    append_block=cand_block,
                )
            )

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
    _store_changeset(cs)
    return cs


def apply_changeset(changeset: Changeset, selected: set[int] | None, author_id: str) -> list[Page]:
    """Write the selected items (approve-all: pass `selected=None`; reject-all:
    pass an empty set — nothing is written).

    Individual-tier items are written verbatim as drafted. Team-tier items are
    **re-derived under a blob lease**: the changeset carries the reviewed
    append block, and that block is re-applied to the page body as it stands at
    write time. A changeset is built before a human reviews it, so its captured
    body can be minutes or hours stale; writing `new_body` verbatim would
    silently drop anything appended in that window — exactly the lost-update
    the build spec's leasing rule exists to prevent. The append-only rule is
    what makes re-deriving safe: it is a re-read-and-re-append, never a merge.
    """
    scope = make_partition_key(changeset.tier, changeset.owner)  # type: ignore[arg-type]
    written: list[Page] = []
    change_type = "merge_append" if changeset.tier == "team" else "ingest"
    for i, item in enumerate(changeset.items):
        if selected is not None and i not in selected:
            continue
        written.append(_apply_item(changeset, item, scope, change_type, author_id))
    discard_changeset(changeset.id, changeset.user_id)
    return written


def _apply_item(
    changeset: Changeset, item: ChangesetItem, scope: str, change_type: str, author_id: str
) -> Page:
    needs_lease = changeset.tier == "team" and item.append_block is not None
    blob_path, lease_id = (
        ab.acquire_page_lease(changeset.tier, changeset.owner, item.title)
        if needs_lease
        else ("", None)
    )
    try:
        page = ab.find_page_by_title(item.title, scope)
        if page:
            # Re-append to the current body, not the one captured at build time.
            page.body = (
                _apply_append_block(page.body, item.append_block) if needs_lease else item.new_body
            )
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
        return ab.write_page(
            page,
            change_type=change_type,
            author_id=author_id,
            lease_id=lease_id,
            source_ref=changeset.source_ref_id or "",
        )
    finally:
        ab.release_page_lease(blob_path, lease_id)


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
