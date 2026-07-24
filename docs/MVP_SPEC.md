# MVP Spec — Document Corpus → Policy Area Wiki + Map

> **Partly superseded — read `FRAMEWORK.md` and `MODEL_SPEC.md` first.**
>
> This document remains the accurate record of the pipeline that is **built and running**,
> and the wiki layer it describes (ingest, concepts, consolidation, page synthesis,
> provenance) stands. Sections marked **[SUPERSEDED]** below are the structure and
> evaluation layers, which the first real corpus run showed do not do the job: the output
> is ~270 accurate pages from which nothing can be predicted.
>
> The diagnosis and the replacement design are in `FRAMEWORK.md` (what we are building and
> why) and `MODEL_SPEC.md` (how). Nothing here should be built on without checking those.

**This is a fork of `BUILD_SPEC.md`, not a replacement.** It is the first slice of the full product: a one-shot pipeline that takes a team's existing documents and produces a navigable wiki, a community map, and a predicted knowledge frontier. `BUILD_SPEC.md` remains the target architecture; nothing here contradicts it, but most of it is deferred.

## What this tests

The full product rests on one unproven premise: **that agent-generated synthesis of a knowledge domain is actually good.** If you hand it a real corpus and the pages read as generic summary, or the communities carve the domain badly, or the frontier suggests plausible-sounding nonsense, nothing downstream matters. This MVP answers that question and nothing else.

Two consequences of that framing:

- **It does not test compounding.** A one-shot conversion cannot demonstrate the thing that actually differentiates this product. A good demo here should not be over-read as validating the full concept.
- **It requires nobody to change how they work.** Users hand over documents they already have and get something back. Zero adoption cost, which matters for the internal pitch.

**Framing risk to manage deliberately**: a document-to-wiki converter demos well and is a substantially less interesting product than a self-maintaining knowledge base. If the pitch lands on the converter, that is what people will expect and ask for. Present it from the start as the first slice of something that compounds.

## Pipeline

Six stages. The ordering matters — in particular, the page set must be known before any page is written, or the model invents `[[links]]` to pages that do not exist.

### Stage 1 — Ingest
- Store every source document verbatim and immutably (`sources/` in blob), exactly as in the full spec.
- Extract text via the existing extractors (PDF, docx, pptx, xlsx/CSV, text/markdown, URL).
- No summarization, no page creation at this stage. This is purely getting text on disk with provenance intact.

### Stage 2 — Concept extraction (document by document)
- One pass **per document**, independently. Each pass reads a single document in full and outputs a structured list of the concepts/entities/topics it covers, with a one-line description of each and the passages that evidence it.
- Document-by-document is deliberate: it preserves context clarity per source, parallelizes trivially, and keeps each call small and cheap.
- Output is per-document concept lists, not wiki pages.

### Stage 3 — Consolidation into the page set
This is the stage that determines whether the output is a wiki or a filing cabinet. **Twenty documents do not map to twenty pages.** A concept appearing across six documents becomes one page those six feed, rather than being restated six times.

- Merge all per-document concept lists into a single canonical page set: page title, description, and the list of source documents (with passage refs) that feed it.
- **Deduplication is the hard part**: "infant feeding" / "feeding in infancy" / "breastfeeding" may be the same page, or may be three legitimately distinct pages. Approach: embed concept names + descriptions, cluster near-duplicates by similarity, and use one bounded model call per cluster to decide whether to merge and pick the canonical name. This is a legitimate use of embeddings — it is a *pipeline* step, not a visualization input (see the standing rule that no visual is embedding-driven).
- Output: the definitive page set, fixed before any page is written.

**Revised after the first real run (13 UK cancer policy documents).** The original design ran hierarchical model-driven consolidation *first* — batching concept lists into fixed-size groups, consolidating each, recursing — with embedding dedup as a second pass to catch what crossed batch boundaries. On a real corpus it barely merged anything: 460 concepts became 434 pages, only 18 of them fed by more than one document, at a cost of 298 model calls.

The cause was the batching. Concepts were sorted by name before batching, so a batch of eight contained alphabetical neighbours, not semantic ones — "Access to treatment" and "Treatment delays" can never be compared if they sit hundreds of entries apart. The model was repeatedly asked to merge sets of unrelated things and correctly declined.

So the order is inverted: **cluster semantically first, then make one judgment call per cluster.** Exact-name folding runs before both, since it is free. Two further corrections from the same run: a cluster resolves into *groups* rather than a merge/don't-merge boolean, because a cluster can legitimately hold two distinct subjects; and a group whose members match none of the inputs is discarded rather than kept — the original produced 30 pages with no source documents and no passages at all, which would have been synthesized from nothing.

