"""Corpus pipeline orchestrator — the six stages end to end, run in a
background thread with stage-by-stage progress persisted to
`_corpus/status.json` (a full corpus takes minutes; a synchronous request
would time out behind Container Apps ingress).

Bootstrap semantics (MVP spec): the check is on the *scope* — empty scope →
run; populated scope → refuse unless an explicit rebuild was requested.
Rebuild wipes generated output but never raw sources (immutable) nor the
Stage 2 concept cache (see abstractions.reset_scope), and the pipeline can
re-run over stored sources without re-upload."""

import threading
import traceback

from app import abstractions as ab
from app import blob_store, knowledge_map, lint, wiki
from app.corpus import artifacts, concepts, consolidate, frontier, synthesize
from app.ingest import extractors
from app.models import IngestLogEntry, User, make_partition_key, new_id, now_iso

# One run per scope at a time — in-process guard (single-replica POC; the
# status blob additionally records terminal state across restarts).
_active: dict[str, threading.Thread] = {}
_lock = threading.Lock()

STAGES = [
    "1/6 Ingesting documents",
    "2/6 Extracting concepts",
    "3/6 Consolidating page set",
    "4/6 Writing pages",
    "5/6 Building map",
    "6/6 Frontier + contents + lint",
]


def is_running(tier: str, owner: str) -> bool:
    with _lock:
        thread = _active.get(f"{tier}/{owner}")
        return thread is not None and thread.is_alive()


def get_status(tier: str, owner: str) -> dict:
    status = artifacts.read_json(artifacts.status_path(tier, owner)) or {"state": "none"}
    # A run that died without a terminal write (process restart) shouldn't
    # poll forever as "running".
    if status.get("state") == "running" and not is_running(tier, owner):
        status["state"] = "error"
        status.setdefault("error", "Run was interrupted (app restarted). Start a new run.")
    return status


def start_run(
    files: list[tuple[str, bytes]],
    tier: str,
    owner: str,
    user: User,
    *,
    rebuild: bool = False,
    reuse_sources: bool = False,
) -> str | None:
    """Start the pipeline in a background thread. Returns an error message
    instead of starting when a run is already active or the bootstrap check
    refuses (scope populated without rebuild)."""
    key = f"{tier}/{owner}"
    with _lock:
        if key in _active and _active[key].is_alive():
            return "A pipeline run is already in progress for this scope."
        scope = make_partition_key(tier, owner)  # type: ignore[arg-type]
        if not rebuild and ab.list_pages(scope):
            return (
                "This scope already has content. Tick 'Rebuild' to wipe the "
                "generated wiki and rebuild it from documents."
            )
        run_id = new_id()
        _write_status(tier, owner, run_id, "running", STAGES[0], 0, len(files))
        thread = threading.Thread(
            target=_run,
            args=(files, tier, owner, user, run_id, rebuild, reuse_sources),
            daemon=True,
        )
        _active[key] = thread
        thread.start()
    return None


def _write_status(
    tier: str,
    owner: str,
    run_id: str,
    state: str,
    stage: str,
    done: int,
    total: int,
    *,
    counts: dict | None = None,
    error: str | None = None,
    started_at: str | None = None,
    errors: list | None = None,
) -> None:
    existing = artifacts.read_json(artifacts.status_path(tier, owner)) or {}
    if existing.get("run_id") != run_id:
        existing = {}
    artifacts.write_json(
        artifacts.status_path(tier, owner),
        {
            "run_id": run_id,
            "state": state,
            "stage": stage,
            "done": done,
            "total": total,
            "counts": counts or existing.get("counts") or {},
            "errors": errors if errors is not None else existing.get("errors") or [],
            "error": error,
            "started_at": started_at or existing.get("started_at") or now_iso(),
            "finished_at": now_iso() if state in ("done", "error") else None,
        },
    )


def _ingest_uploads(
    files: list[tuple[str, bytes]], tier: str, owner: str, errors: list
) -> list[concepts.CorpusDoc]:
    docs = []
    for filename, data in files:
        try:
            source = extractors.extract_file(filename, data)
            ref = blob_store.write_raw_source(tier, owner, filename, data)
            docs.append(concepts.CorpusDoc(source, ref, concepts.doc_hash(data)))
        except extractors.ExtractionError as err:
            errors.append({"file": filename, "error": str(err)})
    return docs


