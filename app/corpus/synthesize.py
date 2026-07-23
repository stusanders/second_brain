"""Stage 4 — page writing, page by page with full context (MVP spec).

One pass per page, synthesizing ALL source material that feeds the page in a
single call — never one pass per document revising the same page repeatedly
(that produces a pile of revisions, not a wiki). The page set is already
fixed (Stage 3), so every page knows the complete list of titles it may
[[link]] to; link enforcement is code, not prompt — any invented link is
unwrapped to plain text before the page is written.

Context discipline: pages are fed the *extracted passages* captured in Stage
2, not whole documents. If passages alone overflow the budget, each large
source is pre-summarized first and the page synthesized from those summaries
— accepting the quality cost and recording it visibly on the page."""

from collections.abc import Callable

from app import abstractions as ab
from app import wiki
from app.corpus import artifacts
from app.models import Page, User, make_partition_key

SYNTH_SYSTEM = (
    "You are writing one page of a knowledge wiki synthesized from a corpus "
    "of source documents. Write the page for the given concept using ONLY the "
    "source passages provided — no outside knowledge, no invented facts. "
    "Synthesize ACROSS the sources: say what emerges from reading them "
    "together (agreements, tensions, complementary detail), specific enough "
    "to be traceable to the passages — never a generic summary of any one "
    "document. Where another wiki page from the provided list of valid page "
    "titles is genuinely relevant, reference it inline as [[Exact Title]]; "
    "link only titles from that list. Output clean markdown: no preamble, no "
    "title heading (the title is stored separately), no source list at the "
    "end (sources are tracked separately)."
)

SOURCE_SUMMARY_SYSTEM = (
    "Condense the following extracted passages from one source document into "
    "a dense factual summary (under 250 words) preserving specific claims, "
    "numbers, and terminology. Output only the summary."
)

# A single source's passage block bigger than this gets pre-summarized when
# the page's total context overflows the budget.
LARGE_SOURCE_CHARS = 3000


def _source_block(src: dict) -> str:
    passages = "\n".join(f"> {p}" for p in src.get("passages", []))
    return f"### Source: {src.get('source_label', src['source_ref_id'])}\n{passages}"


def _fit_sources(sources: list[dict], budget: int) -> tuple[list[str], bool]:
    """Source blocks within the char budget. If over, pre-summarize the large
    sources (one model call each) and rebuild; returns (blocks, summarized)."""
    blocks = [_source_block(s) for s in sources]
    if sum(len(b) for b in blocks) <= budget:
        return blocks, False

    condensed = []
    for src, block in zip(sources, blocks, strict=True):
        if len(block) > LARGE_SOURCE_CHARS:
            summary = ab.call_model(
                "\n".join(src.get("passages", [])),
                system=SOURCE_SUMMARY_SYSTEM,
                max_tokens=600,
                deployment=artifacts.pipeline_deployment(),
            )
            label = src.get("source_label", src["source_ref_id"])
            condensed.append(f"### Source (pre-summarized): {label}\n{summary}")
        else:
            condensed.append(block)
    # Hard cap regardless — drop trailing blocks past the budget.
    kept, used = [], 0
    for b in condensed:
        if used + len(b) > budget:
            break
        kept.append(b)
        used += len(b)
    return kept or condensed[:1], True


def _strip_unknown_links(body: str, title_to_id: dict[str, str]) -> str:
    """Unwrap any [[Title]] not in the page set — models still occasionally
    invent links; the spec's 'only link known pages' rule is enforced here."""
    known = {t.lower() for t in title_to_id}

    def _replace(match):
        title = match.group(1).strip()
        return match.group(0) if title.lower() in known else title

    return wiki.WIKILINK_RE.sub(_replace, body)


def write_pages(
    pageset: dict,
    tier: str,
    owner: str,
    user: User,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[Page], list[dict]]:
    """Stage 4 over the fixed page set. Returns (written pages, errors);
    per-page failures are recorded and the run continues."""
    from app.config import get_settings

    budget = get_settings().corpus_max_page_chars
    pages_spec = pageset["pages"]
    title_to_id = {p["title"]: p["page_id"] for p in pages_spec}
    titles_line = "; ".join(sorted(title_to_id))
    scope = make_partition_key(tier, owner)  # type: ignore[arg-type]

    written: list[Page] = []
    errors: list[dict] = []
    for i, spec in enumerate(pages_spec):
        try:
            blocks, summarized = _fit_sources(spec["sources"], budget)
            prompt = (
                f"Page title: {spec['title']}\n"
                f"Page scope: {spec['description']}\n\n"
                f"Valid page titles for [[links]]: {titles_line}\n\n"
                "Source passages:\n\n" + "\n\n".join(blocks)
            )
            body = ab.call_model(
                prompt,
                system=SYNTH_SYSTEM,
                max_tokens=2500,
                deployment=artifacts.pipeline_deployment(),
            ).strip()
            body = _strip_unknown_links(body, title_to_id)
            if summarized:
                body += "\n\n_Some sources were pre-summarized due to volume._"
            page = Page(
                id=spec["page_id"],
                partition_key=scope,
                title=spec["title"],
                body=body,
                tier=tier,  # type: ignore[arg-type]
                owner_id=owner,
                source_refs=[s["source_ref_id"] for s in spec["sources"]],
                links_out=wiki.resolve_links_local(body, title_to_id),
            )
            written.append(ab.write_page(page, change_type="ingest", author_id=user.id))
        except Exception as err:  # noqa: BLE001 — recorded per-page, run continues
            errors.append({"title": spec["title"], "error": str(err)})
        if progress:
            progress(i + 1, len(pages_spec))
    return written, errors
