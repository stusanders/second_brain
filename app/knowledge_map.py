"""Knowledge map: whole-wiki structure derived from the explicit [[link]]
graph — NOT embeddings (build spec: "Radial knowledge map ... Grouping is
conceptual, derived from the explicit [[link]] graph — not semantic/embedding
similarity"). The map shows the structure the agent actually *built* through
filing and cross-referencing; grouping by embedding similarity would throw
away exactly the curation the pattern is about. Embeddings stay confined to
search and conflict detection elsewhere in the app.

Mechanism: build an undirected graph over pages (edges = [[wikilinks]]), run
Louvain community detection (networkx native, seeded for stability — no model
calls), then one small model call per community to name it. The result is
cached as a non-indexed blob ({tier}/{owner}/_map/map.json), following the same
"_"-prefixed convention as the lint queue and derived views (excluded from the
page index by blob_store.list_page_paths). Both the radial map view and the
contents page read this cache, so community naming isn't re-run on every page
load.

Note on stability: the *partition* is deterministic (fixed Louvain seed), but
community *names* come from a model call over sample titles — so a name can
churn across regenerations (e.g. "Client relationships" -> "Client work") when
membership shifts slightly, even for an essentially unchanged cluster. That is
expected and acceptable: names are cosmetic labels over a deterministic graph,
not identifiers.
"""

import json

import networkx as nx

from app import abstractions as ab
from app import blob_store
from app.models import make_partition_key, now_iso
from app.wiki import INDEX_TITLE

# Fixed seed so the same link graph yields the same partition across runs —
# the map's shape only changes when links actually change, not on every regen.
LOUVAIN_SEED = 1

# Cost discipline: cap how many member titles a naming call sees.
NAME_SAMPLE = 12

NAME_SYSTEM = (
    "You are labelling a cluster of related wiki pages. Given a list of page "
    "titles that are densely cross-linked, reply with a SHORT category label "
    "(2-4 words, Title Case, no punctuation, no quotes) naming what they have "
    "in common. Reply with only the label."
)


def _map_path(tier: str, owner: str) -> str:
    return f"{tier}/{owner}/_map/map.json"


def load_map(tier: str, owner: str) -> dict | None:
    """The cached map artifact, or None if one hasn't been built yet."""
    raw = blob_store.read_text(_map_path(tier, owner))
    return json.loads(raw) if raw else None


def build_link_graph(pages: list) -> nx.Graph:
    """Undirected graph over page ids; an edge for each [[wikilink]] between
    two pages that both exist. The generated Index/contents page is excluded —
    it links to everything and would collapse the whole wiki into one hub."""
    graph = nx.Graph()
    real = [p for p in pages if p.title != INDEX_TITLE]
    ids = {p.id for p in real}
    for p in real:
        graph.add_node(p.id)
    for p in real:
        for target in p.links_out:
            if target in ids and target != p.id:
                graph.add_edge(p.id, target)
    return graph


def detect_communities(graph: nx.Graph) -> tuple[list[set], list[str]]:
    """Return (communities, orphans). Orphans are pages with no links either
    way — they can't be placed and are surfaced as such (build spec accepts
    this as informative, since an orphan is already a lint finding). Community
    detection runs only over the linked subgraph."""
    orphans = [n for n in graph.nodes if graph.degree(n) == 0]
    linked = graph.subgraph(n for n in graph.nodes if graph.degree(n) > 0)
    if linked.number_of_nodes() == 0:
        return [], orphans
    communities = nx.community.louvain_communities(linked, seed=LOUVAIN_SEED)
    return [set(c) for c in communities], orphans


def _name_community(titles: list[str]) -> str:
    """One small model call → a short label. Falls back to the first title if
    the community is a singleton or the call yields nothing usable."""
    if len(titles) == 1:
        return titles[0]
    try:
        label = (
            ab.call_model(
                "Titles:\n" + "\n".join(f"- {t}" for t in titles[:NAME_SAMPLE]),
                system=NAME_SYSTEM,
                max_tokens=30,
            )
            .strip()
            .strip("\"'")
        )
        return label or titles[0]
    except Exception:
        return titles[0]


def build_map(tier: str, owner: str) -> dict:
    """Recompute the map from the current link graph, name each community,
    and cache the result. Called on demand (map view when no cache exists, or
    the 'Regenerate map' button) — never on every page load."""
    scope = make_partition_key(tier, owner)  # type: ignore[arg-type]
    pages = [p for p in ab.list_pages(scope) if p.title != INDEX_TITLE]
    title_by_id = {p.id: p.title for p in pages}

    graph = build_link_graph(pages)
    communities, orphans = detect_communities(graph)

    named = []
    for members in communities:
        member_ids = [i for i in members if i in title_by_id]
        if not member_ids:
            continue
        titles = [title_by_id[i] for i in member_ids]
        named.append({"name": _name_community(titles), "page_ids": member_ids})
    # Largest communities first — steadier layout, and the map's overview reads
    # from most to least connected.
    named.sort(key=lambda c: len(c["page_ids"]), reverse=True)

    artifact = {
        "generated_at": now_iso(),
        "communities": named,
        "unplaced": [i for i in orphans if i in title_by_id],
        "nodes": {p.id: {"title": p.title} for p in pages},
        "edges": [list(e) for e in graph.edges],
    }
    blob_store.write_text(_map_path(tier, owner), json.dumps(artifact, indent=2))
    return artifact


def get_or_build_map(tier: str, owner: str) -> dict:
    """Cached map if present, otherwise build (and cache) it once."""
    return load_map(tier, owner) or build_map(tier, owner)
