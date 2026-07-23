"""Stage 3 — consolidation into the page set (MVP spec: the stage that
determines whether the output is a wiki or a filing cabinet).

Twenty documents do not map to twenty pages: a concept appearing across six
documents becomes one page those six feed. Deduplication is the hard part —
"infant feeding" / "feeding in infancy" / "breastfeeding" may be one page or
three legitimately distinct ones. Approach: embed concept names+descriptions,
cluster near-duplicates by cosine similarity (pure Python + networkx — no new
dependencies), then one bounded model call per multi-member cluster to decide
what merges and under which canonical name. Embeddings here are a *pipeline*
step, not a visualization input (the map stays link-graph-driven); the
vectors are transient in-memory state, never written to Cosmos.

Scale: hundreds of concept lists cannot be consolidated in one pass, so
consolidation goes hierarchical — batch the per-document lists into groups,
consolidate each group, then consolidate the group outputs. Balanced and
order-independent, unlike a running incremental merge.

Output: the definitive page set (`_corpus/pageset.json`), fixed before any
page is written. Each page gets its id here so Stage 4 can resolve [[links]]
against the full known set."""

import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import networkx as nx

from app import abstractions as ab
from app.config import get_settings
from app.corpus import artifacts
from app.models import new_id, now_iso

MERGE_SYSTEM = (
    "You maintain a knowledge wiki being built from a document corpus. Below "
    "are concept entries whose names/descriptions are similar enough that they "
    "MAY describe the same page. Decide how they group into wiki pages: merge "
    "entries only when they genuinely cover the same concept; keep genuinely "
    "distinct concepts separate (near-similarity is not identity — e.g. "
    "'infant feeding' and 'breastfeeding' may deserve separate pages). Reply "
    'with a JSON object exactly of the form {"groups": [{"canonical_title": '
    '"<best page title, Title Case>", "description": "<one line covering the '
    'merged concept>", "member_indices": [<0-based indices of the entries in '
    "this group>]}]}. Every input index must appear in exactly one group. "
    "Output only the JSON object."
)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _records_from_doc(doc_entry: dict) -> list[dict]:
    """Flatten one Stage 2 cache entry into per-concept records, each carrying
    a single source (ref id + label + evidencing passages)."""
    return [
        {
            "name": c["name"],
            "description": c.get("description", ""),
            "sources": [
                {
                    "source_ref_id": doc_entry["source_ref_id"],
                    "source_label": doc_entry.get("source_label", ""),
                    "passages": c.get("passages", []),
                }
            ],
        }
        for c in doc_entry.get("concepts", [])
    ]


def _merge_sources(records: list[dict]) -> list[dict]:
    """Union of the records' sources, deduped by source_ref_id with passages
    concatenated — a merged page keeps every feeding document exactly once."""
    by_ref: dict[str, dict] = {}
    for r in records:
        for src in r["sources"]:
            existing = by_ref.get(src["source_ref_id"])
            if existing:
                for p in src["passages"]:
                    if p not in existing["passages"]:
                        existing["passages"].append(p)
            else:
                by_ref[src["source_ref_id"]] = {
                    "source_ref_id": src["source_ref_id"],
                    "source_label": src["source_label"],
                    "passages": list(src["passages"]),
                }
    return list(by_ref.values())


def _embed_all(records: list[dict]) -> list[list[float]]:
    cap = max(1, get_settings().corpus_concurrency)
    texts = [f"{r['name']} — {r['description']}" for r in records]
    with ThreadPoolExecutor(max_workers=cap) as pool:
        return list(pool.map(ab.embed, texts))


def _cluster(records: list[dict]) -> list[list[int]]:
    """Near-duplicate clusters: nodes = records, an edge where cosine ≥ the
    dedup threshold, connected components = clusters."""
    vectors = _embed_all(records)
    threshold = get_settings().corpus_dedup_threshold
    graph = nx.Graph()
    graph.add_nodes_from(range(len(records)))
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            if _cosine(vectors[i], vectors[j]) >= threshold:
                graph.add_edge(i, j)
    return [sorted(c) for c in nx.connected_components(graph)]


def _decide_cluster(members: list[dict]) -> list[dict]:
    """One bounded model call deciding how a near-duplicate cluster groups
    into pages. The model may merge all, some, or none (a cluster can split
    into several pages). Malformed output after retry → keep members separate
    — over-splitting is recoverable by a later run; a bad merge is not."""
    listing = "\n".join(f"{i}. {m['name']} — {m['description']}" for i, m in enumerate(members))
    try:
        data = artifacts.call_json(listing, system=MERGE_SYSTEM, max_tokens=2000)
        groups = data.get("groups", [])
        seen: set[int] = set()
        out = []
        for g in groups:
            indices = [i for i in g.get("member_indices", []) if 0 <= int(i) < len(members)]
            indices = [i for i in indices if i not in seen]
            if not indices:
                continue
            seen.update(indices)
            grouped = [members[i] for i in indices]
            out.append(
                {
                    "name": str(g.get("canonical_title", "")).strip() or grouped[0]["name"],
                    "description": str(g.get("description", "")).strip()
                    or grouped[0]["description"],
                    "sources": _merge_sources(grouped),
                }
            )
        # Any index the model dropped stays as its own page.
        out.extend(members[i] for i in range(len(members)) if i not in seen)
        return out
    except (ValueError, TypeError):
        return members


def _consolidate_batch(records: list[dict]) -> list[dict]:
    """Steps 2–5 over one batch of concept records: embed, cluster, decide
    per multi-member cluster, pass singletons through."""
    if len(records) < 2:
        return records
    merged: list[dict] = []
    for cluster in _cluster(records):
        members = [records[i] for i in cluster]
        if len(members) == 1:
            merged.extend(members)
        else:
            merged.extend(_decide_cluster(members))
    return merged


def build_pageset(
    doc_entries: list[dict],
    tier: str,
    owner: str,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """The definitive page set from all per-document concept lists. Uses
    hierarchical consolidation when the corpus is large (order-independent,
    unlike an incremental merge); single-level otherwise."""
    batch = max(2, get_settings().corpus_consolidate_batch)
    groups: list[list[dict]]
    if len(doc_entries) > batch * batch:
        groups = [doc_entries[i : i + batch] for i in range(0, len(doc_entries), batch)]
    else:
        groups = [doc_entries]

    consolidated_groups: list[list[dict]] = []
    for gi, group in enumerate(groups):
        records = [r for entry in group for r in _records_from_doc(entry)]
        consolidated_groups.append(_consolidate_batch(records))
        if progress:
            progress(gi + 1, len(groups) + (1 if len(groups) > 1 else 0))

    if len(consolidated_groups) > 1:
        records = [r for g in consolidated_groups for r in g]
        final = _consolidate_batch(records)
        if progress:
            progress(len(groups) + 1, len(groups) + 1)
    else:
        final = consolidated_groups[0]

    # Fix ids + enforce case-insensitively unique titles before any page is
    # written — Stage 4 links resolve against exactly this set.
    pages = []
    seen_titles: set[str] = set()
    for r in final:
        title = r["name"].strip()
        n = 2
        while title.lower() in seen_titles:
            title = f"{r['name'].strip()} ({n})"
            n += 1
        seen_titles.add(title.lower())
        pages.append(
            {
                "page_id": new_id(),
                "title": title,
                "description": r["description"],
                "sources": r["sources"],
            }
        )

    pageset = {"generated_at": now_iso(), "pages": pages}
    artifacts.write_json(artifacts.pageset_path(tier, owner), pageset)
    return pageset
