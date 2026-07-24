"""Stage 2 (concept extraction) and Stage 3 (consolidation).

Stage 3 decides whether the output is a wiki or a filing cabinet, so most of
what is pinned here is about merging behaviour: concepts spanning documents
become one page, near-duplicates are judged rather than assumed, and the result
does not depend on the order documents happened to arrive in.
"""

from app.corpus import concepts, consolidate
from app.corpus.models import Concept, DocumentConcepts, Passage


def _doc(name: str, text: str = "Some text.") -> dict:
    return {"doc_ref": f"team/dev-team/{name}", "doc_name": name, "text": text}


def _concept(name: str, description: str = "", docs: tuple[str, ...] = ("a.pdf",)) -> Concept:
    return Concept(
        name=name,
        description=description or f"About {name}.",
        passages=[
            Passage(text=f"Evidence for {name} in {d}", doc_ref=f"team/dev-team/{d}", doc_name=d)
            for d in docs
        ],
    )


# ------------------------------------------------------------------- Stage 2


def test_extraction_captures_concepts_and_passages(azure):
    azure.model.queue_json(
        {
            "concepts": [
                {
                    "name": "Means-tested childcare subsidies",
                    "description": "Subsidy design by household income.",
                    "passages": ["Subsidies taper above the median.", "Take-up was 62%."],
                }
            ]
        }
    )

    result = concepts.extract_document(_doc("policy.pdf"))

    assert result.error == ""
    assert [c.name for c in result.concepts] == ["Means-tested childcare subsidies"]
    passages = result.concepts[0].passages
    assert [p.text for p in passages] == ["Subsidies taper above the median.", "Take-up was 62%."]
    # Passages must carry provenance, or Stage 4 can't attribute them.
    assert all(p.doc_ref == "team/dev-team/policy.pdf" for p in passages)
    assert all(p.doc_name == "policy.pdf" for p in passages)


def test_a_concept_without_passages_is_still_kept(azure):
    """Degraded, but dropping it would lose a subject entirely."""
    azure.model.queue_json({"concepts": [{"name": "Elasticity", "description": "d"}]})
    result = concepts.extract_document(_doc("a.pdf"))
    assert [c.name for c in result.concepts] == ["Elasticity"]


def test_malformed_concepts_are_skipped_not_fatal(azure):
    azure.model.queue_json(
        {"concepts": [{"description": "no name"}, "not a dict", {"name": "Real Concept"}]}
    )
    result = concepts.extract_document(_doc("a.pdf"))
    assert [c.name for c in result.concepts] == ["Real Concept"]