### Stage 4 — Page writing (page by page, full context)
- One pass **per page**, synthesizing **all** source material that feeds that page in a single call — not one pass per document repeatedly revising the same page.
- Rationale: a page written once with every relevant source in view is both cheaper and substantially better than the same page rewritten twenty times as documents arrive. Re-writing per document produces a pile of revisions, not a wiki.
- Each page may only `[[link]]` to pages in the known page set from Stage 3.
- Every page carries its source refs in frontmatter, linking back to the immutable documents that fed it. **Provenance is non-negotiable** — in a one-shot synthesis it is the only way a reader can check the output is honest.

### Stage 5 — Link graph → communities → map

> **[SUPERSEDED as the structure layer]** — built and working as described, but it does not
> produce the structure a reader needs. Pages from the same source document link densely, so
> the link graph encodes **provenance**, not substance, and the communities came out roughly
> 1:1 with the input documents. Filing by which document something came from is the novice
> sort (`FRAMEWORK.md` §"Chunks"); the replacement derives top-level structure from recurring
> organising principles instead (`MODEL_SPEC.md` §2).
>
> Community *naming* is separately and straightforwardly broken: `app/knowledge_map.py:89`
> names each community from a 12-title sample and typically echoes back one member's title.
> A ~65-page group covering waiting times, workforce, MDTs, radiotherapy and palliative care
> was labelled "Nutrition and lifestyle support across cancer care". That is a bug, fixable
> independently of any redesign, and part of why the output reads as document summaries.

- Build the undirected graph from the resolved `[[links]]` across the page set.
- Run community detection (Leiden or Louvain, seeded for stability) to find densely-interlinked neighbourhoods.
- One model call names each community ("Health economics", not "Community 3").
- Render the constrained radial map: central hub → community nodes → member-page satellites, polar placement per community angle slice. Descriptive and navigable — click a node to explore, click through to a page. **Not manipulable**: no drag-to-merge, no gesture-driven restructuring.
- Orphan pages (no links) are surfaced as unplaceable rather than forced into a community.

### Stage 6 — Frontier + contents + lint
- **Frontier**: one model call over the completed wiki predicting territory the corpus implies but does not cover. Rendered as visually-distinct greyed nodes alongside real ones. **Never enters the wiki, index, search, or export** — map layer only.
- **Contents page**: plain catalogue of page links grouped by community. No LLM prose summary. Unlinked pages under a trailing section. **[SUPERSEDED]** — the grouping inherits Stage 5's problem above, and the community names render as `## headings` (`app/wiki.py:194`), so the contents page presents provenance clusters as if they were the domain's themes.
- **Lint**: a final pass across the finished wiki looking for contradictions and superseded claims. Keep this in the MVP — a corpus spanning several years will contain documents that disagree with each other, and surfacing that on day one is one of the more striking things the tool can show a team about their own material. It is not a slow-decay feature here; it is immediate. **Flags only, no auto-fix.**

## Write semantics (decided during build planning)

The MVP's pages are **not append-only**, and this is not a relaxation of the team-tier rule — it is a recognition that the rule guards against a problem the MVP does not have.

Append-only exists to solve one specific problem: multiple people pushing content onto a shared page, where the agent silently rewriting someone else's contribution would destroy attribution and trust. The MVP has no push, no contributors, and no shared-authorship problem. Pages are written once by the agent from documents. There is nothing to append to and nobody's work to protect.

Concretely for Stage 4: pages are written **wholesale, in full, in one pass**. No log structure, no derived-view layer, no bottom-appending. Just a page.

**Adding documents to an already-processed corpus is full regeneration.** Re-run the pipeline over the whole corpus; every page is rewritten. Simple, deterministic, and it keeps the edit-vs-append distinction out of the MVP entirely. It costs a full pass and discards any human corrections, which is acceptable for a pipeline whose purpose is testing synthesis quality — if you want different output, you re-run it. Version history covers it either way, since each regeneration is just a new version. Incremental ingest is the full product's job.

**Graduation path.** In the full product a team page is an append log of human contributions plus a derived synthesis on top. MVP pages are agent-synthesized from documents, which structurally is a *derived view*, not a log entry. So when this grows into the full product, the MVP output becomes the initial derived view and the append log starts empty above it and accumulates human pushes. Nothing has to be restructured.

## Scale handling

The pipeline must work at hundreds of documents, not a handful. The constraint is context, and the shape of the answer is the same as a deep-research agent: **never pass whole documents where extracts will do.**

- **Stage 2** is naturally bounded — one document per call. Parallelize with a concurrency cap.
- **Stage 3 cannot consolidate hundreds of concept lists in one call.** Use hierarchical consolidation: batch the per-document lists into groups, consolidate each group, then consolidate the group outputs. Balanced and order-independent, unlike a running incremental merge (which is cheaper but produces order-dependent results — the first document disproportionately shapes the page set).
- **Stage 4 may exceed context when a concept appears in many documents.** Feed the *relevant extracted passages* for that page (captured in Stage 2), not the full source documents. If passages alone still overflow, summarize per-source first, then synthesize the page from those summaries — accepting the quality cost and recording it.
- Cost discipline from the full spec applies throughout: narrow before invoking the model, prefer structured outputs, route deterministic work through code, keep the stable prompt prefix byte-identical for cache reuse.

