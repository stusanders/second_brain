# LLM Wiki — Build Specification

## What this is

A tool that lets a person build a persistent, AI-maintained knowledge wiki from documents they encounter while working — internal and external, including sensitive material. Individual wikis are private and personal. Selected pages can be pushed to a shared team wiki via an LLM-mediated merge step. This is a POC, built solo and fast, intended to be shown to the builder's team and eventually migrated onto their organisation's Microsoft 365 Copilot API once access is available.

Read the original LLM Wiki pattern doc for the philosophical grounding — this spec is the concrete instantiation of that pattern for this use case. (Note on attribution: the pattern doc is an unattributed idea file, not Karpathy's work. The separate concept borrowed from Karpathy is the **autonomy slider** — start at low autonomy, earn the right to loosen it, distinguish by stakes rather than task type — which informs the manual/automatic mode design and the trust bar for automatic mode.)

## Non-negotiable architectural principle: the abstraction layer

Every call to an external system must go through a narrow, named function — not called directly from business logic. This is what makes the eventual Copilot API migration a swap of internals, not a rewrite. Minimum required functions:

- `call_model(prompt, context) -> response` — currently routes to Azure OpenAI (EU) for both tiers; will route to Copilot API's model layer at migration.
- `get_context(query, scope) -> content` — currently reads markdown from Blob Storage, located via the Cosmos index; will route to Retrieval/Context API at migration.
- `search(query, scope) -> results` — currently Cosmos vector search returning page ids, bodies then read from blob; will route to Search API at migration.
- `write_page(content, target) -> result` — currently writes markdown to Blob Storage **then** updates the Cosmos index (in that order — see storage model); will route to Work IQ Tools API at migration.
- `embed(text) -> vector` — currently Azure OpenAI embeddings endpoint.

Do not call Azure OpenAI, Cosmos DB, or Blob Storage SDKs directly from ingest/query/merge logic. Route everything through these functions.

## Deployment shape

Both tiers are deployed as **one containerized web app on Azure** (single deployment, two modes/views: personal workspace and team workspace), not a local app and not separate deployments. There is no offline mode — the app requires connectivity to be reached at all, so no local storage, no local embedding fallback, no local SQLite.

## Data residency (hard requirement)

EU/UK data residency is a hard requirement, not a preference. Consequences for model routing:

- **Both tiers' model calls** → Azure OpenAI, deployed in an EU region. One provider, one cloud, no cross-cloud hop. This stands in for Copilot API's model routing at the POC stage.
- No AWS/Bedrock dependency in this design — earlier drafts used Bedrock for the individual tier on the assumption it would run locally/cross-cloud; that rationale no longer applies now that both tiers are hosted on Azure, so it's been removed.
- Flag at migration time: Anthropic models are explicitly out of scope for Microsoft's EU Data Boundary commitment. If the production Copilot API deployment routes any call through Claude specifically, the EU residency guarantee does not automatically apply the way it does for Microsoft's own models. This needs verification with the tenant admin / compliance at migration time. Not a current concern since this design doesn't use Claude, but worth knowing in case Copilot API's own routing introduces it later.

## Storage model: markdown canonical, index rebuildable

**Azure Blob Storage holds the canonical content. Cosmos DB is a derived index over it, rebuildable from scratch by re-reading blob.** This inversion is deliberate — it preserves the original pattern's "the wiki is just a folder of markdown files" portability property, which a database-canonical design loses. If Cosmos is wiped, it can be rebuilt; if blob is wiped, the wiki is gone.

Three stores:

### 1. Blob Storage — canonical wiki content
- Page bodies as plain `.md` files, one file per page, in a path structure that encodes tier and owner (e.g. `individual/{user_id}/{page_slug}.md`, `team/{team_id}/{page_slug}.md`).
- **Discipline rule**: no app-specific structure may leak into page bodies. A page file must be readable and useful in any markdown editor with no knowledge of this app. Metadata that isn't natural markdown (ids, embeddings, version pointers) lives in the index, not in the file. YAML frontmatter for genuinely document-level metadata (title, tags, date, source refs) is acceptable — it's a markdown-native convention.
- Version snapshots also live here (e.g. `.../{page_slug}/v{n}.md`), full snapshots not deltas; diffs computed on demand at display time.

### 2. Blob Storage — immutable raw source store
- Every ingested source is stored verbatim, unmodified, never edited or deleted — the original PDF/docx/extracted-article-text as received.
- Every wiki page links back to the raw sources that contributed to it. **Provenance is a hard requirement, not a nice-to-have**: in an org context, "where did this claim come from" must always be answerable, and a synthesized page whose sources can't be traced is untrustworthy by default.
- This is the original pattern's "raw sources are immutable, the LLM reads from them but never modifies them" layer, made explicit.

### 3. Cosmos DB — derived index (rebuildable)
Holds only what files are bad at: vector similarity and structured queries. Every record here can be regenerated from blob content.
- **PageIndex** — page_id, title, slug, blob_path, tier, owner_id/team_id, created_at, updated_at, links_out (page ids referenced), source_refs (blob paths of contributing raw sources), current_version.
- **VersionIndex** — page_id, version_number, blob_path, timestamp, change_type (`ingest` | `merge_append` | `derived_view_regen` | `manual_edit`).
- **Embedding** — page_id, vector, model_version (tag which embedding model produced it, so vectors from different models are never compared as if compatible).
- **IngestLog** — chronological, append-only. timestamp, source_type, raw_source_blob_path, mode (`manual` | `automatic`), pages_affected, tier.
- **TeamMembership** — not stored; resolved from the Entra ID groups claim in the ID token at auth time (see Azure setup guide). Team = the Microsoft 365 Group backing that team's Teams channel.

### Cosmos DB access: Entra ID/RBAC, not a static key
Cosmos DB has key-based authentication disabled; the app authenticates as the `llm_wiki_poc` app registration itself (`ClientSecretCredential` in `app/db.py`), assigned the Cosmos DB Built-in Data Contributor data-plane role. No `COSMOS_KEY` exists anywhere in this project. Blob Storage and Azure OpenAI still use account keys/API keys — Cosmos got the RBAC treatment because it was a small, contained change (one file); doing the same for the others is a reasonable, undone follow-up, not something ruled out architecturally.

**Platform limitation this creates**: Cosmos DB's data-plane RBAC does not permit metadata operations (create/delete database or container) — the SDK calls used elsewhere in this project are rejected under AAD auth regardless of assigned role. Database and container creation is therefore a one-time, by-hand step via the Portal's Data Explorer (or CLI/IaC), not something `scripts/provision_cosmos.py` can do; that script only verifies the containers exist and are reachable. Ordinary read/write/query operations work fine via RBAC once containers exist — only creation is restricted.

### Write ordering and failure handling
Blob write first, then index update. If the index write fails after a successful blob write, the content is safe and the index is stale — recoverable by a reindex job. The reverse order would risk an index pointing at content that doesn't exist. A `reindex()` operation that rebuilds Cosmos from blob must exist and be runnable on demand; it is the recovery path for any index corruption or drift.

### Concurrency
Blob has no automatic concurrency control the way a database does. Team-tier writes must take a **blob lease** before writing and release after, with a retry loop — otherwise two simultaneous pushes to the same page can silently overwrite each other. This is cheap to implement and made safer by the append-only rule (a retry just re-reads and re-appends; no merge logic needed). Individual-tier writes are single-user and don't need leasing.

### Export
An `export_wiki()` operation that produces a downloadable archive of the markdown files plus a manifest (links, metadata, source refs). Cheap given blob already holds files in this shape, and doubles as the user-facing backup story.

**As implemented** (`app.wiki.export_wiki()`, `GET /{tier}/{owner}/export`): a zip archive, one `.md` file per page plus `manifest.json` (id, title, filename, tier, timestamps, version, links_out, source_refs per page). Reads through the existing `app.abstractions` page-listing/read functions, not `blob_store` directly.

## The schema document (per-user, co-evolved)

This is the equivalent of `CLAUDE.md`/`AGENTS.md` in the original pattern, and it is **not** application code. It's what makes the agent a disciplined wiki maintainer rather than a generic chatbot, and the original pattern is explicit that it's co-evolved by the user over time as they figure out what works for their domain.

- **Stored as a markdown file in blob**, alongside the wiki it governs (`individual/{user_id}/_schema.md`, `team/{team_id}/_schema.md`) — editable by the user directly, versioned like any other page.
- **Read at the start of every ingest, query, and lint operation** and included in the model context. Conventions live here, not hardcoded in the app.
- **Contents**: page structure conventions, naming rules, when to create a new entity page vs. extend an existing one, how to handle conflicting sources, what to emphasize/ignore for this user's domain, output format preferences.
- **Ships with a sensible default** so a new user isn't starting from a blank file, but is explicitly theirs to modify — if conventions live only in application code, every user gets identical behavior and nobody can tune it for their domain, which defeats the point.
- **Agent may propose amendments**: after a run of manual-mode ingests, the agent can suggest schema updates based on corrections the user made repeatedly ("you consistently asked me to keep summaries shorter — should I add that?"). Proposed, never auto-applied.
- **Also serves as the personalization mechanism**: a "how this person thinks" section (reasoning patterns, preferred framing, recurring concerns) lives here. This is the portable alternative to fine-tuning — it's a prompt, so it survives every model swap for free, updates instantly rather than needing a retrain, and carries over intact at Copilot API migration. Fine-tuning was explicitly considered and rejected for this project: current Azure fine-tuning availability is narrow (GPT-4o fine-tuning is being retired, GPT-5 SFT unsupported), it teaches style rather than facts (retrieval already covers facts), and a fine-tuned deployment is fully stranded at Copilot API migration since custom weights have no equivalent in Copilot's orchestration layer.

**Few-shot personalization (complements the schema doc)**: when a manual-mode discussion starts, retrieve 1-2 past discussion transcripts on semantically similar material (via the existing embedding search) and include them as few-shot context, so the model sees concrete examples of how this user reasons through comparable sources.

**As implemented** (`app/schema.py`): storage, versioning, the shipped default, and read-at-start-of-ingest/query/lint are all built — the doc is folded into the system prompt via `context_block()` in `app.ingest.pipeline`, `app.query`, and `app.lint`. The `automatic_lint_queue_threshold` operating parameter (see Lint queue prioritization below) is parsed from this doc's frontmatter with a small regex, not a YAML dependency — it's the one structured value this doc currently needs. **Not built**: agent-proposed amendments, and the few-shot personalization paragraph above — both are still open, tracked in the README.

## Core operations

The original pattern has three peer operations — **Ingest**, **Query**, and **Lint**. All three are first-class here; none is optional. Ingest alone produces a pile of summaries, not a compounding knowledge base.

### Query (and query-as-write)
- User asks a question against the wiki. The agent searches for relevant pages, reads them, and synthesizes an answer **with citations back to specific pages and their underlying raw sources**.
- **The output of a good answer can be filed back into the wiki as a new page.** This is not optional polish — it's half the compounding loop. A comparison the user asked for, an analysis, a connection discovered mid-conversation: these are as valuable as ingested sources and should not evaporate into chat history.
- Implementation: after answering, offer "save this to the wiki" — same diff-review flow as ingest, same cross-page update logic, logged in IngestLog with a distinct type (`query_derived`) so it's traceable as synthesis rather than source-derived.
- Answers filed this way are marked as query-derived in their frontmatter, with the originating question retained — so a later reader knows this page exists because someone asked something, not because a document arrived.

### Lint (scheduled and on-demand)
A periodic health-check pass. Without it a wiki degrades into a pile of stale summaries within months — this is the operation that keeps the artifact compounding rather than accumulating. Checks for:
- **Contradictions** between pages
- **Stale claims** superseded by newer sources
- **Orphan pages** with no inbound links
- **Missing cross-references** — pages that should link to each other and don't
- **Concepts mentioned repeatedly but lacking their own page** — candidates for promotion to a full entity/concept page
- **Data gaps** worth filling, with suggested questions to investigate or sources to look for

Lint has its own manual/automatic toggle, mirroring the ingest mode toggle (same UX pattern, independent setting):
- **Manual lint** (default): flags and proposes; does not auto-fix. Output is a review queue the user works through, each item approved or dismissed individually.
- **Automatic lint**: applies **mechanical** findings without a review gate — missing cross-references, stub pages for concepts mentioned repeatedly, orphan-page flags, index updates, derived-view regeneration. These have an obviously correct action and are additive/reversible via version history. **Judgment** findings — contradictions between pages, staleness calls, page merges — always queue for manual review regardless of lint mode; getting these wrong writes a confident falsehood that future syntheses build on, so they're never auto-applied. This mirrors the existing principle of routing deterministic work through code and reserving human attention for genuine judgment calls.
- **Team tier constraint**: automatic lint may only regenerate derived views and add cross-references — it can never touch the append log (already immutable by spec). Any judgment finding on the team tier goes through the normal push/review path.
- **Individual tier**: safe to be more permissive than team tier, since it's single-owner and every change is reversible via version history.

**As implemented** (`app/lint.py`): mechanical checks (orphans, missing cross-refs, stub candidates) are code-only, no model calls — cheap enough to run on every pass. Judgment checks (contradictions/staleness via the same two-stage vector-shortlist-then-LLM pattern as push's conflict check; data gaps via one bounded call over page titles) are capped at the first 15 pages per run for cost discipline. Findings persist as a JSON blob per scope (`{tier}/{owner}/_lint/queue.json`), not a new Cosmos container — adding one would mean another by-hand Data Explorer step per `AZURE_SETUP_GUIDE.md` §4, which this build avoided since the queue has no vector-search or structured-query need Cosmos actually serves. **Team-tier constraint, as built** (`app/derived_views.py`): automatic lint now regenerates derived views to resolve `missing_xref` findings — never by editing the append log, only by regenerating the mentioning page's derived view (the synthesis step adds the cross-reference there). `orphan`/`stub_candidate` still have no team-tier fix (creating a page or resolving an orphan isn't "regenerate a derived view or add a cross-reference") and stay queued regardless of mode, matching the constraint as literally stated. Each automatic team-tier lint pass also regenerates up to `MAX_DERIVED_VIEW_PAGES` other team pages' derived views, standing in for the "regenerated on a schedule" cadence since this POC has no background scheduler (same narrowing as lint's own on-demand-only note below). Not built: the correction-entry `change_type`, off-cycle regen on a correction, and the regeneration-diff-shown-to-filer flow — tracked in the README.

### Lint queue prioritization and visibility (addresses automatic-ingest-mode volume)
Automatic ingest mode writes unattended; lint (in manual mode) only flags. Left undifferentiated, automatic-mode-written pages can accumulate faster than the queue gets worked — the safety net fills faster than it drains. Mitigations, scoped to *visibility and ordering*, not throttling (a single-user, human-paced POC doesn't have the producer/consumer contention a throttle is meant to solve):
- Lint queue items carry their originating `IngestLog.mode` (already stored). Automatic-mode-derived items sort first in the queue by default, with a filter to isolate them.
- A **persistent unreviewed count**, broken out by mode, is visible in the app chrome from anywhere — not only on the lint page. This is the actual intervention: the failure mode is invisibility (forgetting the queue exists), not disorder, so a count you can't avoid seeing is what changes behavior.
- **Threshold-triggered downgrade, not a hard block**: the failure-prone combination is automatic ingest + manual lint — nothing is checked at write time, and the only thing that would check it (manual lint review) is waiting on the same act (user attention) that automatic ingest was chosen to skip. When the unreviewed automatic-derived queue count exceeds a threshold, **automatic ingest mode is disabled** (forced to manual) until the queue is worked down below the threshold. Ingest capture itself is never blocked — only automatic mode is unavailable, so a mid-research capture always succeeds, just at the cost of a discussion instead of a silent write. The threshold is a user-configurable value in the schema doc (`_schema.md`), consistent with that file's role as the place personal operating conventions live. This does not apply when automatic lint is enabled, since mechanical findings clear themselves and the queue only ever holds judgment calls.

**As implemented**: sort-first-by-automatic-origin, the isolating filter, and the threshold downgrade (wired into `POST /ingest`, reading the threshold from `app.schema`) are all built. Two narrower-than-spec calls: (1) lint here is on-demand only (a "Run lint now" button) — there's no background job runner in this POC to drive a *scheduled* pass, so the "scheduled and on-demand" framing above is on-demand-only for now; (2) the unreviewed count is surfaced on the workspace page and the lint queue itself, not literally every route in the app chrome, to avoid an extra blob read on every page view for a count that's already one click from the workspace hub.

### Ingest
See the ingest pipeline section below.

## Ingest pipeline

### Source types to support (all from day one — none of these are individually hard to build)
- PDF (text-based and scanned/OCR)
- Word (.docx)
- PowerPoint (.pptx), including speaker notes
- Excel/CSV — summarize structure and key figures rather than dumping raw cells
- Plain text / Markdown
- Pasted raw text
- Web article URL — extract main content, strip nav/ads/sidebars (e.g. `trafilatura`); headless rendering (Playwright) for JS-heavy pages
- PDF-at-URL — fetch then reuse the PDF handler
- Forwarded email (via Graph API once available — already structured, minimal parsing needed)

No images, no video/transcript ingestion — explicitly excluded per scope decision.

### Trigger model
Explicit, user-initiated only. No passive monitoring, no auto-pull from mailbox/SharePoint. The user drops a file, pastes a URL, or pastes text into an open workspace while working.

### Modes
A single persistent toggle control (not a per-drop selector) — cycled by click or keypress (`m`) — showing current mode. Whatever mode is displayed at the moment of submission (Enter / upload confirm) applies to that whole drop, including multi-file drops.

- **Manual mode**: every source, even in a batch, gets its own full sequential pass — no batching sources into one combined discussion:
  1. Agent reads the source
  2. Agent opens an **open-ended, agent-led discussion** with the user about key takeaways (not a fixed question script — content varies too much for one script to fit)
  3. Agent proposes a summary page (diff shown before write)
  4. On approval: write summary page, update index, update relevant cross-referenced pages, append log entry
- **Automatic mode**: any number of sources dropped together, each gets its own independent LLM-generated summary of key takeaways, written directly to the wiki with no discussion step and no per-item approval gate. (Trust bar for this mode: rely on frontier-model summarization quality; iterate system prompt empirically. Revisit this bar before extending automatic mode to team-tier or multi-user exposure — "good enough for personal use" and "good enough to trust unattended on someone else's documents" are not necessarily the same threshold.)

Batch drops are always processed per-source, never combined into a single cross-source summary, in either mode.

## Wiki structure and edit rules

### Individual tier
- **Editable, contextual**: the agent may rewrite/extend existing page text in place (not just append) as new related sources are ingested — matches the original pattern's incremental-refinement model. Full version history retained regardless (see PageVersion).
- **No log/derived-view split here (decided, not an oversight)**: the team tier's append-log + derived-view structure exists to solve a problem the individual tier doesn't have — multi-contributor attribution and the chronological pile that produces. A single user rewriting their own notes generates neither. The individual tier already has an immutable substrate that matters (the raw source store, verbatim + linked from every page) plus version snapshots; what it lacks is attribution across contributors, which is meaningless for one user. Introducing the split here would import team-tier machinery for a problem that doesn't exist, and a hybrid (split only above some edit-count threshold) is worse than either extreme — it means two code paths, migration logic between them, and per-page behavior that differs for reasons invisible to the user.
- **Read-side gap this leaves, and its fix**: reconstructing "what did I know about X before repeated contextual rewrites smoothed it over" from a raw stack of version snapshots means diffing backward by hand — a real deficiency, but a read-time one, not a storage one. Fix: a **derived chronological history view** per page, computed on demand from existing version snapshots — each version with its date, triggering source, and what changed. Gives the same readable trail the team-tier log provides, without a second write path, without changing `write_page()`, and with no new storage.
- **index.md-equivalent**: a generated index/contents page, updated on every ingest, catalogued by category.
- **log.md-equivalent**: append-only IngestLog, human-readable.

### Team tier
- **Append-only substrate, no exceptions**: the agent must never edit or delete existing text on a team page's append log. New content from a push always lands as a new, dated, attributed section appended to the target page (or a wholly new page, if no related page exists).
- **Bottom-append format**: each appended block is timestamped and attributed to the pushing user, so provenance and recency are visible without needing a diff tool.
- **Semantic conflict flagging**: before an append lands, run the two-stage check below. If a conflict is found, don't block the push — surface it to the pusher for a go/no-go decision, and if they proceed, write the conflict note *into* the appended content itself (e.g. "Note: this may conflict with an earlier statement on [Page X], dated [date]") so future readers see the tension. Conflict resolution is pusher-only; the original author of the conflicting content is not notified or looped in synchronously.

#### Derived current-view pages (resolves the append-only readability problem)
Append-only alone gives auditability but degrades readability — a page pushed to 20 times becomes a dated chronological pile with a growing stack of unresolved conflict flags, which is closer to the RAG failure mode the original pattern argues against than to a maintained wiki. The resolution is a two-layer structure:

- **The append log is the immutable substrate.** Never edited. Full provenance, attribution, and chronology preserved. This is the record.
- **A derived "current view" page sits on top of it, per topic, and IS rewritten.** The agent regenerates it from the log: resolving what's superseded, folding in newer content, presenting a current, readable synthesis of what the team knows about that topic.

Rules for derived views:
- **Explicitly marked as machine-generated** in the UI — never mistakable for someone's authored contribution.
- **Always links back to the log entries it was derived from**, so provenance is one click away and synthesis never displaces the record.
- **Regenerated on a schedule** (alongside lint), not on every push — keeps cost bounded and avoids churning the page on every small addition.
- **Fully disposable**: a derived view can be deleted and regenerated from the log at any time with no loss. It is never the source of truth.
- Unresolved conflicts surface in the derived view as an explicit "contested" note rather than being silently smoothed over by the synthesis.

**As implemented** (`app/derived_views.py`): a derived view is a non-indexed blob (`team/{team_id}/_derived/{slug}.md`, same "_"-prefix pattern `app.lint`'s queue already uses — excluded from the page index by `blob_store.list_page_paths`), with YAML frontmatter marking `machine_generated: true`, the source page id/title, and a regeneration timestamp, plus a leading line linking back to the log page it was derived from. Regenerated wholesale from the full log body via one bounded model call per regeneration (system prompt explicitly instructs surfacing unresolved conflicts as `> **Contested:**` lines rather than smoothing them over). Reachable at `GET /team/{team_id}/derived/{slug}`, linked from the page view, with a "Regenerate now" button (`POST .../regenerate`) for on-demand regen. Triggered automatically by an automatic-mode team-tier lint run (see Lint's "As implemented" note) standing in for "regenerated on a schedule," since this POC has no background scheduler — same narrowing lint itself already has. **Not built**: the correction-entry `change_type`, off-cycle regen on a correction, and the regeneration-diff-shown-to-filer flow described below — tracked in the README, not silently dropped.

#### Correcting a bad derived view (decided)
Derived views are regenerated wholesale from the log, so a synthesis error (e.g. misreading which of two log entries supersedes the other) can't be fixed by editing the log (immutable) or the view (overwritten on the next regen). Resolution: **treat the bad synthesis as new information, not as an error to patch in place.**
- The correction is filed as a normal, dated, attributed entry appended to the log — e.g. "the previous derived view mischaracterized X; the actual status is Y" — flagged as a correction entry (a `change_type` value distinct from an ordinary push, so it's identifiable later). No new write path or edit exception to the append-only rule.
- This is less circular than it looks: the original error came from the model inferring supersession from ambiguous chronology across two ordinary entries. A correction entry doesn't require that inference — it states the resolution explicitly and is the most recent, most explicit entry in the log. Different, easier task than the one that produced the error.
- **Off-cycle regeneration**: a correction entry triggers immediate regeneration of that page's derived view rather than waiting for the scheduled pass — closes the gap where the fix wouldn't be visible until the next scheduled regen.
- **Regeneration diff on correction**: the user who filed the correction is shown the resulting derived view (diff against the prior version) so they can confirm the fix actually landed, rather than assuming it did.
- **Escalation path**: if the same misreading recurs on a topic, the durable fix is a rule in the team's schema doc (`_schema.md`) — e.g. "treat rolling-agreement statements as superseding fixed-renewal dates" — not another correction entry. Keeps repeat fixes inside an existing, provenanced mechanism rather than accumulating patches in the log.
- **A "pin this correction" mechanism (locking a fragment of the view so regeneration preserves it verbatim) was considered and explicitly ruled out.** It's not merely inconsistent with "derived views are disposable, never authoritative" — a pinned fragment is *unprovenanced*: every other line in a derived view traces to a log entry, but a pinned line traces to nothing, exempt from every future correction. That's a worse failure mode than an occasional bad synthesis, which at least self-corrects on the next regeneration.
- **Trade accepted**: correction entries are technically instructions to the summarizer (how to read the existing record), not new facts about the world, appended to a log that otherwise holds genuine source-derived and pushed content. This is a deliberate, bounded exception — worth knowing, not worth building special-casing around.

### Cross-page updates (breadth is a requirement, not a side effect)
A single ingested source is **expected** to touch many pages — the original pattern says 10-15 is normal for one source: the summary page, affected entity pages, concept pages, the index, and any pages whose claims are now contradicted or extended.

**Explicit guard**: do not let the diff-review UI quietly narrow this to a single destination. Reviewing a fifteen-file diff is harder to build than a one-file diff, and the path of least resistance is to make the agent propose one target page — which would silently gut the cross-referencing that makes the wiki compound. If breadth is ever reduced, it must be a deliberate, stated decision, not an artifact of UI convenience.

Requirements this implies for the diff UI:
- Must present a **multi-page changeset** as one reviewable unit — a list of affected pages, each expandable to its own diff, with approve-all / approve-selectively / reject-all.
- Default to showing the full set collapsed with per-page summaries ("Client X — 2 lines added", "Index — 1 entry"), so a 15-page changeset is scannable rather than overwhelming.
- Approving the changeset is one action; the user shouldn't have to click through fifteen separate approvals.

**As implemented** (`app.wiki.build_changeset()` / `apply_changeset()`, `templates/changeset.html`): one `Changeset` (primary page + up to `MAX_CROSS_PAGE_CANDIDATES` model-selected related pages that should also change + the regenerated index, each with a diff and a one-line summary) is built per manual-mode ingest proposal, per push, and per query-derived save — all three now go through this one component instead of a single-page diff, with approve-all / approve-selectively (per-item checkboxes) / reject-all. Automatic-mode ingest builds and applies the same changeset immediately (no gate), so breadth isn't manual-mode-only. **Narrower than spec**: push builds a changeset with `include_cross_page=False` (see `app/push.py` docstring) — push already ran its own placement step to pick one target page, and the "10-15 pages is normal" breadth figure is stated for ingest, not push; this is a deliberate, flagged narrowing, not a silent one.

## Search and conflict detection (self-built, no Azure AI Search)

- **Embeddings**: Azure OpenAI embedding endpoint (same resource as the chat model calls), called via the `embed()` abstraction function. Vectors stored in Cosmos.
- **Similarity search**: Cosmos DB native vector search (DiskANN) for **both** tiers, through the same `search()` function. Note: an earlier draft specified brute-force in-process similarity for the individual tier — that assumed a long-running local process holding embeddings in memory, which no longer applies now that both tiers run as stateless hosted containers. A stateless container would have to read every embedding from storage on each query, which is strictly worse. One search path, both tiers.
- **Conflict detection on push (two-stage, avoids O(n²))**:
  1. Use vector search to narrow to a shortlist of plausibly-related existing team pages (cheap).
  2. Only run an LLM reasoning pass — "does this pushed content conflict with this shortlisted page" — against that shortlist, not the whole wiki (expensive step, scoped down).
- Note for later: this catches conflicts between semantically *similar* content. It will miss conflicts between semantically distant but factually connected content (e.g. two unrelated-looking pages that both reference the same client's status). A knowledge-graph layer (entity/relation extraction) would close this gap but is explicitly deferred — not a POC requirement.

## Push / merge mechanism (individual → team)

1. User selects specific pages or sections from their individual wiki to push (never a whole-wiki sync — this is a deliberate human judgment call about what's team-appropriate, not something to infer automatically).
2. LLM layer determines where the pushed content belongs in the team wiki: append to an existing related page, or create a new page.
3. Two-stage conflict check runs (see above); any flagged conflict is written into the appended content, not resolved automatically.
4. A diff/preview is shown to the pushing user for confirmation before anything is written.
5. On confirmation: content is appended (bottom, dated, attributed) per the append-only rules above.

## Access control

- **Individual tier**: private to the authenticated user. No sharing, no cross-user visibility, by default.
- **Team tier**: scoped per team. A "team" = the Microsoft 365 Group backing that team's Teams channel (confirm via Graph API at build time — don't build a separate membership list). Users outside a team's group must not be able to view or push to that team's wiki. Multiple teams can exist, each fully isolated from the others.
- Auth: Entra ID, single sign-on, for both tiers.

## Frontend

- Single containerized web app, two modes/views (personal / team), same auth.
- **Landing experience**: the Wikipedia-style page view is the default landing page for both tiers — not the map view (see below). Map view is a secondary, deliberately-navigated-to view.
- **Wikipedia-style browsing**: rendered markdown pages, `[[Page Name]]`-style links resolved to real page routes/ids, a generated contents/index page, a search bar wired to the search function above.
- **Local graph, per page**: on each page, show only that page's direct connections (what links here, what it links to) as a small node cluster — not a whole-wiki force-directed graph. This is the primary "how does this connect" affordance for a single page.
- **Themes page**: a periodically-generated (or on-demand) page that clusters the wiki's content via embeddings and describes the resulting themes in plain language with links into each cluster — answers "how is my knowledge structured" without needing a visual map. Reuse the same clustering/embedding infrastructure as the lint pass. Applies to both individual and team tiers.
- **Radial map view (both tiers)**: a visual, two-level drill-down view built directly on the themes-page clustering data (no new backend computation — same clustering call, different rendering). Structure: a central hub node (the wiki root) connects radially to theme/cluster nodes; each theme node connects outward to its member pages as satellite nodes. This is a constrained, hierarchical radial layout (polar-coordinate placement per theme's angle slice), not an open force-directed simulation — deliberately cheaper and more readable at scale than a whole-wiki graph, since layout is structurally determined rather than physics-simulated. Interaction: overview (all themes visible around center) → tap a theme to focus its satellites → tap a page to open its normal Wikipedia-style page view. Reached via navigation (e.g. a "Map" tab), not the landing page. For the team tier, this is expected to also surface how different contributors' pushed content clusters, given the append-only multi-contributor structure — worth watching whether that's useful in practice, no special handling needed structurally since it reuses the same per-tier clustering.
- **Lint review queue**: UI for working through lint proposals (see Core operations) — each flagged item approved or dismissed individually. Lint itself is a core operation, not a frontend feature; this is just its surface.
- **Save-to-wiki from query**: after any query answer, an affordance to file that answer back as a wiki page, routing into the same diff-review flow.
- **Schema doc editor**: a plain markdown editor for the user's own `_schema.md`, plus a surface for reviewing agent-proposed amendments to it.
- **Mode toggle**: persistent control, click/keypress-cycled (`m`, not `Tab` — avoids colliding with browser focus navigation), for manual vs. automatic ingest mode.
- **Diff/review UI**: multi-page changeset review (see Cross-page updates) — used for manual-mode ingest approval, push-to-team confirmation, query-derived page saves, and lint proposals. One component, four callers.

## Explicit non-requirements (deferred, not forgotten)

- No image or video ingestion.
- No offline mode / local storage of any kind.
- No SharePoint/OneDrive file mirroring — the app is the only place pages are browsed.
- No knowledge-graph-based conflict detection (vector-search-based two-stage check only).
- No open force-directed whole-wiki graph visualization — the radial map view covers whole-wiki visual browsing instead, using a structurally-constrained hierarchical layout (cheaper, more readable) rather than physics simulation.
- No live notification loop to the original author when their content is flagged as conflicting.
- No git as the versioning mechanism — blob-stored full snapshots plus a Cosmos version index instead. (Portability, the main reason the original pattern used git, is preserved by markdown-canonical storage and the export operation.)
- No fine-tuning of the underlying model — see the schema document section for why, and for the portable alternative used instead.

## Cost discipline

This is a cost-conscious POC. Apply these patterns throughout:
- Narrow retrieval/search scope before invoking the LLM — never pass unnecessarily broad context.
- Prefer structured/short model outputs (e.g. a classification label) over open-ended prose for deterministic decisions.
- Route simple, deterministic operations through plain code, not through `call_model()` — reserve model calls for genuine judgment calls (summarization, discussion, conflict assessment, merge placement).
- Cache/avoid redundant re-embedding or re-retrieval of unchanged content.
- **Model choice**: deploy using the **Data Zone Standard (EU)** deployment type, which keeps processing within the EU data zone (satisfying residency) while giving more model/quota flexibility than pinning to one specific region — pick the cheapest capable model available under that deployment type at build time rather than committing to a specific model name (catalog/pricing shifts). As deployed: `gpt-5-nano` for chat, `text-embedding-3-small` for embeddings, both Data Zone Standard.
- **Newer chat model families reject the `max_tokens` parameter** — `gpt-5-nano` (and other current-generation reasoning-family models) require `max_completion_tokens` instead; `call_model()`/`call_model_chat()` in `app/abstractions.py` use the latter. Worth checking this parameter name again if the chat deployment is ever swapped to a different model family.
- **Reasoning-family models silently spend the completion budget on hidden reasoning tokens, not just visible output.** Observed on `gpt-5-nano`: `reasoning_tokens` for the same trivial prompt varied from ~60 to ~700 across repeat calls, and when reasoning alone exceeds `max_completion_tokens`, the visible `content` comes back empty with no error — a caller that doesn't check for this writes an empty/garbage page rather than failing loudly. Fix applied in `call_model()`/`call_model_chat()`: pass `reasoning_effort="low"` (drops reasoning token usage to ~0 in testing) and raise if `content` is still empty rather than let an empty string flow through to a page write.

## Open items to verify during build (not blocking, but track)

- Confirm which Microsoft 365 Group backs each team's Teams channel via Graph API, empirically, per team.
- At Copilot API migration time: confirm whether the tenant's Copilot deployment routes any relevant calls through Claude specifically, and whether that affects the EU residency guarantee for this app's data.
