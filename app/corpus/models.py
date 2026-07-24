"""Data carried between pipeline stages.

Plain dataclasses rather than pydantic models: none of this is persisted in
Cosmos or validated at a trust boundary — it lives in run artifacts (JSON
blobs) and in memory between stages, and `asdict`/`from_dict` round-tripping
is all it needs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.models import new_id, now_iso

# Pipeline stage names, in execution order. Used as artifact filenames and as
# the progress labels on the run page.
STAGES = ["ingest", "concepts", "consolidate", "synthesis", "map", "frontier"]


@dataclass
class Passage:
    """A verbatim extract that evidences a concept, with the document it came
    from. These are load-bearing, not decorative: Stage 4 synthesizes pages
    from passages rather than from whole documents, which is the only reason
    the pipeline stays inside context at hundreds of documents."""

    text: str
    doc_ref: str  # blob path of the immutable raw source
    doc_name: str = ""  # display filename


@dataclass
class Concept:
    name: str
    description: str
    passages: list[Passage] = field(default_factory=list)

    def embedding_text(self) -> str:
        """What Stage 3 embeds for dedup — name and description together, since
        the name alone is too short to separate 'feeding' the infant-nutrition
        topic from 'feeding' the livestock one."""
        return f"{self.name}\n{self.description}"


@dataclass
class DocumentConcepts:
    """Stage 2 output for one document."""

    doc_ref: str
    doc_name: str
    concepts: list[Concept] = field(default_factory=list)
    error: str = ""  # extraction or model failure; the run continues without it


@dataclass
class PageSpec:
    """One page in the consolidated page set — Stage 3's output and Stage 4's
    input. Fixed before any page is written, so Stage 4 knows the complete set
    of link targets."""

    title: str
    description: str
    source_refs: list[str] = field(default_factory=list)  # contributing doc blob paths
    passages: list[Passage] = field(default_factory=list)  # pooled across contributing docs
    merged_from: list[str] = field(default_factory=list)  # original concept names
    # Set when the page's passages overflowed context and it had to be
    # synthesized from per-source summaries instead. Surfaced in the run view
    # so evaluation can see which pages took the quality hit.
    degraded: bool = False
    # Set when this page could not be written at all. The run continues without
    # it rather than discarding a corpus that has already cost hundreds of calls.
    error: str = ""


@dataclass
class MergeDecision:
    """One dedup cluster and what was decided about it. Kept in the artifact
    because 'are the pages the right pages' is answered by reading these, not
    by reading the finished wiki."""

    candidates: list[str]
    merged: bool
    canonical_name: str
    reasoning: str = ""


@dataclass
class StageResult:
    name: str
    started_at: str = ""
    finished_at: str = ""
    model_calls: int = 0
    note: str = ""  # human-readable one-liner for the run page
    error: str = ""
    # Live progress within a stage. Consolidate can run for twenty minutes on a
    # real corpus, and a stage that reports nothing until it finishes is
    # indistinguishable from one that has hung.
    progress_done: int = 0
    progress_total: int = 0  # 0 when the total isn't knowable up front
    progress_label: str = ""
    progress_at: str = ""  # last update; lets the reader see it's still moving


@dataclass
class RunStatus:
    """Progress and cost record for one pipeline run. Persisted after every
    stage so the run page can poll it — the pipeline runs in a background
    thread and far outlives the request that started it."""

    id: str = field(default_factory=new_id)
    tier: str = "team"
    owner: str = ""
    started_at: str = field(default_factory=now_iso)
    finished_at: str = ""
    state: str = "running"  # running | complete | failed
    current_stage: str = ""
    document_count: int = 0
    # Files that couldn't be read (wrong type, scanned PDF, empty). Recorded so
    # a document count that doesn't match what was dropped is explainable
    # rather than mysterious.
    skipped: list[str] = field(default_factory=list)
    concept_count: int = 0
    page_count: int = 0
    degraded_page_count: int = 0
    lint_finding_count: int = 0
    total_model_calls: int = 0
    stages: list[StageResult] = field(default_factory=list)
    error: str = ""

    def stage(self, name: str) -> StageResult:
        for s in self.stages:
            if s.name == name:
                return s
        result = StageResult(name=name)
        self.stages.append(result)
        return result

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RunStatus:
        stages = [StageResult(**s) for s in data.pop("stages", [])]
        return cls(stages=stages, **data)


def passage_from_dict(data: dict) -> Passage:
    return Passage(**data)


def concept_from_dict(data: dict) -> Concept:
    return Concept(
        name=data.get("name", ""),
        description=data.get("description", ""),
        passages=[passage_from_dict(p) for p in data.get("passages", [])],
    )


def page_spec_from_dict(data: dict) -> PageSpec:
    return PageSpec(
        title=data.get("title", ""),
        description=data.get("description", ""),
        source_refs=data.get("source_refs", []),
        passages=[passage_from_dict(p) for p in data.get("passages", [])],
        merged_from=data.get("merged_from", []),
        degraded=data.get("degraded", False),
    )
