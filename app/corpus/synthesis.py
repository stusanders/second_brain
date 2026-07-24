"""Stage 4 — writing the pages.

One pass **per page**, synthesizing all the source material that feeds that
page in a single call. Not one pass per document repeatedly revising the same
page: a page written once with every relevant source in view is both cheaper
and substantially better than the same page rewritten twenty times as
documents arrive. Rewriting per document produces a pile of revisions, not a
wiki — which is precisely what `app.ingest.pipeline.ingest_automatic` does, and
why this replaces it as the corpus write path rather than extending it.

Three constraints this module enforces rather than merely requests:

- **Links stay inside the page set.** The page set is fixed by Stage 3 before
  anything is written, so every legal link target is known. The prompt says so
  and a deterministic post-process strips anything else — an invariant code can
  check should never be left to the model.
- **Provenance travels with the page.** `source_refs` in frontmatter points at
  the immutable raw documents. In a one-shot synthesis that is the only way a
  reader can check the output is honest.
- **Pages are written wholesale.** No append blocks, no log structure. The
  append-only rule protects attribution when several people push onto a shared
  page; here the agent writes each page once from documents, so there is
  nothing to append to and nobody's work to protect (docs/MVP_SPEC.md, "Write
  semantics").
"""

from app import abstractions as ab
from app import lint
from app.corpus.models import PageSpec
from app.models import WIKILINK_RE, Page, make_partition_key

# Passage budget for one synthesis call. Beyond this the page is written from
# per-source summaries instead and flagged as degraded.
MAX_PASSAGE_CHARS = 40_000
MAX_SUMMARY_CHARS_PER_SOURCE = 4_000

PAGE_SYSTEM = (
    "You are writing one page of a knowledge wiki, synthesizing every source "
    "that covers its subject.\n\n"
    "Write what the sources collectively establish about this subject: the "
    "specific facts, figures, findings, definitions and open questions. Be "
    "concrete and traceable — a reader should learn something no single source "
    "summary would have told them. Do not pad, do not restate the subject's "
    "name back at the reader, and do not invent anything the passages do not "
    "support.\n\n"
    "Where the sources disagree, say so explicitly and attribute both claims "
    "rather than smoothing the disagreement away.\n\n"
    "Link to related pages with [[Page Title]], using ONLY titles from the "
    "provided list of existing pages. Never invent a link target.\n\n"
    "Output the page body in markdown. No title heading — the title is already "
    "known. No preamble."
)

SUMMARIZE_SYSTEM = (
    "Summarize what this source says about the given subject: key facts, "
    "figures and findings only, no preamble. Be faithful and specific."
)

CONTRADICTION_SYSTEM = (
    "You are checking whether sources that cover the same subject disagree "
    "with each other on a matter of fact — different figures for the same "
    "quantity, incompatible claims, or a position later reversed.\n\n"
    "Report only genuine factual disagreements between the sources. Differences "
    "of emphasis, scope or wording are not disagreements. If there are none, "
    "say so.\n\n"
    'Reply with JSON only: {"contradictions": [{"summary": "one line", '
    '"detail": "what each source claims, naming them"}]}'
)


def _link_targets(page_set: list[PageSpec]) -> dict[str, str]:
    """Lowercased title -> canonical title, for the link guard."""
    return {p.title.lower(): p.title for p in page_set}


def strip_unknown_links(body: str, targets: dict[str, str]) -> str:
    """Remove [[links]] pointing outside the page set, and normalise the casing
    of the ones that survive so they resolve.

    Stage 3 fixes the page set precisely so this is decidable. A link to a page
    that will never exist is a dead end for the reader and a phantom node in
    the map, and the model will produce them however firmly it is asked not to.
    """

    def replace(match) -> str:
        title = match.group(1).strip()
        canonical = targets.get(title.lower())
        return f"[[{canonical}]]" if canonical else title

    return WIKILINK_RE.sub(replace, body)


def _passage_block(spec: PageSpec) -> str:
    return "\n\n".join(f"[from {p.doc_name or p.doc_ref}]\n{p.text}" for p in spec.passages)


def _summarized_block(spec: PageSpec) -> tuple[str, int]:
    """Overflow path: summarize per contributing source first, then synthesize
    the page from those summaries. Accepts a quality cost, which is why the
    caller records it — but degrading beats failing on a large corpus."""
    by_source: dict[str, list[str]] = {}
    for passage in spec.passages:
        by_source.setdefault(passage.doc_name or passage.doc_ref, []).append(passage.text)

    parts, calls = [], 0
    for source, texts in by_source.items():
        joined = "\n\n".join(texts)[:MAX_SUMMARY_CHARS_PER_SOURCE]
        summary = ab.call_model(
            f"Subject: {spec.title}", context=joined, system=SUMMARIZE_SYSTEM, max_tokens=500
        )
        calls += 1
        parts.append(f"[from {source}, summarized]\n{summary.strip()}")
    return "\n\n".join(parts), calls


