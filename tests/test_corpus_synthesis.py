"""Stage 4 (page synthesis) and Stage 6 (frontier), plus the end-to-end run.

Two invariants dominate here. Pages are written wholesale rather than appended,
because the append-only rule guards against multi-contributor overwrites that a
one-shot agent synthesis does not have. And the frontier is a prediction with no
source behind it, so it must never reach the wiki, search or an export.
"""

from app import abstractions as ab
from app import knowledge_map, lint, wiki
from app.corpus import frontier, runner, synthesis
from app.corpus.models import PageSpec, Passage
from app.models import make_partition_key

TEAM = "dev-team"
SCOPE = make_partition_key("team", TEAM)
ALICE = "alice"
BOOTSTRAP_MARKER = "This space is empty"  # unique to the empty-scope page


def _spec(title: str, docs: tuple[str, ...] = ("a.pdf",), passages_each: int = 1) -> PageSpec:
    passages = [
        Passage(text=f"Evidence {i} from {d}", doc_ref=f"team/{TEAM}/{d}", doc_name=d)
        for d in docs
        for i in range(passages_each)
    ]
    return PageSpec(
        title=title,
        description=f"About {title}.",
        source_refs=sorted({p.doc_ref for p in passages}),
        passages=passages,
    )


# ------------------------------------------------------------- the link guard


def test_links_outside_the_page_set_are_stripped():
    """Stage 3 fixes the page set so this is decidable; a link to a page that
    will never exist is a dead end and a phantom node on the map."""
    targets = {"health inequality": "Health inequality"}
    body = "See [[Health inequality]] and [[Invented Page]] for more."

    assert synthesis.strip_unknown_links(body, targets) == (
        "See [[Health inequality]] and Invented Page for more."
    )


def test_link_casing_is_normalised_so_it_resolves():
    targets = {"health inequality": "Health inequality"}
    normalised = synthesis.strip_unknown_links("[[health INEQUALITY]]", targets)
    assert normalised == "[[Health inequality]]"


def test_synthesis_strips_bad_links_even_when_the_model_emits_them(azure):
    azure.model.queue("Body referencing [[Nonexistent]] and [[Real Page]].")
    page_set = [_spec("Real Page"), _spec("Other")]

    page, _ = synthesis.write_page(page_set[0], page_set, "team", TEAM)

    assert "[[Nonexistent]]" not in page.body
    assert "[[Real Page]]" in page.body


# ---------------------------------------------------------------- write shape


def test_pages_are_written_wholesale_not_appended(azure):
    """MVP write semantics: the agent writes each page once from documents, so
    there is no contributor's work to protect and no append block."""
    page_set = [_spec("Health inequality")]
    azure.model.queue("First synthesis.")
    synthesis.write_page(page_set[0], page_set, "team", TEAM)

    azure.model.queue("Second synthesis, fully rewritten.")
    page, _ = synthesis.write_page(page_set[0], page_set, "team", TEAM)

    assert page.body == "Second synthesis, fully rewritten."
    assert "Pushed by" not in page.body
    assert "First synthesis." not in page.body


def test_regeneration_bumps_the_version_and_keeps_history(azure):
    page_set = [_spec("Topic")]
    azure.model.queue("v1 body")
    synthesis.write_page(page_set[0], page_set, "team", TEAM)
    azure.model.queue("v2 body")
    page, _ = synthesis.write_page(page_set[0], page_set, "team", TEAM)

    assert page.version == 2
    assert "v1 body" in azure.blob.read_text(f"team/{TEAM}/topic/v1.md")


def test_change_type_is_never_merge_append(azure):
    page_set = [_spec("Topic")]
    page, _ = synthesis.write_page(page_set[0], page_set, "team", TEAM)
    assert ab.list_versions(page.id, SCOPE)[0].change_type == "ingest"


def test_provenance_reaches_frontmatter(azure):
    """In a one-shot synthesis, source refs are the only way a reader can check
    the output is honest."""
    spec = _spec("Topic", docs=("a.pdf", "b.pdf"))
    page, _ = synthesis.write_page(spec, [spec], "team", TEAM)

    raw = azure.blob.read_text(page.blob_path)
    assert f"team/{TEAM}/a.pdf" in raw
    assert f"team/{TEAM}/b.pdf" in raw


def test_synthesis_writes_from_passages_not_whole_documents(azure):
    """The deep-research property: extracts, never full source text. Whole
    documents would blow context at corpus scale."""
    spec = _spec("Topic")
    spec.passages = [Passage(text="THE EXTRACT", doc_ref="r/a", doc_name="a.pdf")]
    synthesis.write_page(spec, [spec], "team", TEAM)

    call = next(c for c in azure.model.calls if c["kind"] == "call_model")
    assert "THE EXTRACT" in call["context"]


# ------------------------------------------------------------- overflow path


def test_oversized_pages_degrade_rather_than_fail(azure, monkeypatch):
    monkeypatch.setattr(synthesis, "MAX_PASSAGE_CHARS", 50)
    spec = _spec("Topic", docs=("a.pdf", "b.pdf"), passages_each=5)

    page, calls = synthesis.write_page(spec, [spec], "team", TEAM)

    assert spec.degraded is True
    assert page.body  # written anyway
    assert calls > 1  # per-source summaries plus the synthesis call


def test_normal_pages_are_not_marked_degraded(azure):
    spec = _spec("Topic")
    synthesis.write_page(spec, [spec], "team", TEAM)
    assert spec.degraded is False


# ------------------------------------------------- source contradiction capture