def _ingest_stored(tier: str, owner: str, errors: list) -> list[concepts.CorpusDoc]:
    """Re-run over previously ingested raw sources (no re-upload). Stored
    names are `{uuid32}-{original filename}` — strip the prefix to recover
    the extension for the extractor dispatch."""
    docs = []
    for path in blob_store.list_raw_source_paths(tier, owner):
        data = blob_store.read_raw_source(path)
        if data is None:
            continue
        blob_name = path.rsplit("/", 1)[-1]
        filename = blob_name[33:] if len(blob_name) > 33 and blob_name[32] == "-" else blob_name
        try:
            source = extractors.extract_file(filename, data)
            docs.append(concepts.CorpusDoc(source, path, concepts.doc_hash(data)))
        except extractors.ExtractionError as err:
            errors.append({"file": filename, "error": str(err)})
    return docs


def _run(
    files: list[tuple[str, bytes]],
    tier: str,
    owner: str,
    user: User,
    run_id: str,
    rebuild: bool,
    reuse_sources: bool,
) -> None:
    counts: dict = {}
    errors: list = []

    def status(stage: str, done: int, total: int) -> None:
        _write_status(
            tier, owner, run_id, "running", stage, done, total, counts=counts, errors=errors
        )

    try:
        if rebuild:
            ab.reset_scope(tier, owner)

        # Stage 1 — ingest (verbatim + immutable, provenance intact).
        status(STAGES[0], 0, len(files))
        docs = (
            _ingest_stored(tier, owner, errors)
            if reuse_sources
            else _ingest_uploads(files, tier, owner, errors)
        )
        counts["sources"] = len(docs)
        if not docs:
            raise RuntimeError("No readable documents — nothing to build from.")

        # Stage 2 — concepts, document by document (cached by content hash).
        status(STAGES[1], 0, len(docs))
        doc_entries = concepts.extract_all(
            docs, tier, owner, progress=lambda d, t: status(STAGES[1], d, t)
        )
        counts["concepts_cached"] = sum(1 for e in doc_entries if e.get("cached"))
        counts["concepts_fresh"] = sum(
            1 for e in doc_entries if not e.get("cached") and not e.get("error")
        )
        errors.extend(
            {"file": e.get("source_label", "?"), "error": e["error"]}
            for e in doc_entries
            if e.get("error")
        )
        usable = [e for e in doc_entries if e.get("concepts")]
        counts["concepts"] = sum(len(e["concepts"]) for e in usable)
        if not usable:
            raise RuntimeError("Concept extraction produced nothing usable.")

        # Stage 3 — the fixed page set.
        status(STAGES[2], 0, 1)
        pageset = consolidate.build_pageset(
            usable, tier, owner, progress=lambda d, t: status(STAGES[2], d, t)
        )
        counts["pages_planned"] = len(pageset["pages"])

        # Stage 4 — synthesis, page by page.
        status(STAGES[3], 0, len(pageset["pages"]))
        written, page_errors = synthesize.write_pages(
            pageset, tier, owner, user, progress=lambda d, t: status(STAGES[3], d, t)
        )
        counts["pages"] = len(written)
        errors.extend({"file": e["title"], "error": e["error"]} for e in page_errors)
        for doc in docs:
            ab.append_ingest_log(
                IngestLogEntry(
                    partition_key=make_partition_key(tier, owner),  # type: ignore[arg-type]
                    source_type=doc.source.source_type,
                    source_ref=doc.source_ref_id,
                    mode="automatic",
                    pages_affected=[p.title for p in written if doc.source_ref_id in p.source_refs],
                    tier=tier,  # type: ignore[arg-type]
                )
            )

        # Stage 5 — link graph → communities → map, and the community-grouped
        # contents page (deterministic; reads the map cache).
        status(STAGES[4], 0, 1)
        map_data = knowledge_map.build_map(tier, owner)
        counts["communities"] = len(map_data.get("communities", []))
        wiki.regenerate_index(make_partition_key(tier, owner), tier, owner, user.id)  # type: ignore[arg-type]

        # Stage 6 — frontier (map layer only) + lint (flags only: manual mode
        # queues findings, applies nothing).
        status(STAGES[5], 0, 1)
        frontier_data = frontier.build_frontier(tier, owner, pageset, map_data)
        counts["frontier"] = len(frontier_data.get("items", []))
        lint_result = lint.run_lint(tier, owner, "manual", user)
        counts["lint"] = lint_result.get("new_findings", 0)

        _write_status(tier, owner, run_id, "done", "Complete", 1, 1, counts=counts, errors=errors)
    except Exception as err:  # noqa: BLE001 — terminal state must always be written
        traceback.print_exc()
        _write_status(
            tier,
            owner,
            run_id,
            "error",
            "Failed",
            0,
            1,
            counts=counts,
            errors=errors,
            error=str(err),
        )
