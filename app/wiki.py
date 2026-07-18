"""Wiki page services: wikilink resolution, index maintenance, edit rules.

Individual tier: pages are editable in place (incremental refinement).
Team tier: append-only, no exceptions — enforced here, not by prompt.
"""

import difflib
import re

from app import abstractions as ab
from app.models import Page, User, make_partition_key, now_iso

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)\]\]")
INDEX_TITLE = "Index"


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
    block_lines = [f"\n\n---\n\n### Pushed by {user.name} — {now_iso()[:10]}\n"]
    if conflict_note:
        block_lines.append(f"> **Note:** {conflict_note}\n")
    block_lines.append(content.strip())
    block = "\n".join(block_lines)

    page = ab.find_page_by_title(title, scope)
    if page:
        page.body = page.body + block  # append only — never rewrite
        page.version += 1
    else:
        page = Page(
            partition_key=scope,
            title=title,
            body=block.lstrip("\n-— "),
            tier="team",
            owner_id=team_id,
        )
    if source_ref_id and source_ref_id not in page.source_refs:
        page.source_refs.append(source_ref_id)
    page.links_out = resolve_links(page.body, scope)
    return ab.write_page(page, change_type="merge_append", author_id=user.id)


def regenerate_index(scope: str, tier: str, owner: str, author_id: str) -> Page:
    """Rebuild the generated contents page, catalogued by category via a
    short structured model call."""
    pages = [p for p in ab.list_pages(scope) if p.title != INDEX_TITLE]
    if not pages:
        body = "_No pages yet._"
    else:
        titles = "\n".join(f"- {p.title}" for p in pages)
        body = ab.call_model(
            "Organize these wiki page titles into a markdown contents page grouped "
            "under a few sensible category headings. Link each title as [[Title]]. "
            "Output only the markdown, no preamble.\n\n" + titles,
            system="You maintain a personal knowledge wiki's index page.",
            max_tokens=1200,
        )
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
