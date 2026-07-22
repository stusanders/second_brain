"""Link-graph community detection is pure (no Azure/model calls), so it's the
part of the knowledge map worth unit-testing directly."""

from types import SimpleNamespace

from app import knowledge_map as km


def _page(pid, title, links_out):
    return SimpleNamespace(id=pid, title=title, links_out=links_out)


def test_two_clusters_and_an_orphan():
    # Two densely-linked triangles bridged by a single edge, plus a page nothing
    # links to. Louvain should recover the two triangles; the orphan is unplaceable.
    pages = [
        _page("a", "A", ["b", "c"]),
        _page("b", "B", ["a", "c"]),
        _page("c", "C", ["a", "b", "d"]),  # the bridge
        _page("d", "D", ["e", "f"]),
        _page("e", "E", ["d", "f"]),
        _page("f", "F", ["d", "e"]),
        _page("orphan", "Orphan", []),
    ]
    graph = km.build_link_graph(pages)
    communities, orphans = km.detect_communities(graph)

    assert orphans == ["orphan"]
    members = sorted(sorted(c) for c in communities)
    assert members == [["a", "b", "c"], ["d", "e", "f"]]


def test_index_page_is_excluded_from_graph():
    # The generated Index links to everything; including it would collapse the
    # whole wiki into one hub, so it must be dropped from the graph.
    pages = [
        _page("idx", km.INDEX_TITLE, ["a", "b"]),
        _page("a", "A", ["b"]),
        _page("b", "B", ["a"]),
    ]
    graph = km.build_link_graph(pages)
    assert "idx" not in graph.nodes
    assert set(graph.nodes) == {"a", "b"}


def test_links_to_missing_pages_are_ignored():
    # A [[wikilink]] to a page that doesn't exist yet must not create a phantom node.
    pages = [_page("a", "A", ["ghost"]), _page("b", "B", [])]
    graph = km.build_link_graph(pages)
    assert set(graph.nodes) == {"a", "b"}
    assert graph.number_of_edges() == 0
