"""Team-tier derived current-view pages (build spec: "Derived current-view
pages" — resolves the append-only readability problem: a team page pushed
to repeatedly becomes a dated chronological pile, closer to a RAG failure
mode than a maintained wiki).

- **The append log is the immutable substrate.** It's the team `Page` body
  itself (see `app.wiki.append_to_team_page`) — never touched here.
- **A derived "current view" sits on top, per topic, and IS rewritten**:
  regenerated wholesale from the log each time, resolving what's superseded
  and folding in newer content. Fully disposable — deleting one and
  regenerating it loses nothing, since it's never the source of truth.

Stored as a non-indexed blob (`team/{team_id}/_derived/{slug}.md`), the same
pattern `app.lint`'s review queue already uses for app-metadata that has no
vector-search or structured-query need of its own — not a Cosmos-indexed
`Page`, so a derived view is structurally incapable of being mistaken for an
authored contribution. `blob_store`'s "_"-prefix exclusion (see
`list_page_paths`) already keeps both this and the lint queue out of the
normal page listing. Per the abstraction-layer rule this bypasses `app
.abstractions.write_page` deliberately, for the same reason `app.lint`
already does for its queue: this isn't a `Page` in the Cosmos-index sense,
so routing it through `write_page` would wrongly enroll it in page
indexing/embedding/version-history machinery built for authored content.

**Narrower than spec, flagged**: "regenerated on a schedule, alongside
lint" assumes a background scheduler this POC doesn't have (see README —
lint itself is already on-demand only for the same reason); regeneration
here is triggered by an on-demand team-tier lint run, or the explicit
"Regenerate now" affordance on a page. The correction-entry off-cycle-regen
and pin-a-fragment mechanisms described in the build spec are not built —
out of scope for this pass, tracked in the README.
"""

from app import abstractions as ab
from app import blob_store
from app.models import Page, now_iso, slugify

SYNTHESIS_SYSTEM = (
    "You maintain the readable current-view synthesis of a team knowledge wiki "
    "page whose underlying content is an append-only, multi-contributor log. Read "
    "the full log below and produce a single, current, well-organized markdown "
    "synthesis of what the team now knows about this topic: resolve what's "
    "superseded by newer entries, fold in newer content, and use [[wikilinks]] "
    "for any other concept the log mentions. Where the log contains an "
    "unresolved conflict note (a pushed-content conflict flag), do NOT silently "
    "smooth it over — surface it explicitly as its own line starting with "
    "'> **Contested:**'. Output only the markdown body — no title line, no "
    "frontmatter."
)


def derived_view_path(team_id: str, slug: str) -> str:
    return f"team/{team_id}/_derived/{slug}.md"


def _frontmatter(log_page: Page) -> str:
    title_escaped = log_page.title.replace('"', '\\"')
    return (
        "---\n"
        "machine_generated: true\n"
        f"derived_from_page_id: {log_page.id}\n"
        f'derived_from_title: "{title_escaped}"\n'
        f"regenerated_at: {now_iso()}\n"
        "---\n\n"
        f"_Machine-generated synthesis of **[[{log_page.title}]]**'s append log — "
        "disposable, regenerable at any time, and never the source of truth. "
        f"[View the full log and its provenance](/team/{log_page.owner_id}/page/"
        f"{log_page.id})._\n\n"
    )


def regenerate_derived_view(log_page: Page, schema_ctx: str = "") -> str:
    """Regenerate one page's derived view from its append log, write it, and
    return the blob path it was written to. Safe to call any time — a
    derived view can be deleted and regenerated with no loss, since it's
    computed wholesale from the log every time, never incrementally."""
    body = ab.call_model(
        log_page.body,
        system=SYNTHESIS_SYSTEM + ("\n\n" + schema_ctx if schema_ctx else ""),
        max_tokens=2000,
    )
    slug = log_page.slug or slugify(log_page.title)
    path = derived_view_path(log_page.owner_id, slug)
    blob_store.write_text(path, _frontmatter(log_page) + body)
    return path


def read_derived_view(team_id: str, slug: str) -> str | None:
    return blob_store.read_text(derived_view_path(team_id, slug))


def regenerate_all(pages: list[Page], schema_ctx: str = "") -> int:
    """Regenerate derived views for every given (full-body) team page.
    Build spec: "regenerated on a schedule (alongside lint)" — the entry
    point for that is a team-tier lint run (see app.lint.run_lint), since
    this POC has no background scheduler to drive an independent cadence."""
    count = 0
    for page in pages:
        if page.body.strip():
            regenerate_derived_view(page, schema_ctx)
            count += 1
    return count
