"""Stage 3 — consolidating per-document concept lists into the page set.

This is the stage that decides whether the output is a wiki or a filing
cabinet. **Twenty documents do not map to twenty pages.** A concept appearing
across six documents becomes one page those six feed, rather than being
restated six times.

Three steps, cheapest first:

1. **Exact-name merge** (free). Thirteen documents on one policy area name the
   same things repeatedly; folding identical names costs nothing and removes
   the largest single source of duplication before any model call.

2. **Semantic clustering** (embeddings). Group concepts whose embeddings are
   close, so the model only ever judges candidates that are plausibly the same
   subject.

3. **One judgment call per cluster** (model). Each cluster becomes one or more
   pages — groups, not a merge/don't-merge boolean, because a cluster can hold
   two genuinely distinct subjects. "Infant feeding" / "feeding in infancy" /
   "breastfeeding" may be one page or three, and only judgment settles it.

**Why clustering comes before the model calls.** The first design batched
concepts alphabetically and consolidated recursively. It failed on a real
corpus: 460 concepts produced 434 pages, of which only 18 were fed by more than
one document. Alphabetical batching means near-synonyms almost never land in
the same call — "Access to treatment" and "Treatment delays" cannot be compared
if they are hundreds of entries apart — so the model was asked, 298 times, to
merge sets of unrelated things, and correctly declined. Grouping by embedding
first is what puts mergeable concepts in front of the model at all.

Embeddings here are a *pipeline* step, not a visualization input — the map
stays driven by the explicit [[link]] graph, per the standing rule.

Output is the definitive page set, fixed before any page is written, so Stage 4
knows the complete set of legal link targets.
"""

import numpy as np

from app import abstractions as ab
from app.corpus.models import Concept, DocumentConcepts, MergeDecision, PageSpec, Passage

# Cosine similarity above which two concepts are treated as *candidates* for
# being the same page. The model call that follows is what actually decides, so
# this only has to be permissive enough not to miss a genuine pair — a missed
# candidate can never be recovered, a spurious one costs one cheap call.
#
# Set from the first real corpus (538 cancer-policy concepts, text-embedding-
# 3-small): the pairwise-cosine median was 0.35, genuine same-subject pairs sat
# at 0.85-0.95, and related-but-distinct around 0.60-0.68. 0.68 brings ~370 of
# 538 concepts into a cluster where the model can judge them, without dragging
# in merely-adjacent topics. Re-measure with scripts/measure_similarity.py if
# the embedding model changes.
DEDUP_SIMILARITY_THRESHOLD = 0.68

# Ceiling on one cluster. Similarity is not transitive, so unbounded
# connected-components clustering chains A~B~C into a single blob even when A
# and C are unrelated. Capping the cluster size bounds that chaining, and the
# model still gets to split a cluster into several pages.
MAX_CLUSTER_MEMBERS = 15

CLUSTER_RESOLVE_SYSTEM = (
    "You are deciding what pages a knowledge wiki should have. You are given a "
    "numbered group of closely-related concepts, each extracted from a source "
    "document. Different documents describe the same subject in different "
    "words, and one page should serve all of them.\n\n"
    "Group the numbered inputs into the smallest set of pages a reader would "
    "actually want. Merge freely: near-synonyms, one concept phrased from two "
    "angles, and a general subject with its document-specific restatement all "
    "belong on one page. Only keep two pages apart when a reader looking for "
    "one would be actively misled by finding the other.\n\n"
    "Every input number must appear in exactly one group — do not drop any, and "
    "do not invent subjects the inputs don't mention.\n\n"
    'Reply with JSON only: {"groups": [{"name": "canonical page title", '
    '"description": "one sentence", "members": [1, 4, 5], '
    '"reasoning": "one short sentence"}]}'
)


# --------------------------------------------------------- merging concepts


