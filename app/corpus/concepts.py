"""Stage 2 — concept extraction, one document at a time.

One pass per document, independently. Document-by-document is deliberate: it
keeps context clear per source, parallelizes trivially, and keeps each call
small and cheap. The output is per-document concept lists, not wiki pages —
nothing here decides what a page is; that is Stage 3's job.

**Passages are the load-bearing part.** Each concept carries the verbatim
extracts that evidence it. Stage 4 synthesizes pages from those extracts rather
than from whole documents, which is the only reason the pipeline stays inside
context at hundreds of documents — the same discipline a deep-research agent
uses: never pass whole documents where extracts will do. A concept without
passages forces Stage 4 back onto full source text and the pipeline stops
scaling.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from app import abstractions as ab
from app.corpus.models import Concept, DocumentConcepts, Passage

# Azure OpenAI enforces per-minute token and request limits. Firing hundreds of
# documents at once trips them; capping is trivial and discovering it mid-run
# is not.
MAX_CONCURRENT_EXTRACTIONS = 8
RATE_LIMIT_RETRIES = 4
RETRY_INITIAL_SECONDS = 2.0

# Documents longer than this are chunked before extraction and their concepts
# merged afterwards. Well inside the model's window, leaving room for the
# instruction and the structured reply.
MAX_CHARS_PER_CALL = 60_000

# Guidance, not a hard cap. The first real run extracted ~35 subjects per
# document, which produced a page set almost as large as the concept list —
# document section headings rather than subjects a wiki would have a page for.
# Asking for a coarser grain is the cheapest lever on that.
TARGET_CONCEPTS_PER_DOCUMENT = 15

EXTRACT_SYSTEM = (
    "You are cataloguing what a single document covers, as preparation for "
    "building a knowledge wiki about a subject area.\n\n"
    "Name the subjects this document addresses, at the level a wiki page would "
    "sit at — the level a knowledgeable reader would recognise as a topic in "
    "its own right, not a heading in this particular document. Other documents "
    "in this corpus cover the same subjects in different words, and a later "
    "stage has to recognise them as the same thing, so use the general name for "
    "a subject rather than this document's phrasing of it.\n\n"
    "Concretely: 'Cervical screening intervals' is a subject. 'Age-related "
    "scope and exclusion considerations' is this document's section heading and "
    "is not. 'Health inequalities in screening uptake' is a subject; 'Barriers "
    "to uptake among high-risk individuals in the 2023 pilot' is a finding "
    "about one — record it as evidence for the subject, not as its own entry.\n\n"
    f"Aim for {TARGET_CONCEPTS_PER_DOCUMENT} or so subjects for a substantial "
    "document, fewer for a short one. Prefer too few and broad over too many "
    "and narrow. Do not invent subjects the document does not discuss, and do "
    "not treat the document's own title as a subject.\n\n"
    "For each subject, quote one to three SHORT verbatim passages from the "
    "document that evidence it. Quote exactly — these extracts are the material "
    "a later stage writes from, so they must be faithful and self-contained.\n\n"
    'Reply with JSON only, of the form: {"concepts": [{"name": "...", '
    '"description": "one sentence", "passages": ["verbatim quote", ...]}]}'
)


def _split_oversized(segment: str) -> list[str]:
    """Hard-split a single segment that is itself longer than the limit.

    Necessary because paragraph breaks are not guaranteed: pypdf joins pages
    with single newlines, so a long government PDF can arrive as one
    unbroken run of text with no blank lines anywhere. Splitting only on
    "\\n\\n" would hand that straight to the model as one enormous call.
    Prefers a sentence boundary near the cut so quotes stay usable."""
    parts: list[str] = []
    while len(segment) > MAX_CHARS_PER_CALL:
        window = segment[:MAX_CHARS_PER_CALL]
        cut = max(window.rfind(". "), window.rfind("\n"))
        if cut < MAX_CHARS_PER_CALL // 2:  # no sensible boundary; cut bluntly
            cut = MAX_CHARS_PER_CALL
        parts.append(segment[:cut])
        segment = segment[cut:].lstrip()
    if segment.strip():
        parts.append(segment)
    return parts


def _chunks(text: str) -> list[str]:
    """Split a document into extraction-sized pieces, on paragraph boundaries
    where they exist so a quote is unlikely to be cut in half."""
    if len(text) <= MAX_CHARS_PER_CALL:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        if len(para) > MAX_CHARS_PER_CALL:
            if current.strip():
                chunks.append(current)
                current = ""
            chunks.extend(_split_oversized(para))
            continue
        if len(current) + len(para) + 2 > MAX_CHARS_PER_CALL and current:
            chunks.append(current)
            current = ""
        current += para + "\n\n"
    if current.strip():
        chunks.append(current)
    return chunks


def _call_with_retry(prompt: str, context: str) -> dict:
    """Retry on rate limits with exponential backoff. Anything else raises —
    a malformed corpus should fail loudly, not be silently half-processed."""
    delay = RETRY_INITIAL_SECONDS
    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            return ab.call_model_json(prompt, context=context, system=EXTRACT_SYSTEM)
        except Exception as e:  # noqa: BLE001 — SDK error types vary by transport
            if "429" not in str(e) and "rate limit" not in str(e).lower():
                raise
            if attempt == RATE_LIMIT_RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2
    return {}


def _parse(payload: dict, doc_ref: str, doc_name: str) -> list[Concept]:
    """Defensive: JSON mode guarantees syntax, not shape."""
    concepts: list[Concept] = []
    for item in payload.get("concepts") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        raw_passages = item.get("passages") or []
        passages = [
            Passage(text=str(p).strip(), doc_ref=doc_ref, doc_name=doc_name)
            for p in raw_passages
            if str(p).strip()
        ]
        concepts.append(
            Concept(
                name=name,
                description=str(item.get("description", "")).strip(),
                passages=passages,
            )
        )
    return concepts


def _merge_within_document(concepts: list[Concept]) -> list[Concept]:
    """A chunked document can name the same concept in several chunks. Merge on
    exact lowercased name — cross-document merging is Stage 3's job and needs
    judgment; this is just tidying one document's own output."""
    merged: dict[str, Concept] = {}
    for concept in concepts:
        key = concept.name.lower()
        if key in merged:
            merged[key].passages.extend(concept.passages)
            if len(concept.description) > len(merged[key].description):
                merged[key].description = concept.description
        else:
            merged[key] = concept
    return list(merged.values())


