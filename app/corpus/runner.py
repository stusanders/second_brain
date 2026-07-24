"""Pipeline orchestration, background execution, and run artifacts.

A full corpus pass makes hundreds of model calls and will far exceed any HTTP
request timeout, and this POC has no scheduler or job runner. So the run
happens on a background thread and reports through a status blob the run page
polls — the honest minimum that keeps the UI responsive without pretending to
have infrastructure that isn't there.

Artifacts live under `{tier}/{owner}/_pipeline/{run_id}/`, following the
`_`-prefix convention already used by the lint queue, the map cache and derived
views; `blob_store.list_page_paths` excludes those names, so nothing here is
ever mistaken for wiki content. One artifact per stage, written as the stage
finishes, plus `status.json`.

The artifacts are not debug output. The MVP exists to answer whether this
synthesis is any good, and the decisive question — are these the right pages? —
is answered by reading Stage 2 and Stage 3 output directly, not by inspecting
the finished wiki.
"""

import json
import threading
import time
import traceback
from dataclasses import asdict

from app import blob_store, knowledge_map, lint, wiki
from app.corpus import concepts, consolidate, frontier, synthesis
from app.corpus.models import RunStatus, StageResult
from app.models import make_partition_key, now_iso


def _run_prefix(tier: str, owner: str, run_id: str) -> str:
    return f"{tier}/{owner}/_pipeline/{run_id}/"


def _status_path(tier: str, owner: str, run_id: str) -> str:
    return _run_prefix(tier, owner, run_id) + "status.json"


def artifact_path(tier: str, owner: str, run_id: str, stage: str) -> str:
    return _run_prefix(tier, owner, run_id) + f"{stage}.json"


# ------------------------------------------------------------------ artifacts


def save_status(status: RunStatus) -> None:
    blob_store.write_text(
        _status_path(status.tier, status.owner, status.id),
        json.dumps(status.to_dict(), indent=2),
    )


def load_status(tier: str, owner: str, run_id: str) -> RunStatus | None:
    raw = blob_store.read_text(_status_path(tier, owner, run_id))
    return RunStatus.from_dict(json.loads(raw)) if raw else None


def save_artifact(tier: str, owner: str, run_id: str, stage: str, payload: dict) -> None:
    blob_store.write_text(
        artifact_path(tier, owner, run_id, stage), json.dumps(payload, indent=2, default=str)
    )


def load_artifact(tier: str, owner: str, run_id: str, stage: str) -> dict | None:
    raw = blob_store.read_text(artifact_path(tier, owner, run_id, stage))
    return json.loads(raw) if raw else None


def latest_run_id(tier: str, owner: str) -> str | None:
    """Most recent run for a scope, by artifact write order. Used to send a
    returning user back to their run rather than making them keep the URL."""
    prefix = f"{tier}/{owner}/_pipeline/"
    run_ids = {
        path[len(prefix) :].split("/", 1)[0]
        for path in blob_store.list_paths(prefix)
        if path.endswith("status.json")
    }
    statuses = [s for rid in run_ids if (s := load_status(tier, owner, rid))]
    if not statuses:
        return None
    return max(statuses, key=lambda s: s.started_at).id


# -------------------------------------------------------------------- staging