def _merge_exact_names(concepts: list[Concept]) -> list[Concept]:
    """Fold concepts that share a name, before any model call. Free,
    deterministic, and it removes the largest single source of duplication —
    thirteen documents on one policy area name the same things repeatedly."""
    grouped: dict[str, Concept] = {}
    for concept in concepts:
        key = concept.name.strip().lower()
        if key in grouped:
            grouped[key].passages.extend(concept.passages)
            if len(concept.description) > len(grouped[key].description):
                grouped[key].description = concept.description
        else:
            grouped[key] = Concept(
                name=concept.name.strip(),
                description=concept.description,
                passages=list(concept.passages),
            )
    return list(grouped.values())


def _cluster_by_similarity(vectors: list[list[float]]) -> list[list[int]]:
    """Group concepts whose embeddings are close, so the model only ever judges
    candidates that are actually plausible.

    Greedy, highest-similarity-first union with a size cap rather than plain
    connected components: similarity isn't transitive, so unbounded chaining
    merges A~B~C into one blob even when A and C are unrelated. One matrix
    multiply — pairwise comparison in pure Python is not viable at this size.
    """
    n = len(vectors)
    if n < 2:
        return [[i] for i in range(n)]

    matrix = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = matrix / norms
    similarity = unit @ unit.T

    parent = list(range(n))
    size = [1] * n

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    rows, cols = np.where(np.triu(similarity, k=1) >= DEDUP_SIMILARITY_THRESHOLD)
    order = np.argsort(-similarity[rows, cols])  # strongest pairs first
    for idx in order:
        a, b = find(int(rows[idx])), find(int(cols[idx]))
        if a == b or size[a] + size[b] > MAX_CLUSTER_MEMBERS:
            continue
        parent[a] = b
        size[b] += size[a]

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _resolve_cluster(members: list[Concept]) -> tuple[list[Concept], list[MergeDecision], int]:
    """One model call deciding what pages a cluster of similar concepts becomes.

    Returns pages, not a yes/no: a cluster can legitimately contain two distinct
    subjects, and forcing a binary merge decision on the whole group loses that.
    """
    if len(members) < 2:
        return members, [], 0

    # Members are referenced by 1-based number, not by name. Asking the model to
    # echo concept names verbatim is fragile — it paraphrases them, the strings
    # don't match, and the whole cluster silently falls through unmerged (the
    # first real run merged only 8 of 74 clusters this way). A number it echoes
    # reliably.
    listing = "\n".join(f"{i}. {c.name}: {c.description}" for i, c in enumerate(members, 1))
    payload = ab.call_model_json(
        "Group these numbered concepts into the pages a wiki should have.",
        context=listing,
        system=CLUSTER_RESOLVE_SYSTEM,
    )

    claimed: set[int] = set()
    resolved: list[Concept] = []
    decisions: list[MergeDecision] = []

    for group in payload.get("groups") or []:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name", "")).strip()
        idxs = _member_indices(group.get("members"), len(members))
        idxs = [i for i in idxs if i not in claimed]  # first group to claim wins
        if not name or not idxs:
            continue
        claimed.update(idxs)
        picked = [members[i] for i in idxs]
        passages: list[Passage] = []
        for c in picked:
            passages.extend(c.passages)
        longest = max(picked, key=lambda c: len(c.description))
        resolved.append(Concept(name=name, description=longest.description, passages=passages))
        decisions.append(
            MergeDecision(
                candidates=[c.name for c in picked],
                merged=len(picked) > 1,
                canonical_name=name,
                reasoning=str(group.get("reasoning", "")).strip(),
            )
        )

    # Anything the model failed to place is carried through unchanged. Dropping
    # a concept because a call forgot it would lose its evidence silently.
    for i, concept in enumerate(members):
        if i not in claimed:
            resolved.append(concept)
    return resolved, decisions, 1


