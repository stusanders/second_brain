# LLM Wiki — POC

A persistent, AI-maintained knowledge wiki. Private individual wikis; selected pages pushed to an append-only team wiki via an LLM-mediated merge with conflict flagging. See `docs/BUILD_SPEC.md` for the full design and `docs/AZURE_SETUP_GUIDE.md` for the manual Azure setup.

## Architecture in one paragraph

FastAPI web app (single container, Azure Container Apps) → all external calls go through the abstraction layer in `app/abstractions.py` (`call_model`, `get_context`, `search`, `write_page`, `embed`) → Azure OpenAI (EU region) for chat + embeddings. Storage is split: **Blob Storage is canonical** (page markdown, version snapshots, immutable raw sources), **Cosmos DB is a derived, rebuildable index** (metadata + vector search only) — see "Storage model" in the build spec. Auth is Entra ID (confidential client); team scoping comes from the ID token's groups claim. This layout is what makes the eventual Microsoft 365 Copilot API migration a swap of `abstractions.py` internals, not a rewrite.

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
| `app/query.py` | Query operation: ask a question, get a cited synthesized answer, optionally save it to the wiki via the same multi-page changeset review flow as ingest |
| `app/push.py` | Individual→team push: placement, two-stage conflict check, changeset preview/confirm |
| `app/derived_views.py` | Team-tier derived current-view pages: regenerated wholesale from a page's append log, machine-generated marker + link back to the log, non-indexed blob storage (disposable/regeneratable) |
| `app/schema.py` | Per-scope `_schema.md` governance/personalization doc: blob-backed storage + versioning, shipped default, read into every ingest/query/lint system prompt, `automatic_lint_queue_threshold` operating parameter |
| `app/lint.py` | Lint operation: mechanical checks (orphans, missing cross-refs, stub candidates — code only) and judgment checks (contradictions, staleness, data gaps — bounded model calls); manual/automatic modes, JSON-blob-backed review queue, unreviewed-count visibility, threshold-triggered automatic-ingest downgrade; automatic team-tier mode resolves `missing_xref` via `app.derived_views` regeneration |
| `app/main.py` | Routes + templates |
| `scripts/provision_cosmos.py` | Creates Cosmos containers (vector policy must be set at creation) + blob containers |

## Not yet built (tracked, deliberate)

Nothing built:

- OCR for scanned PDFs (extractor raises a clear error for now)
- Playwright rendering for JS-heavy pages (trafilatura path works today)
- Agent-proposed `_schema.md` amendments ("you consistently asked me to keep summaries shorter — should I add that?") — schema doc is currently user-edited only, no amendment-proposal flow yet
- Few-shot personalization (retrieving past discussion transcripts as few-shot context) — not wired up
- Themes page and radial map view (clustering infra)
- Email ingest (deferred until Graph `Mail.Read` is set up)
- Multi-source manual-mode queue UI (sessions are created; only the first is auto-opened)

Built narrower than the spec technically calls for, flagged rather than silently reduced:

- **No background scheduler in this POC** (the root cause behind the next two): lint is on-demand only (a "Run lint now" button), not "scheduled and on-demand" as the build spec calls for. Derived-view regeneration piggybacks on that same on-demand lint run (or an explicit "Regenerate now" button on a page) rather than an independent schedule, bounded to `MAX_DERIVED_VIEW_PAGES` pages per pass for cost discipline.
- The build spec's correction-entry workflow for derived views (a dedicated `change_type`, off-cycle regen triggered by a correction, a regeneration diff shown to the filer) isn't built — a correction today is just a normal push/edit, and only the base regenerate-from-log / machine-generated-marker / link-back / disposable behavior is implemented.
- Contradiction/staleness lint findings are judgment-only flags with no auto-generated resolution diff — approving one acknowledges it; deciding *how* to fix it is manual, by design (build spec: getting this wrong writes a confident falsehood).
- The unreviewed lint count is visible on the workspace page and the lint queue itself, not in the global nav on every route (e.g. not on page-view or push-preview screens) — a narrower reading of the build spec's "visible from anywhere," chosen to avoid an extra blob read on every page load for a count that's cheap to check from the workspace hub.
- Push builds the same `Changeset`/review-component the other two callers use, but with `include_cross_page=False` — it does not run the model-driven cross-page-update detection ingest and query-save get (see `app/push.py` docstring), since push already ran its own placement step to choose one target page and the build spec states the "10-15 pages is normal" breadth requirement for ingest, not push.
