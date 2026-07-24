# LLM Wiki — POC

A persistent, AI-maintained knowledge wiki. Private individual wikis; selected pages pushed to an append-only team wiki via an LLM-mediated merge with conflict flagging. See `docs/BUILD_SPEC.md` for the full design and `docs/AZURE_SETUP_GUIDE.md` for the manual Azure setup.

**Built and running** (`docs/MVP_SPEC.md`): a one-shot pipeline that turns a team's existing documents into a wiki, a community map, and a predicted knowledge frontier.

**Being designed** (`docs/FRAMEWORK.md`, `docs/MODEL_SPEC.md`): the first real corpus run answered the MVP's question with a qualified no. The pipeline produces ~270 accurate, well-sourced pages from which nothing can be predicted — a good index, not a model of the domain. `FRAMEWORK.md` sets out what would count as solving that (a mental model, in a specific and testable sense) and `MODEL_SPEC.md` how to build it. Nothing in `MODEL_SPEC.md` is built yet; the wiki layer underneath it stands.

**Gate 0 is done and passed** (`docs/GATE0_PILOT.md`): all thirteen corpus documents read by hand before writing any code, to find out whether the design was worth building. Arguments chain, and they chain *across* documents — the 2026 National Cancer Plan uses the screening impact assessments' conclusions as its premises. The pilot also overturned its own first result, which is why it is written up rather than summarised: reading four similar documents produced a confident finding that reading all thirteen destroyed.

Terminology across all of these is defined in `CONTEXT.md`; decisions that were hard to reverse are in `docs/adr/`.

## Architecture in one paragraph

FastAPI web app (single container, Azure Container Apps) → all external calls go through the abstraction layer in `app/abstractions.py` (`call_model`, `get_context`, `search`, `write_page`, `embed`) → Azure OpenAI (EU region) for chat + embeddings. Storage is split: **Blob Storage is canonical** (page markdown, version snapshots, immutable raw sources), **Cosmos DB is a derived, rebuildable index** (metadata + vector search only) — see "Storage model" in the build spec. Auth is Entra ID (confidential client); team scoping comes from the ID token's groups claim. This layout is what makes the eventual Microsoft 365 Copilot API migration a swap of `abstractions.py` internals, not a rewrite.

Blob is genuinely self-sufficient: every page blob carries YAML frontmatter (id, title, created date, version, source refs), so `scripts/reindex.py` can rebuild Cosmos from scratch without losing page identity, version history, provenance, or the `[[link]]` graph. Team-tier writes re-derive their append under a blob lease at write time rather than replaying a body captured before human review, so two concurrent pushes to the same page can't silently overwrite each other. In-flight review state is persisted to blob rather than process memory, so a discussion or a pending push survives a restart or a request landing on another replica.

Ingest, push, and query-derived saves all route their writes through one shared review unit — a `Changeset` (`app/wiki.py`) covering every page a single source touches (the primary page, any related pages the model flags for an update, the regenerated index) — rather than each caller reviewing a single page in isolation. Team pages are append-only logs; a separate, disposable **derived current-view** layer (`app/derived_views.py`) is regenerated from each log to keep it readable without ever rewriting the log itself.

## Local dev

```bash
uv sync                          # or: pip install -e ".[dev]"
cp .env.example .env             # fill in Azure OpenAI + Cosmos + Blob Storage values
uv run python scripts/provision_cosmos.py   # once, after Cosmos + Blob Storage accounts exist
AUTH_DEV_BYPASS=true uv run uvicorn app.main:app --reload
```

`AUTH_DEV_BYPASS=true` skips Entra sign-in and acts as a fake user — local dev only, never set it in production. Azure OpenAI, Cosmos, and Blob Storage credentials are still required (there is deliberately no local storage or local model fallback, per spec).

## Deploy

```bash
docker build -t llmwiki .
# push to ACR, deploy to the Container Apps environment,
# set secrets per .env.example, then add the prod redirect URI
# to the Entra app registration.
```

## Layout

