# LLM Wiki — POC

A persistent, AI-maintained knowledge wiki. Private individual wikis; selected pages pushed to an append-only team wiki via an LLM-mediated merge with conflict flagging. See `docs/BUILD_SPEC.md` for the full design and `docs/AZURE_SETUP_GUIDE.md` for the manual Azure setup.

## Architecture in one paragraph

FastAPI web app (single container, Azure Container Apps) → all external calls go through the abstraction layer in `app/abstractions.py` (`call_model`, `get_context`, `search`, `write_page`, `embed`) → Azure OpenAI (EU region) for chat + embeddings. Storage is split: **Blob Storage is canonical** (page markdown, version snapshots, immutable raw sources), **Cosmos DB is a derived, rebuildable index** (metadata + vector search only) — see "Storage model" in the build spec. Auth is Entra ID (confidential client); team scoping comes from the ID token's groups claim. This layout is what makes the eventual Microsoft 365 Copilot API migration a swap of `abstractions.py` internals, not a rewrite.

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
| `app/wiki.py` | Wikilinks, index regeneration, individual-editable vs team-append-only rules |
| `app/ingest/` | Extractors (pdf/docx/pptx/xlsx/csv/text/url) + manual/automatic pipeline; raw sources written to blob for provenance |
| `app/query.py` | Query operation: ask a question, get a cited synthesized answer, optionally save it to the wiki via the same diff-review flow as ingest |
| `app/push.py` | Individual→team push: placement, two-stage conflict check, preview/confirm |
| `app/schema.py` | Per-scope `_schema.md` governance/personalization doc: blob-backed storage + versioning, shipped default, read into every ingest/query/lint system prompt, `automatic_lint_queue_threshold` operating parameter |
| `app/lint.py` | Lint operation: mechanical checks (orphans, missing cross-refs, stub candidates — code only) and judgment checks (contradictions, staleness, data gaps — bounded model calls); manual/automatic modes, JSON-blob-backed review queue, unreviewed-count visibility, threshold-triggered automatic-ingest downgrade |
| `app/main.py` | Routes + templates |
| `scripts/provision_cosmos.py` | Creates Cosmos containers (vector policy must be set at creation) + blob containers |

## Not yet built (tracked, deliberate)

- OCR for scanned PDFs (extractor raises a clear error for now)
- Playwright rendering for JS-heavy pages (trafilatura path works today)
- Team-tier derived current-view pages (see BUILD_SPEC "Derived current-view pages"). Because this layer doesn't exist yet, automatic lint mode never mutates team-tier content — it only records mechanical findings as pending for manual review, even on the team tier. Team-tier lint findings therefore behave like manual mode regardless of the mode toggle, until derived views land.
- Contradiction/staleness lint findings are judgment-only flags with no auto-generated resolution diff — approving one acknowledges it; deciding *how* to fix it is manual, by design (build spec: getting this wrong writes a confident falsehood).
- Scheduled/background lint runs — lint is on-demand only (a "Run lint now" button), since there's no background job runner in this POC yet. The build spec calls for "scheduled and on-demand."
- The unreviewed lint count is visible on the workspace page and the lint queue itself, not in the global nav on every route (e.g. not on page-view or push-preview screens) — a narrower reading of the build spec's "visible from anywhere," chosen to avoid an extra blob read on every page load for a count that's cheap to check from the workspace hub.
- Agent-proposed `_schema.md` amendments ("you consistently asked me to keep summaries shorter — should I add that?") — schema doc is currently user-edited only, no amendment-proposal flow yet.
- Few-shot personalization (retrieving past discussion transcripts as few-shot context) — not wired up.
- Themes page and radial map view (clustering infra)
- `export_wiki()` backup/portability operation
- Email ingest (deferred until Graph `Mail.Read` is set up)
- Multi-source manual-mode queue UI (sessions are created; only the first is auto-opened)