def test_single_source_pages_skip_the_contradiction_check(azure):
    """A lone source cannot disagree with itself; checking would be wasted spend."""
    spec = _spec("Topic", docs=("a.pdf",))
    found, calls = synthesis._find_contradictions(spec, "block")
    assert found == []
    assert calls == 0


def test_disagreeing_sources_are_flagged(azure):
    spec = _spec("Topic", docs=("a.pdf", "b.pdf"))
    azure.model.queue_json(
        {"contradictions": [{"summary": "Different take-up figures", "detail": "62% vs 71%."}]}
    )

    found, calls = synthesis._find_contradictions(spec, "block")

    assert found[0]["summary"] == "Different take-up figures"
    assert calls == 1


def test_contradictions_land_in_the_lint_queue(azure):
    synthesis.record_contradictions(
        "team",
        TEAM,
        [{"page_id": "p1", "page_title": "Topic", "summary": "Sources disagree", "detail": "d"}],
    )

    findings = lint.list_findings("team", TEAM)
    assert [f.summary for f in findings] == ["Sources disagree"]
    assert findings[0].check_type == "contradiction"


# ------------------------------------------------------------------- frontier


def test_frontier_drops_entries_that_already_exist(azure):
    """A 'gap' that is already a page is a retrieval failure, not a prediction."""
    azure.model.queue_json(
        {
            "frontier": [
                {"title": "Existing Page", "why": "w", "near": "n"},
                {"title": "Genuinely Missing", "why": "w", "near": "n"},
            ]
        }
    )

    entries, _ = frontier.predict(["Existing Page"], ["Area"])

    assert [e["title"] for e in entries] == ["Genuinely Missing"]


def test_frontier_on_an_empty_wiki_makes_no_call(azure):
    entries, calls = frontier.predict([], [])
    assert entries == []
    assert calls == 0


def test_frontier_never_becomes_a_page(azure):
    """The hard rule: predictions have no source behind them, so they must not
    enter the wiki, search, or an export."""
    ab.write_page(
        wiki.Page(partition_key=SCOPE, title="Real Page", body="Body.", tier="team", owner_id=TEAM),
        change_type="ingest",
        author_id="system",
    )
    knowledge_map.build_map("team", TEAM)
    knowledge_map.save_frontier("team", TEAM, [{"title": "Predicted Gap", "why": "w", "near": "n"}])

    titles = [p.title for p in ab.list_pages(SCOPE)]
    assert "Predicted Gap" not in titles
    assert "Predicted Gap" not in [h["title"] for h in ab.search("Predicted Gap", SCOPE)]
    assert b"Predicted Gap" not in wiki.export_wiki("team", TEAM)


def test_frontier_is_stored_on_the_map_artifact(azure):
    ab.write_page(
        wiki.Page(partition_key=SCOPE, title="P", body="B", tier="team", owner_id=TEAM),
        change_type="ingest",
        author_id="system",
    )
    knowledge_map.build_map("team", TEAM)
    knowledge_map.save_frontier("team", TEAM, [{"title": "Gap", "why": "w", "near": "n"}])

    assert knowledge_map.load_map("team", TEAM)["frontier"][0]["title"] == "Gap"


def test_regenerating_the_map_clears_a_stale_frontier(azure):
    """A frontier computed against a different page set would mislead."""
    ab.write_page(
        wiki.Page(partition_key=SCOPE, title="P", body="B", tier="team", owner_id=TEAM),
        change_type="ingest",
        author_id="system",
    )
    knowledge_map.build_map("team", TEAM)
    knowledge_map.save_frontier("team", TEAM, [{"title": "Gap", "why": "w", "near": "n"}])

    knowledge_map.build_map("team", TEAM)

    assert "frontier" not in knowledge_map.load_map("team", TEAM)


# ----------------------------------------------------------------- end to end


def test_a_full_run_produces_every_stage_output(azure, client):
    """The run isn't complete until the map, frontier, contents and lint have
    all happened — a run that stops after writing pages has skipped half of
    what the MVP is meant to show."""
    azure.model.default_json = {
        "concepts": [{"name": "Health inequality", "description": "d", "passages": ["evidence"]}]
    }
    resp = client(ALICE).post(
        f"/team/{TEAM}/bootstrap",
        files=[
            ("files", ("a.txt", b"Health inequality content.", "text/plain")),
            ("files", ("b.txt", b"More on health inequality.", "text/plain")),
        ],
        follow_redirects=False,
    )
    run_id = resp.headers["location"].rsplit("/", 1)[-1]
    runner.wait_for(run_id)

    status = runner.load_status("team", TEAM, run_id)
    assert status.state == "complete", status.error
    assert status.page_count >= 1
    assert [s.name for s in status.stages] == [
        "ingest",
        "concepts",
        "consolidate",
        "synthesis",
        "map",
        "frontier",
    ]

    # All four Stage 5/6 outputs exist.
    assert knowledge_map.load_map("team", TEAM) is not None
    assert ab.find_page_by_title(wiki.INDEX_TITLE, SCOPE) is not None
    assert any(p.title != wiki.INDEX_TITLE for p in ab.list_pages(SCOPE))


def test_a_full_run_leaves_a_usable_wiki(azure, client):
    azure.model.default_json = {
        "concepts": [{"name": "Topic", "description": "d", "passages": ["evidence"]}]
    }
    resp = client(ALICE).post(
        f"/team/{TEAM}/bootstrap",
        files=[("files", ("a.txt", b"Content.", "text/plain"))],
        follow_redirects=False,
    )
    runner.wait_for(resp.headers["location"].rsplit("/", 1)[-1])

    # The scope is populated, so the bootstrap check must now stand down.
    assert BOOTSTRAP_MARKER not in client(ALICE).get(f"/team/{TEAM}/").text