## Bootstrap trigger

A check, not a mode: **scope empty → run the pipeline; scope has content → normal operation.** The check is on the *scope* (team or corpus), not the user, so a later joiner on a populated corpus lands on the existing map rather than being asked for documents that already exist.

## What is reused vs. new

**Already built, reused as-is**: ingest extractors, blob/Cosmos storage split, page model and version history, `[[link]]` resolution and rendering, backlinks ("Linked from"), changeset machinery, lint's mechanical checks, export, auth.

**New for the MVP**: Stage 2 concept extraction, Stage 3 consolidation and dedup, Stage 4 page-by-page synthesis (replacing per-document ingest as the write path), community detection and naming, the radial map view, the frontier, the community-grouped contents page, and the bootstrap check.

**Changed**: the contents page becomes a deterministic community-grouped catalogue rather than LLM prose.

## Explicitly out of scope for the MVP

Deferred to the full spec, not cut from the product:

- Team tier / individual tier split, and the push/merge mechanism
- Manual vs. automatic ingest modes, and the lint mode toggle
- Working conversation with the agent, and query-as-write
- Chat history
- Derived current-view pages and the append-only team substrate
- Schema doc co-evolution (a fixed default is fine here)
- Visible agent work (recent-change surfacing, per-page activity, fading highlights)
- Correction-of-existing-content as an input path
- Selection-based interaction (highlight-to-ask, inline contradiction marks, lineage on hover)
- Dispatched background agents

## Evaluation

**This matters more than any implementation detail in this document.** The MVP exists to answer a question, and the answer requires someone who knows the domain.

**First pass — self-evaluation on a known corpus.** University economics material, including a health-and-education-economics dissertation that sits apart from the taught subdomains. This is a good test corpus for a specific reason: it contains both *overlapping subdomains* (which should cluster with genuine cross-links) and a *genuinely distinct area* (which should surface as its own region). A corpus of unrelated material would let the communities separate trivially and would not test Stage 3 at all.

**Choose density over breadth.** A narrower, deeper subset where subtopics genuinely overlap is a better test than everything available. The hard case is cross-cutting concepts — the thing a folder tree cannot represent and a link graph can.

**What to check, in order:**

> **[SUPERSEDED — items 1 and 3]** Both ask whether the carve is a good one. Neither asks
> whether a carve is the right output at all, and the first real run answered that: the
> pages were largely the right pages, the communities were defensible, and the artifact
> still supported no inference. The replacement criteria are in `MODEL_SPEC.md` §10, built
> around whether a reader can predict what the field would conclude about a case not in the
> corpus. Items 2, 4, 5 and 6 still stand as written.
>
> Item 6 in particular is worth keeping and strengthening: the contradiction detector is
> reused as the "genuine disagreement" input to the new design, and needs the scope fix in
> `MODEL_SPEC.md` §6 before it can be trusted at that job.

1. **Are the pages the right pages?** Does the page set carve the domain the way someone who knows it would? This is Stage 3, and it is the make-or-break stage.
2. **Is each page accurate and non-generic?** Does it say something specific, traceable to its sources, that a summary of any one document would not?
3. **Do the communities carve sensibly?** Do they correspond to how a practitioner would divide the area?
4. **Do cross-cutting concepts survive?** Does a concept spanning several subdomains appear once with links out, rather than being duplicated or forced into one community?
5. **Is the frontier real?** Does it suggest territory that genuinely exists and is genuinely missing — or plausible-sounding nonsense?
6. **Does lint find real contradictions?** Not artifacts of synthesis, but actual disagreements between sources.

**Second pass — domain evaluation by a policy team.** Only after the first pass. Decide in advance who evaluates it and what they are asked, since that shapes what the output needs to show.

## Open items

- Whether Leiden or Louvain — Leiden gives better-connected communities and is worth preferring if the dependency is available cleanly; either is acceptable, seeded for stability. **Resolved for now**: staying on networkx's native Louvain (already built, seeded, tested). Leiden needs `igraph` + `leidenalg`, a compiled dependency; revisit only if evaluation shows communities carving badly.
- Whether query (ask-the-wiki) is worth including in the MVP. It is built already, so the cost is near zero, but it is not needed to answer the MVP's question. Include if it helps the demo, cut if it distracts from the wiki-and-map output. **Resolved for now**: left reachable but unfeatured.
- Community *names* will churn slightly between regenerations as membership shifts. Expected, not a bug — worth noting in the UI rather than engineering around.