def _find_contradictions(spec: PageSpec, source_block: str) -> tuple[list[dict], int]:
    """Stage 4 is the only point in the pipeline where every source for a
    subject is in view at once, which makes it the only place a genuine
    disagreement *between sources* can be spotted. Lint's own contradiction
    check compares finished pages against each other — by which point this
    synthesis may already have reconciled the disagreement away.
    """
    if len({p.doc_ref for p in spec.passages}) < 2:
        return [], 0  # a single source cannot disagree with itself
    payload = ab.call_model_json(
        f"Subject: {spec.title}", context=source_block, system=CONTRADICTION_SYSTEM
    )
    found = [c for c in (payload.get("contradictions") or []) if isinstance(c, dict)]
    return found, 1


def write_page(spec: PageSpec, page_set: list[PageSpec], tier: str, owner: str) -> tuple[Page, int]:
    """Synthesize and write one page. Returns the written page and the number
    of model calls made."""
    scope = make_partition_key(tier, owner)  # type: ignore[arg-type]
    targets = _link_targets(page_set)
    calls = 0

    source_block = _passage_block(spec)
    if len(source_block) > MAX_PASSAGE_CHARS:
        source_block, summary_calls = _summarized_block(spec)
        calls += summary_calls
        spec.degraded = True

    other_titles = [p.title for p in page_set if p.title != spec.title]
    body = ab.call_model(
        f"Subject: {spec.title}\n"
        f"What it is: {spec.description}\n\n"
        "Existing pages you may link to:\n" + "\n".join(f"- {t}" for t in other_titles),
        context=source_block,
        system=PAGE_SYSTEM,
    )
    calls += 1

    body = strip_unknown_links(body.strip(), targets)

    page = ab.find_page_by_title(spec.title, scope)
    if page:
        # Full regeneration: the page is rewritten, not appended to. Version
        # history keeps the previous pass.
        page.body = body
        page.version += 1
    else:
        page = Page(
            partition_key=scope,
            title=spec.title,
            body=body,
            tier=tier,  # type: ignore[arg-type]
            owner_id=owner,
        )
    page.source_refs = list(spec.source_refs)

    written = ab.write_page(page, change_type="ingest", author_id="system:corpus")
    return written, calls


def synthesize(
    page_set: list[PageSpec], tier: str, owner: str, progress=None
) -> tuple[list[Page], list[dict], int]:
    """Write every page in the set, then resolve links now that all pages
    exist. Returns (pages, contradiction findings, model calls).

    Links are resolved in a second pass because `links_out` maps titles to page
    *ids*, and a page written early cannot know the id of one written later.
    """
    pages: list[Page] = []
    contradictions: list[dict] = []
    calls = 0
    if progress:
        progress.start(total=len(page_set), label="pages written")

    for spec in page_set:
        # One page failing must not discard the whole run. By this point the
        # corpus has already cost hundreds of model calls, and losing all of it
        # because a single page tripped a token limit is indefensible — the
        # failure is recorded on the spec and the wiki is written without it.
        try:
            page, n = write_page(spec, page_set, tier, owner)
        except Exception as e:  # noqa: BLE001 — one page must not sink the run
            spec.error = f"{type(e).__name__}: {e}"
            if progress:
                progress.tick(label=f"skipped “{spec.title}” ({type(e).__name__})")
            continue
        calls += n
        pages.append(page)
        if progress:
            progress.tick(label=f"wrote “{page.title}”")

        try:
            found, n = _find_contradictions(spec, _passage_block(spec))
        except Exception:  # noqa: BLE001 — a by-product; never worth failing for
            found, n = [], 0
        calls += n
        for item in found:
            contradictions.append(
                {
                    "page_id": page.id,
                    "page_title": page.title,
                    "summary": str(item.get("summary", "")).strip(),
                    "detail": str(item.get("detail", "")).strip(),
                }
            )

    calls += _resolve_links(pages)
    return pages, contradictions, calls


def _resolve_links(pages: list[Page]) -> int:
    """Second pass: turn [[Title]] references into links_out page ids. The map
    is built entirely from links_out, so skipping this leaves every page an
    orphan.

    Only the index rows are rewritten, not the page bodies — the bodies already
    contain the right link text, and rewriting them would bump every version for
    no content change."""
    id_by_title = {p.title.lower(): p.id for p in pages}
    for page in pages:
        links = []
        for title in WIKILINK_RE.findall(page.body):
            target = id_by_title.get(title.strip().lower())
            if target and target != page.id and target not in links:
                links.append(target)
        if links != page.links_out:
            page.links_out = links
            ab.upsert_page_index(page)
    return 0


def record_contradictions(tier: str, owner: str, contradictions: list[dict]) -> None:
    """File Stage 4's findings into the existing lint queue, so the run ends
    with source-level and page-level disagreements in one place."""
    if not contradictions:
        return
    lint._append_findings(
        tier,
        owner,
        [
            lint.LintFinding(
                category="judgment",
                check_type="contradiction",
                summary=c["summary"] or f"Sources disagree on '{c['page_title']}'",
                detail=c["detail"],
                page_ids=[c["page_id"]],
                origin_mode="automatic",
            )
            for c in contradictions
        ],
    )