| Path | What |
|---|---|
| `app/abstractions.py` | The migration boundary — the only module that touches Azure OpenAI SDK directly; storage goes through `app.blob_store` / `app.db` |
| `app/blob_store.py` | Canonical content store — page/version markdown, immutable raw sources, team-tier blob leases |
| `app/db.py` | Cosmos client — derived index only (page_index, version_index, embeddings, ingest_log), rebuildable via `abstractions.reindex()` |
| `app/models.py` | Page (runtime), PageIndex/VersionIndex (Cosmos storage schema), Embedding, IngestLog entities |
| `app/auth.py` | Entra OIDC flow, session cookie, team access checks |
| `app/wiki.py` | Wikilinks, index regeneration, individual-editable vs team-append-only rules, and the multi-page `Changeset`/`build_changeset`/`apply_changeset` review machinery shared by ingest, push, and query-save; `export_wiki()` |
| `app/ingest/` | Extractors (pdf/docx/pptx/xlsx/csv/text/url) + manual/automatic pipeline; raw sources written to blob for provenance |
| `app/query.py` | Query operation: ask a question, get a cited synthesized answer (or an honest "the wiki doesn't cover this" when retrieval is empty, no fabrication), optionally save it to the wiki via the same multi-page changeset review flow as ingest |
| `app/knowledge_map.py` | Whole-wiki structure from the explicit `[[link]]` graph (not embeddings): networkx Louvain community detection + one naming call per community, cached as a non-indexed `_map/map.json` blob; feeds both the radial Map view and the deterministic Contents page |
| `app/push.py` | Individual→team push: placement, two-stage conflict check, changeset preview/confirm |
| `app/derived_views.py` | Team-tier derived current-view pages: regenerated wholesale from a page's append log, machine-generated marker + link back to the log, non-indexed blob storage (disposable/regeneratable) |
| `app/schema.py` | Per-scope `_schema.md` governance/personalization doc: blob-backed storage + versioning, shipped default, read into every ingest/query/lint system prompt, `automatic_lint_queue_threshold` operating parameter |
| `app/frontmatter.py` | YAML frontmatter on canonical page blobs (id, title, created_at, version, source_refs) — what makes blob self-sufficient so `reindex()` can restore identity and provenance after a Cosmos wipe; parsed/stripped at the blob boundary so `Page.body` stays clean |
| `app/review_store.py` | Durable storage for in-flight review state (manual sessions, pending changesets) as non-indexed `_sessions/`/`_changesets/` blobs, scoped by owning user so ownership checks are structural |
| `app/corpus/` | The MVP pipeline: `concepts.py` (per-document extraction with evidencing passages), `consolidate.py` (hierarchical merge + embedding dedup into the page set), `synthesis.py` (one page written from all its sources, wholesale), `frontier.py` (predicted gaps, map-layer only), `runner.py` (background orchestration + per-stage artifacts) |
| `app/lint.py` | Lint operation: mechanical checks (orphans, missing cross-refs, stub candidates — code only) and judgment checks (contradictions, staleness, data gaps — bounded model calls); manual/automatic modes, JSON-blob-backed review queue, unreviewed-count visibility, threshold-triggered automatic-ingest downgrade; automatic team-tier mode resolves `missing_xref` via `app.derived_views` regeneration |
| `app/main.py` | Routes + templates |
| `scripts/provision_cosmos.py` | Verifies Cosmos containers (created by hand — RBAC forbids metadata ops) + creates blob containers |
| `scripts/reindex.py` | Rebuilds the Cosmos index for one scope from blob, with `--dry-run`; same operation as the workspace "Rebuild index" button |
| `scripts/smoke_test.py` | Cheap check that Azure OpenAI (including JSON mode), Cosmos and Blob are all reachable — run before a paid corpus build |

## Not yet built (tracked, deliberate)

Nothing built:

- OCR for scanned PDFs (extractor raises a clear error for now)
- Playwright rendering for JS-heavy pages (trafilatura path works today)
- Agent-proposed `_schema.md` amendments ("you consistently asked me to keep summaries shorter — should I add that?") — schema doc is currently user-edited only, no amendment-proposal flow yet
- Few-shot personalization (retrieving past discussion transcripts as few-shot context) — not wired up
- Email ingest (deferred until Graph `Mail.Read` is set up)
- Multi-source manual-mode queue UI (sessions are created; only the first is auto-opened)