def _member_indices(raw, count: int) -> list[int]:
    """Parse a group's `members` into 0-based indices, tolerating the shapes a
    model actually returns: integers, numeric strings, or "3." / "#3". Anything
    out of range is dropped rather than trusted."""
    out: list[int] = []
    for m in raw or []:
        digits = "".join(ch for ch in str(m) if ch.isdigit())
        if not digits:
            continue
        i = int(digits) - 1
        if 0 <= i < count and i not in out:
            out.append(i)
    return out


def consolidate_concepts(
    per_document: list[DocumentConcepts],
    progress=None,
) -> tuple[list[Concept], list[MergeDecision], int]:
    """Exact-name merge, then semantic clustering, then one judgment call per
    cluster.

    Clustering comes *before* the model calls deliberately. An earlier design
    batched concepts alphabetically, which meant near-synonyms almost never
    landed in the same call — "Access to treatment" and "Treatment delays" are
    far apart alphabetically and can never be compared. Grouping by embedding
    first is what puts mergeable concepts in front of the model at all, and it
    turns a many-round recursive merge into one call per cluster.
    """
    concepts: list[Concept] = []
    for doc in per_document:
        concepts.extend(doc.concepts)
    if not concepts:
        return [], [], 0

    concepts = _merge_exact_names(concepts)
    concepts.sort(key=lambda c: c.name.lower())  # stable, order-independent
    if progress:
        progress.start(label=f"{len(concepts)} concepts after exact-name merge")

    vectors = ab.embed_many([c.embedding_text() for c in concepts])
    clusters = _cluster_by_similarity(vectors)
    judged = [c for c in clusters if len(c) > 1]
    if progress:
        progress.start(total=len(judged), label=f"{len(judged)} cluster(s) to resolve")

    resolved: list[Concept] = []
    decisions: list[MergeDecision] = []
    calls = 0
    for cluster in clusters:
        members = [concepts[i] for i in cluster]
        out, decs, n = _resolve_cluster(members)
        resolved.extend(out)
        decisions.extend(decs)
        calls += n
        if n and progress:
            progress.tick(label=f"resolved {calls}/{len(judged)} cluster(s)")

    # Names can converge after resolution (two clusters both landing on
    # "Health inequalities"), so fold once more on the exact name.
    resolved = _merge_exact_names(resolved)
    return resolved, decisions, calls


# ------------------------------------------------------------- the page set


def _dedupe_passages(passages: list[Passage]) -> list[Passage]:
    seen: set[tuple[str, str]] = set()
    unique: list[Passage] = []
    for p in passages:
        key = (p.doc_ref, p.text)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def to_page_set(concepts: list[Concept]) -> list[PageSpec]:
    """Turn surviving concepts into the definitive page set. Titles are made
    unique here: two pages with the same title would collide on slug at write
    time and silently overwrite each other."""
    pages: list[PageSpec] = []
    used_titles: set[str] = set()
    for concept in concepts:
        title = concept.name.strip()
        if not title:
            continue
        base, suffix = title, 2
        while title.lower() in used_titles:
            title = f"{base} ({suffix})"
            suffix += 1
        used_titles.add(title.lower())

        passages = _dedupe_passages(concept.passages)
        # A page with no evidence has nothing to be written from and nothing to
        # trace back to. Synthesizing one would produce unsourced content in a
        # wiki whose credibility rests on every page citing its documents.
        if not passages:
            continue
        pages.append(
            PageSpec(
                title=title,
                description=concept.description,
                source_refs=sorted({p.doc_ref for p in passages}),
                passages=passages,
                merged_from=sorted({p.doc_name for p in passages if p.doc_name}),
            )
        )
    pages.sort(key=lambda p: p.title.lower())
    return pages


def consolidate(
    per_document: list[DocumentConcepts],
    progress=None,
) -> tuple[list[PageSpec], list[MergeDecision], int]:
    """Full Stage 3: exact-name merge, semantic clustering, one judgment call
    per cluster, then the page set. Returns (page set, decisions, model calls)."""
    survivors, decisions, calls = consolidate_concepts(per_document, progress=progress)
    return to_page_set(survivors), decisions, calls