def test_a_failing_document_does_not_kill_the_run(azure, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(concepts.ab, "call_model_json", boom)

    result = concepts.extract_document(_doc("a.pdf"))

    assert "model unavailable" in result.error
    assert result.concepts == []


def test_long_documents_are_chunked_and_merged(azure):
    long_text = ("paragraph\n\n" * 20000)[: concepts.MAX_CHARS_PER_CALL * 2]
    azure.model.queue_json(
        {"concepts": [{"name": "Topic", "description": "first", "passages": ["p1"]}]},
        {
            "concepts": [
                {"name": "topic", "description": "a longer description", "passages": ["p2"]}
            ]
        },
    )

    result = concepts.extract_document(_doc("big.pdf", long_text))

    # Same concept named in two chunks becomes one, keeping both passages.
    assert len(result.concepts) == 1
    assert len(result.concepts[0].passages) == 2
    assert result.concepts[0].description == "a longer description"


def test_a_document_with_no_paragraph_breaks_is_still_split():
    """pypdf joins pages with single newlines, so a large government PDF can
    arrive as one unbroken run of text. Splitting only on blank lines would
    hand the whole thing to the model as one call and blow context."""
    text = "word " * 100_000  # ~500k chars, no "\n\n" anywhere

    chunks = concepts._chunks(text)

    assert len(chunks) > 1
    assert max(len(c) for c in chunks) <= concepts.MAX_CHARS_PER_CALL


def test_chunking_prefers_paragraph_boundaries():
    text = "para text\n\n" * 20_000
    chunks = concepts._chunks(text)
    assert all(len(c) <= concepts.MAX_CHARS_PER_CALL for c in chunks)


def test_chunking_loses_no_content():
    """Truncation would be invisible in the output, which is the worst kind of
    data loss for a tool whose credibility rests on provenance."""
    text = ("sentence one. sentence two. " * 5_000) + "\n\n" + ("tail. " * 5_000)

    rejoined = "".join(concepts._chunks(text))

    assert len(rejoined.replace("\n", "").replace(" ", "")) == len(
        text.replace("\n", "").replace(" ", "")
    )


def test_a_single_oversized_paragraph_is_hard_split():
    one_para = "x" * (concepts.MAX_CHARS_PER_CALL * 3)
    chunks = concepts._chunks(one_para)
    assert len(chunks) >= 3
    assert all(len(c) <= concepts.MAX_CHARS_PER_CALL for c in chunks)


def test_short_documents_are_a_single_chunk():
    assert concepts._chunks("short text") == ["short text"]


def test_extraction_respects_the_concurrency_cap(azure):
    """Firing a whole corpus at Azure OpenAI at once trips its rate limits."""
    documents = [_doc(f"doc{i}.pdf") for i in range(40)]
    azure.model.default_json = {"concepts": [{"name": "X", "description": "d"}]}

    concepts.extract_all(documents)

    assert azure.model.max_concurrent <= concepts.MAX_CONCURRENT_EXTRACTIONS
    assert azure.model.max_concurrent > 1  # ...but it is actually parallel


def test_extract_all_preserves_input_order(azure):
    """Completion order would make everything downstream non-reproducible."""
    documents = [_doc(f"doc{i}.pdf") for i in range(10)]
    azure.model.default_json = {"concepts": []}

    results = concepts.extract_all(documents)[0]

    assert [r.doc_name for r in results] == [d["doc_name"] for d in documents]


# ------------------------------------------------------------------- Stage 3


def test_a_concept_across_documents_becomes_one_page(azure):
    """The core claim of this stage: twenty documents do not make twenty pages."""
    per_doc = [
        DocumentConcepts(
            doc_ref=f"team/dev-team/doc{i}.pdf",
            doc_name=f"doc{i}.pdf",
            concepts=[_concept("Health inequality", docs=(f"doc{i}.pdf",))],
        )
        for i in range(6)
    ]
    azure.model.queue_json(
        {
            "concepts": [
                {
                    "name": "Health inequality",
                    "description": "Differences in health outcomes by income.",
                    "merged_from": ["health inequality"],
                }
            ]
        }
    )

    pages, _, _ = consolidate.consolidate(per_doc)

    assert len(pages) == 1
    assert len(pages[0].source_refs) == 6  # fed by all six documents
    assert len(pages[0].merged_from) == 6


def test_consolidation_is_order_independent(azure):
    """A running incremental merge would let whichever document arrived first
    disproportionately shape the page set. Hierarchical batching must not."""
    names = ["Elasticity", "Health inequality", "Labour supply", "Childcare subsidies"]
    forward = [
        DocumentConcepts(doc_ref=f"r/{n}", doc_name=f"{n}.pdf", concepts=[_concept(n)])
        for n in names
    ]
    backward = list(reversed(forward))
    azure.model.default_json = {"concepts": []}  # no merges proposed; carry-through path

    first = [p.title for p in consolidate.consolidate(forward)[0]]
    second = [p.title for p in consolidate.consolidate(backward)[0]]

    assert first == second


def test_concepts_the_model_forgets_are_carried_through(azure):
    """Silently dropping a concept loses its evidence, which is the one failure
    this stage cannot have."""
    per_doc = [
        DocumentConcepts(
            doc_ref="r/a",
            doc_name="a.pdf",
            concepts=[_concept("Kept"), _concept("Forgotten")],
        )
    ]
    azure.model.queue_json(
        {"concepts": [{"name": "Kept", "description": "d", "merged_from": ["kept"]}]}
    )

    pages, _, _ = consolidate.consolidate(per_doc)

    assert {p.title for p in pages} == {"Kept", "Forgotten"}


def test_identically_named_concepts_pool_their_evidence(azure):
    """Six documents naming the same subject must contribute six sets of
    passages, not one — keying by name would silently discard five."""
    batch = [_concept("Health inequality", docs=(f"doc{i}.pdf",)) for i in range(6)]

    merged = consolidate._merge_exact_names(batch)

    assert len(merged) == 1
    assert len(merged[0].passages) == 6


def test_a_cluster_merges_and_pools_passages(azure):
    """The whole point of the stage: differently-worded concepts from different
    documents become one page fed by both."""
    # Members are referenced by 1-based number, as the model is asked to return.
    azure.model.queue_json(
        {
            "groups": [
                {
                    "name": "Infant feeding",
                    "description": "d",
                    "members": [1, 2],
                    "reasoning": "Same subject.",
                }
            ]
        }
    )

    resolved, decisions, calls = consolidate._resolve_cluster(
        [
            _concept("Infant feeding", docs=("a.pdf",)),
            _concept("Feeding in infancy", docs=("b.pdf",)),
        ]
    )

    assert [c.name for c in resolved] == ["Infant feeding"]
    assert len(resolved[0].passages) == 2  # evidence from both follows the merge
    assert decisions[0].merged is True
    assert calls == 1


def test_paraphrased_member_names_do_not_break_matching(azure):
    """The bug the numbered scheme fixes: the model returns a group whose name
    doesn't echo any input verbatim. With string matching the whole cluster
    fell through unmerged; with numbers it merges as intended."""
    azure.model.queue_json(
        {
            "groups": [
                {
                    "name": "Cervical screening self-sampling",  # matches no input verbatim
                    "description": "d",
                    "members": [1, 2],
                }
            ]
        }
    )

    resolved, decisions, _ = consolidate._resolve_cluster(
        [
            _concept("HPV self-sampling in cervical screening", docs=("a.pdf",)),
            _concept("Self-sampling for cervical cancer", docs=("b.pdf",)),
        ]
    )

    assert len(resolved) == 1
    assert decisions[0].merged is True
    assert len(resolved[0].passages) == 2


def test_a_cluster_can_split_into_several_pages(azure):
    """A cluster of similar-sounding concepts may hold genuinely distinct
    subjects; forcing a single merge/no-merge verdict on the group would lose
    that, so the model returns groups rather than a boolean."""
    azure.model.queue_json(
        {
            "groups": [
                {"name": "Child poverty", "description": "d", "members": [1]},
                {"name": "Child mortality", "description": "d", "members": [2]},
            ]
        }
    )

    resolved, _, _ = consolidate._resolve_cluster(
        [_concept("Child poverty"), _concept("Child mortality")]
    )

    assert {c.name for c in resolved} == {"Child poverty", "Child mortality"}


def test_an_invented_group_matching_no_input_is_discarded(azure):
    """The first real run produced 30 pages with no source documents at all,
    because a merged name matched none of its inputs. A page with no evidence
    would be written from nothing."""
    azure.model.queue_json(
        {"groups": [{"name": "Entirely Invented Subject", "description": "d", "members": [99]}]}
    )

    resolved, decisions, _ = consolidate._resolve_cluster(
        [_concept("Real Concept A"), _concept("Real Concept B")]
    )

    assert "Entirely Invented Subject" not in {c.name for c in resolved}
    assert {c.name for c in resolved} == {"Real Concept A", "Real Concept B"}
    assert decisions == []


def test_pages_without_evidence_never_reach_the_page_set(azure):
    from app.corpus.models import Concept

    pages = consolidate.to_page_set(
        [Concept(name="Unsourced", description="d", passages=[]), _concept("Sourced")]
    )

    assert [p.title for p in pages] == ["Sourced"]


def test_clustering_caps_chaining(azure):
    """Similarity isn't transitive; unbounded connected components would merge
    A~B~C into one blob even when A and C are unrelated."""
    import numpy as np

    # A chain where each vector is close to its neighbour only.
    vectors = [list(np.eye(40, dtype=float)[i] + np.eye(40, dtype=float)[i + 1]) for i in range(39)]
    clusters = consolidate._cluster_by_similarity(vectors)

    assert max(len(c) for c in clusters) <= consolidate.MAX_CLUSTER_MEMBERS


def test_similarity_clustering_groups_identical_vectors():
    identical = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
    clusters = consolidate._cluster_by_similarity(identical)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 2]


def test_duplicate_titles_are_made_unique(azure):
    """Two pages with one title would collide on slug and silently overwrite."""
    pages = consolidate.to_page_set([_concept("Elasticity"), _concept("Elasticity")])
    assert [p.title for p in pages] == ["Elasticity", "Elasticity (2)"]


def test_repeated_passages_are_not_duplicated(azure):
    shared = Passage(text="same quote", doc_ref="r/a", doc_name="a.pdf")
    concept = Concept(name="Topic", description="d", passages=[shared, shared])
    pages = consolidate.to_page_set([concept])
    assert len(pages[0].passages) == 1


def test_page_set_records_its_contributing_sources(azure):
    pages = consolidate.to_page_set([_concept("Topic", docs=("a.pdf", "b.pdf"))])
    assert pages[0].source_refs == ["team/dev-team/a.pdf", "team/dev-team/b.pdf"]