## Running the corpus pipeline

Open an empty scope (e.g. `/team/{group_id}/`) and it offers the bootstrap page instead of a
workspace — the check is on the scope, not the user, so a later joiner on a built corpus lands
on the wiki. Drop documents, and the six stages run on a background thread with a live run page
at `/{tier}/{owner}/pipeline/{run_id}`.

That run page is also the evaluation surface. Every stage writes an artifact, and the decisive
question — *are these the right pages?* — is answered by reading the Stage 2 concept lists and
the Stage 3 merge decisions directly, not by squinting at the finished wiki. Start there.

Re-running is **full regeneration**: "Add documents / rebuild" on the workspace re-runs the whole
pipeline over the corpus and rewrites every page, with version history keeping the previous pass.
Incremental ingest is the full product's job.

Long documents are **chunked, never truncated** — the single-document ingest path still caps
extraction at `extractors.MAX_CHARS` for cost discipline, but the corpus path opts out, because
silently dropping the back half of a 150-page policy document would be invisible in the output.
Each chunk is one extraction call, so the run page shows a per-document call count.

Run `uv run python scripts/smoke_test.py` first — a corpus pass is hundreds of model calls, and
finding a misconfigured deployment on call three hundred is an expensive way to learn about it.

## Testing

`uv run pytest`. External systems are faked in `tests/conftest.py` — an in-memory blob store (leases enforced), an in-memory Cosmos with partition-key isolation, and a recording model stub — so the real code paths run unmodified without Azure. The suite pins the invariants that fail silently rather than loudly: team append-only, concurrent-push safety, reindex round-trip (identity, versions, provenance, link graph), review state surviving a restart, individual-wiki privacy and team membership, and the query no-coverage rule making zero model calls.

Built narrower than the spec technically calls for, flagged rather than silently reduced:

- **No background scheduler in this POC** (the root cause behind the next two): lint is on-demand only (a "Run lint now" button), not "scheduled and on-demand" as the build spec calls for. Derived-view regeneration piggybacks on that same on-demand lint run (or an explicit "Regenerate now" button on a page) rather than an independent schedule, bounded to `MAX_DERIVED_VIEW_PAGES` pages per pass for cost discipline.
- The build spec's correction-entry workflow for derived views (a dedicated `change_type`, off-cycle regen triggered by a correction, a regeneration diff shown to the filer) isn't built — a correction today is just a normal push/edit, and only the base regenerate-from-log / machine-generated-marker / link-back / disposable behavior is implemented.
- Contradiction/staleness lint findings are judgment-only flags with no auto-generated resolution diff — approving one acknowledges it; deciding *how* to fix it is manual, by design (build spec: getting this wrong writes a confident falsehood).
- The unreviewed lint count is visible on the workspace page and the lint queue itself, not in the global nav on every route (e.g. not on page-view or push-preview screens) — a narrower reading of the build spec's "visible from anywhere," chosen to avoid an extra blob read on every page load for a count that's cheap to check from the workspace hub.
- Push builds the same `Changeset`/review-component the other two callers use, but with `include_cross_page=False` — it does not run the model-driven cross-page-update detection ingest and query-save get (see `app/push.py` docstring), since push already ran its own placement step to choose one target page and the build spec states the "10-15 pages is normal" breadth requirement for ingest, not push.
- Query-path `temperature=0` (build spec) is plumbed through `call_model` but only forwarded when `chat_supports_temperature` is set — the deployed reasoning-family model (`gpt-5-nano`) rejects `temperature != 1`, so it's inert until a non-reasoning model is deployed. Fixed retrieval, not sampling, is the reproducibility lever meanwhile, as the spec notes.
- The Contents page groups by the cached knowledge-map communities; before the map has been built once (or right after new pages are added), unclustered pages appear under "Recently added"/"Unlinked" until the next map regeneration, rather than triggering an immediate (costly) recompute on every ingest.
