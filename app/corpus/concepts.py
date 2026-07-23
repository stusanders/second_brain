"""Stage 2 — concept extraction, document by document (MVP spec).

One model pass per document, independently: structured list of the concepts
the document covers, each with a one-line description and verbatim evidence
passages. Document-by-document is deliberate — context clarity per source,
trivial parallelism, small cheap calls. No page creation here.

Extractions are cached by sha256 of the raw document bytes
(`_corpus/concepts/{hash}.json`) and survive rebuilds (reset_scope spares
this prefix), so eval re-runs only pay for changed documents."""

import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.config import get_settings
from app.corpus import artifacts
from app.ingest.extractors import ExtractedSource
from app.models import now_iso

CONCEPT_SYSTEM = (
    "You are indexing one source document for a knowledge wiki. Identify the "
    "distinct concepts, entities, or topics this document substantively covers "
    "— typically 3-12; fewer for a narrow document, more for a broad one. Skip "
    "boilerplate and passing mentions. Reply with a JSON object exactly of the "
    'form {"concepts": [{"name": "<concise concept name>", "description": '
    '"<one line: what this document says about it>", "passages": ["<short '
    'VERBATIM quote from the document evidencing it>", ...]}]}. Passages must '
    "be exact quotes (1-3 per concept, each under 500 characters), never "
    "paraphrases. Output only the JSON object."
)


@dataclass
class CorpusDoc:
    """One ingested document entering Stage 2: extracted text + provenance."""

    source: ExtractedSource
    source_ref_id: str  # immutable raw-source blob path (provenance)
    source_hash: str  # sha256 of raw bytes — the concept-cache key


def doc_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_concepts(doc: CorpusDoc, tier: str, owner: str) -> dict:
    """Concept list for one document — cache hit or one model call. Returns
    the cache-file dict; `cached` marks whether a model call was skipped."""
    path = artifacts.concept_cache_path(tier, owner, doc.source_hash)
    cached = artifacts.read_json(path)
    if cached and cached.get("concepts"):
        return {**cached, "cached": True}

    data = artifacts.call_json(
        f"<document filename={doc.source.source_ref!r}>\n{doc.source.text}\n</document>",
        system=CONCEPT_SYSTEM,
        max_tokens=4000,
    )
    concepts = [
        {
            "name": str(c.get("name", "")).strip(),
            "description": str(c.get("description", "")).strip(),
            "passages": [str(p) for p in c.get("passages", []) if str(p).strip()],
        }
        for c in data.get("concepts", [])
        if str(c.get("name", "")).strip()
    ]
    entry = {
        "source_hash": doc.source_hash,
        "source_ref_id": doc.source_ref_id,
        "source_label": doc.source.source_ref,
        "extracted_at": now_iso(),
        "concepts": concepts,
    }
    artifacts.write_json(path, entry)
    return {**entry, "cached": False}


def extract_all(
    docs: list[CorpusDoc],
    tier: str,
    owner: str,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """Stage 2 over the whole corpus, parallelized with a concurrency cap.
    Per-document failures don't sink the run: a failed document yields an
    entry with an `error` field and no concepts, reported at the end."""
    cap = max(1, get_settings().corpus_concurrency)
    results: list[dict | None] = [None] * len(docs)

    def _one(i: int) -> None:
        try:
            results[i] = extract_concepts(docs[i], tier, owner)
        except Exception as err:  # noqa: BLE001 — recorded per-document, run continues
            results[i] = {
                "source_hash": docs[i].source_hash,
                "source_ref_id": docs[i].source_ref_id,
                "source_label": docs[i].source.source_ref,
                "concepts": [],
                "error": str(err),
            }
        if progress:
            progress(sum(r is not None for r in results), len(docs))

    with ThreadPoolExecutor(max_workers=cap) as pool:
        list(pool.map(_one, range(len(docs))))
    return [r for r in results if r is not None]