def extract_document(document: dict) -> DocumentConcepts:
    """One document in, its concept list out. Never raises: a document that
    fails to process records the error and the run continues without it —
    losing one source is better than losing the corpus."""
    doc_ref, doc_name = document["doc_ref"], document["doc_name"]
    result = DocumentConcepts(doc_ref=doc_ref, doc_name=doc_name)
    try:
        concepts: list[Concept] = []
        for chunk in _chunks(document.get("text", "")):
            payload = _call_with_retry(
                f"Catalogue what this document ('{doc_name}') covers.", chunk
            )
            concepts.extend(_parse(payload, doc_ref, doc_name))
        result.concepts = _merge_within_document(concepts)
    except Exception as e:  # noqa: BLE001 — one bad source must not kill the run
        result.error = f"{type(e).__name__}: {e}"
    return result


def extract_all(documents: list[dict], progress=None) -> tuple[list[DocumentConcepts], int]:
    """Fan out across documents under the concurrency cap. Returns the results
    in input order (not completion order, which would make runs non-reproducible
    downstream) plus the number of model calls made."""
    if not documents:
        return [], 0

    total_calls = sum(len(_chunks(d.get("text", ""))) for d in documents)
    if progress:
        progress.start(total=len(documents), label="documents read")

    def _one(document: dict) -> DocumentConcepts:
        result = extract_document(document)
        if progress:
            progress.tick()
        return result

    workers = min(MAX_CONCURRENT_EXTRACTIONS, len(documents))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_one, documents))
    return results, total_calls