class Progress:
    """Live within-stage progress, persisted to the status blob.

    Writes are throttled: a blob write per model call would add latency to a
    stage already making hundreds of them, and the reader only needs to see
    movement, not every increment. Thread-safe because Stage 2 fans out across
    a thread pool.
    """

    MIN_INTERVAL_SECONDS = 2.0

    def __init__(self, status: RunStatus, result: StageResult) -> None:
        self._status = status
        self._result = result
        self._lock = threading.Lock()
        self._last_write = 0.0

    def start(self, total: int = 0, label: str = "") -> None:
        with self._lock:
            self._result.progress_done = 0
            self._result.progress_total = total
            self._result.progress_label = label
        self._flush(force=True)

    def tick(self, n: int = 1, label: str | None = None) -> None:
        with self._lock:
            self._result.progress_done += n
            if label is not None:
                self._result.progress_label = label
        self._flush()

    def set_label(self, label: str) -> None:
        with self._lock:
            self._result.progress_label = label
        self._flush()

    def _flush(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_write < self.MIN_INTERVAL_SECONDS:
                return
            self._last_write = now
            self._result.progress_at = now_iso()
            # Keep the headline call count moving too, not just at stage end.
            self._result.model_calls = max(self._result.model_calls, self._result.progress_done)
            self._status.total_model_calls = sum(s.model_calls for s in self._status.stages)
        save_status(self._status)


class NullProgress:
    """No-op, so the stage modules stay callable without a runner."""

    def start(self, total: int = 0, label: str = "") -> None: ...
    def tick(self, n: int = 1, label: str | None = None) -> None: ...
    def set_label(self, label: str) -> None: ...


class StageRecorder:
    """Context manager that times a stage, records its model-call count, and
    persists status on exit — so a run page polling mid-pipeline always sees
    where the run has got to, and a crash leaves the failure recorded rather
    than a run stuck on 'running' forever."""

    def __init__(self, status: RunStatus, name: str) -> None:
        self.status = status
        self.result: StageResult = status.stage(name)
        self.progress = Progress(status, self.result)

    # Proxies so a stage body can set `stage.note` / `stage.model_calls`
    # naturally while still reaching `stage.progress`.
    @property
    def note(self) -> str:
        return self.result.note

    @note.setter
    def note(self, value: str) -> None:
        self.result.note = value

    @property
    def model_calls(self) -> int:
        return self.result.model_calls

    @model_calls.setter
    def model_calls(self, value: int) -> None:
        self.result.model_calls = value

    def __enter__(self) -> "StageRecorder":
        self.status.current_stage = self.result.name
        self.result.started_at = now_iso()
        save_status(self.status)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.result.finished_at = now_iso()
        if exc:
            self.result.error = f"{exc_type.__name__}: {exc}"
            self.status.state = "failed"
            self.status.error = self.result.error
        self.status.total_model_calls = sum(s.model_calls for s in self.status.stages)
        save_status(self.status)
        return False  # never swallow; run() handles the failure


# ------------------------------------------------------------------ execution


_threads: dict[str, threading.Thread] = {}


def start_run(
    tier: str, owner: str, documents: list[dict], user, skipped: list[str] | None = None
) -> RunStatus:
    """Persist a starting status, then run the pipeline on a background thread.
    Returns immediately with the status so the caller can redirect to the run
    page."""
    status = RunStatus(tier=tier, owner=owner, document_count=len(documents), skipped=skipped or [])
    save_status(status)

    thread = threading.Thread(
        target=_run_guarded, args=(status, documents, user), daemon=True, name=f"corpus-{status.id}"
    )
    _threads[status.id] = thread
    thread.start()
    return status


def wait_for(run_id: str, timeout: float = 30.0) -> None:
    """Block until a run's thread finishes. Only meaningful in-process — a
    real deployment reads the status blob instead — but it lets tests assert on
    a finished run without sleeping and hoping."""
    thread = _threads.get(run_id)
    if thread:
        thread.join(timeout)


def _run_guarded(status: RunStatus, documents: list[dict], user) -> None:
    """Any exception here happens on a thread nobody is waiting on, so it must
    land in the status blob or it is invisible."""
    try:
        run_pipeline(status, documents, user)
    except Exception as e:  # noqa: BLE001 — background thread; must not die silently
        status.state = "failed"
        status.error = f"{type(e).__name__}: {e}"
        status.finished_at = now_iso()
        save_status(status)
        traceback.print_exc()


def run_pipeline(status: RunStatus, documents: list[dict], user) -> RunStatus:
    """Stages 1-6. Stages beyond ingest arrive in later phases; the ordering
    and the artifact-per-stage contract are fixed here."""
    tier, owner, run_id = status.tier, status.owner, status.id

    with StageRecorder(status, "ingest") as stage:
        save_artifact(tier, owner, run_id, "ingest", {"documents": documents})
        stage.note = f"{len(documents)} document(s) stored and extracted"

    with StageRecorder(status, "concepts") as stage:
        per_document, calls = concepts.extract_all(documents, progress=stage.progress)
        stage.model_calls = calls
        status.concept_count = sum(len(d.concepts) for d in per_document)
        failed = [d for d in per_document if d.error]
        stage.note = f"{status.concept_count} concepts from {len(per_document)} document(s)"
        if failed:
            stage.note += f" ({len(failed)} document(s) could not be read)"
        save_artifact(
            tier,
            owner,
            run_id,
            "concepts",
            {"documents": [asdict(d) for d in per_document]},
        )

    with StageRecorder(status, "consolidate") as stage:
        page_set, decisions, calls = consolidate.consolidate(per_document, progress=stage.progress)
        stage.model_calls = calls
        status.page_count = len(page_set)
        stage.note = (
            f"{status.concept_count} concepts consolidated into {len(page_set)} page(s)"
            if status.concept_count
            else "nothing to consolidate"
        )
        save_artifact(
            tier,
            owner,
            run_id,
            "consolidate",
            {
                "pages": [asdict(p) for p in page_set],
                "merge_decisions": [asdict(d) for d in decisions],
            },
        )

    with StageRecorder(status, "synthesis") as stage:
        pages, contradictions, calls = synthesis.synthesize(
            page_set, tier, owner, progress=stage.progress
        )
        stage.model_calls = calls
        synthesis.record_contradictions(tier, owner, contradictions)
        status.degraded_page_count = sum(1 for p in page_set if p.degraded)
        stage.note = f"{len(pages)} page(s) written"
        if status.degraded_page_count:
            stage.note += f", {status.degraded_page_count} from summaries"
        if contradictions:
            stage.note += f"; {len(contradictions)} source disagreement(s) flagged"
        save_artifact(
            tier,
            owner,
            run_id,
            "synthesis",
            {
                "pages": [{"id": p.id, "title": p.title, "links_out": p.links_out} for p in pages],
                "contradictions": contradictions,
            },
        )

    with StageRecorder(status, "map") as stage:
        # The link graph, Louvain partition and community naming are already
        # built (app.knowledge_map) — nothing corpus-specific about them.
        artifact = knowledge_map.build_map(tier, owner)
        stage.model_calls = len(artifact.get("communities", []))
        # Contents groups by the map communities, so it must follow the map.
        wiki.regenerate_index(
            make_partition_key(tier, owner), tier, owner, author_id="system:corpus"
        )
        stage.note = (
            f"{len(artifact.get('communities', []))} community group(s), "
            f"{len(artifact.get('unplaced', []))} unplaced"
        )

    with StageRecorder(status, "frontier") as stage:
        entries, calls = frontier.predict(
            [n["title"] for n in artifact.get("nodes", {}).values()],
            [c["name"] for c in artifact.get("communities", [])],
        )
        stage.model_calls = calls
        # Stored inside the map artifact only — never as pages, so the frontier
        # structurally cannot reach the wiki, index, search or export.
        knowledge_map.save_frontier(tier, owner, entries)
        stage.note = f"{len(entries)} predicted gap(s)"

        # Lint closes the run rather than waiting to be clicked: a corpus
        # spanning years contains superseded positions and sources that
        # disagree, and surfacing that on day one is one of the more striking
        # things this can show a team about its own material.
        #
        # But by this point the wiki, map and frontier are all written — the run
        # has succeeded. Lint is a bounded flag-generator over that finished
        # output, so a failure here must not mark the whole build failed and
        # throw away everything above it. Record the problem and complete.
        try:
            lint.run_lint(tier, owner, mode="manual", user=user, max_judgment_pages=len(page_set))
            status.lint_finding_count = lint.unreviewed_counts(tier, owner)["total"]
            stage.note += f"; {status.lint_finding_count} finding(s) to review"
        except Exception as e:  # noqa: BLE001 — the wiki is already built; lint is optional
            stage.note += f"; lint did not complete ({type(e).__name__})"

    status.state = "complete"
    status.current_stage = ""
    status.finished_at = now_iso()
    save_status(status)
    return status
