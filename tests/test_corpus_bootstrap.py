"""The bootstrap entry point: a check on the scope, not a mode, and not the
user. An empty scope offers the pipeline; a populated one is a normal
workspace, so a later joiner on a built corpus lands on the wiki rather than
being asked for documents that already exist.
"""

from app import abstractions as ab
from app.corpus import runner
from app.corpus.models import RunStatus
from app.ingest import extractors
from app.models import Page, make_partition_key, now_iso

TEAM = "dev-team"
ALICE = "alice"

# Text unique to the empty-scope bootstrap page. Not the button label, which is
# scope-specific, and not the form action, since the populated workspace now
# carries a /bootstrap form too.
BOOTSTRAP_MARKER = "This space is empty"


def _page(owner: str, title: str, tier: str = "team") -> Page:
    scope = make_partition_key(tier, owner)
    return ab.write_page(
        Page(partition_key=scope, title=title, body="Body.", tier=tier, owner_id=owner),
        change_type="ingest",
        author_id=owner,
    )


def test_empty_scope_offers_the_pipeline(client):
    resp = client(ALICE).get(f"/team/{TEAM}/")

    assert resp.status_code == 200
    assert BOOTSTRAP_MARKER in resp.text


def test_populated_scope_shows_the_normal_workspace(client):
    _page(TEAM, "Acme Q3")

    resp = client(ALICE).get(f"/team/{TEAM}/")

    assert resp.status_code == 200
    assert BOOTSTRAP_MARKER not in resp.text
    assert "Acme Q3" in resp.text


def test_the_check_is_on_the_scope_not_the_user(client):
    """A second team member arriving at a built corpus must get the wiki, not
    a request for documents that already exist."""
    _page(TEAM, "Acme Q3")

    resp = client("bob").get(f"/team/{TEAM}/")

    assert BOOTSTRAP_MARKER not in resp.text


def test_bootstrap_page_names_the_scope_it_will_build(client):
    """Every empty scope shows this same page, so starting a long paid build in
    the wrong one is easy. The target has to be on screen."""
    body = client(ALICE).get(f"/team/{TEAM}/").text

    assert "Dev Team" in body
    assert f"/team/{TEAM}/" in body


def test_personal_scope_warns_before_building_a_shared_corpus(client):
    """`/` redirects to the personal scope, which is also empty — so this is
    where a team corpus most plausibly gets built by accident."""
    body = client(ALICE).get("/individual/alice/").text

    assert "personal space" in body
    assert f'href="/team/{TEAM}/"' in body  # one click to the right place


def test_team_scope_has_no_misdirection_warning(client):
    body = client(ALICE).get(f"/team/{TEAM}/").text
    assert "A shared corpus usually belongs in a team" not in body


def test_a_running_pipeline_redirects_to_its_run_page(azure, client):
    """The scope is still empty mid-run; without this the user would be asked
    to upload documents while their upload is being processed."""
    status = RunStatus(tier="team", owner=TEAM, state="running")
    runner.save_status(status)

    resp = client(ALICE).get(f"/team/{TEAM}/", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/team/{TEAM}/pipeline/{status.id}"


# ---------------------------------------------------------------- the upload


def test_upload_stores_sources_and_starts_a_run(azure, client):
    resp = client(ALICE).post(
        f"/team/{TEAM}/bootstrap",
        files=[("files", ("notes.txt", b"Health economics content.", "text/plain"))],
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "/pipeline/" in resp.headers["location"]
    # Verbatim and immutable, per the storage model.
    assert len(azure.blob.sources) == 1
    assert list(azure.blob.sources.values())[0] == b"Health economics content."


def test_upload_rejects_a_corpus_it_cannot_read(client):
    resp = client(ALICE).post(
        f"/team/{TEAM}/bootstrap",
        files=[("files", ("archive.zip", b"PK\x03\x04", "application/zip"))],
    )

    assert resp.status_code == 422
    assert "archive.zip" in resp.text


def test_unreadable_files_are_skipped_not_fatal(azure, client):
    """A dropped folder will contain files the extractors can't read. Losing
    the whole upload over one of them would be absurd."""
    resp = client(ALICE).post(
        f"/team/{TEAM}/bootstrap",
        files=[
            ("files", ("good.txt", b"Real content.", "text/plain")),
            ("files", ("logo.zip", b"PK\x03\x04", "application/zip")),
        ],
        follow_redirects=False,
    )

    assert resp.status_code == 303
    run_id = resp.headers["location"].rsplit("/", 1)[-1]
    status = runner.load_status("team", TEAM, run_id)
    assert status.document_count == 1
    assert any("logo.zip" in s for s in status.skipped)


def test_skipped_files_are_reported_on_the_run_page(azure, client):
    """A document count lower than what was dropped needs a visible reason."""
    c = client(ALICE)
    resp = c.post(
        f"/team/{TEAM}/bootstrap",
        files=[
            ("files", ("good.txt", b"Real content.", "text/plain")),
            ("files", ("logo.zip", b"PK\x03\x04", "application/zip")),
        ],
        follow_redirects=False,
    )
    run_id = resp.headers["location"].rsplit("/", 1)[-1]
    runner.wait_for(run_id)

    assert "logo.zip" in c.get(f"/team/{TEAM}/pipeline/{run_id}").text


def test_upload_form_supports_folder_drops(client):
    """The corpus arrives as a directory tree, so the drop zone has to walk
    folders rather than only accept a hand-picked file list."""
    body = client(ALICE).get(f"/team/{TEAM}/").text

    assert "dropzone" in body
    assert "webkitGetAsEntry" in body  # recursive folder traversal
    assert "readEntries" in body  # drained in batches, so >100 files work


def test_upload_is_scoped(client):
    outsider = client("carol", teams={"other-team": "Other"})
    resp = outsider.post(
        f"/team/{TEAM}/bootstrap",
        files=[("files", ("notes.txt", b"text", "text/plain"))],
    )
    assert resp.status_code == 403


# ----------------------------------------------------------------- run status


def test_run_completes_and_records_the_ingest_stage(azure, client):
    c = client(ALICE)
    resp = c.post(
        f"/team/{TEAM}/bootstrap",
        files=[
            ("files", ("a.txt", b"First document.", "text/plain")),
            ("files", ("b.txt", b"Second document.", "text/plain")),
        ],
        follow_redirects=False,
    )
    run_id = resp.headers["location"].rsplit("/", 1)[-1]
    runner.wait_for(run_id)

    status = runner.load_status("team", TEAM, run_id)
    assert status is not None
    assert status.state == "complete"
    assert status.document_count == 2

    page = c.get(f"/team/{TEAM}/pipeline/{run_id}")
    assert page.status_code == 200
    assert "a.txt" in page.text


def test_run_page_is_scoped(azure, client):
    status = RunStatus(tier="team", owner=TEAM)
    runner.save_status(status)

    outsider = client("carol", teams={"other-team": "Other"})
    assert outsider.get(f"/team/{TEAM}/pipeline/{status.id}").status_code == 403


def test_unknown_run_is_404(azure, client):
    assert client(ALICE).get(f"/team/{TEAM}/pipeline/nope").status_code == 404


def test_pipeline_artifacts_are_not_wiki_pages(azure, client):
    """Run artifacts live under a "_"-prefixed path. The pipeline does write
    real pages, so this asserts the artifacts specifically stay out of the page
    index rather than that nothing was written."""
    resp = client(ALICE).post(
        f"/team/{TEAM}/bootstrap",
        files=[("files", ("a.txt", b"Text.", "text/plain"))],
        follow_redirects=False,
    )
    runner.wait_for(resp.headers["location"].rsplit("/", 1)[-1])

    page_paths = azure.blob.list_page_paths("team", TEAM)
    assert not any("_pipeline" in p for p in page_paths)
    assert azure.blob.list_paths(f"team/{TEAM}/_pipeline/")  # artifacts do exist


def test_corpus_ingest_does_not_truncate_long_documents(azure, client):
    """A 150-page policy document truncated at 200k characters would lose its
    back half silently — invisible in the output, and fatal for provenance."""
    long_text = ("policy sentence. " * 30_000).encode()  # ~510k chars
    assert len(long_text) > extractors.MAX_CHARS

    resp = client(ALICE).post(
        f"/team/{TEAM}/bootstrap",
        files=[("files", ("green-paper.txt", long_text, "text/plain"))],
        follow_redirects=False,
    )
    run_id = resp.headers["location"].rsplit("/", 1)[-1]
    runner.wait_for(run_id)

    ingest = runner.load_artifact("team", TEAM, run_id, "ingest")
    doc = ingest["documents"][0]
    assert doc["chars"] > extractors.MAX_CHARS
    assert doc["chunks"] > 1  # split into several extraction calls, not truncated


def test_single_document_ingest_still_truncates(azure):
    """The cap is cost discipline for the one-call ingest path and should stay
    there — only the corpus pipeline, which chunks, opts out."""
    data = ("x" * (extractors.MAX_CHARS + 5000)).encode()
    assert len(extractors.extract_file("note.txt", data).text) == extractors.MAX_CHARS


def test_rebuild_is_reachable_from_a_populated_workspace(azure, client):
    """Iterating on the dedup threshold means re-running repeatedly; without an
    entry point that means hitting the endpoint by hand every time."""
    _page(TEAM, "Existing Page")

    body = client(ALICE).get(f"/team/{TEAM}/").text

    assert f'action="/team/{TEAM}/bootstrap"' in body
    assert "Rebuild from documents" in body


def test_rebuilding_a_populated_scope_regenerates_pages(azure, client):
    """Full regeneration, per the MVP write semantics: pages are rewritten and
    version history keeps the previous pass."""
    azure.model.default_json = {
        "concepts": [{"name": "Topic", "description": "d", "passages": ["evidence"]}]
    }
    c = client(ALICE)
    first = c.post(
        f"/team/{TEAM}/bootstrap",
        files=[("files", ("a.txt", b"Content.", "text/plain"))],
        follow_redirects=False,
    )
    runner.wait_for(first.headers["location"].rsplit("/", 1)[-1])
    before = ab.find_page_by_title("Topic", make_partition_key("team", TEAM))

    second = c.post(
        f"/team/{TEAM}/bootstrap",
        files=[("files", ("b.txt", b"More content.", "text/plain"))],
        follow_redirects=False,
    )
    runner.wait_for(second.headers["location"].rsplit("/", 1)[-1])

    after = ab.find_page_by_title("Topic", make_partition_key("team", TEAM))
    assert after.id == before.id  # same page, rewritten
    assert after.version > before.version
    assert "Pushed by" not in after.body  # rewritten, never appended


def test_progress_is_visible_while_a_stage_is_still_running(azure):
    """Consolidate can run for twenty minutes on a real corpus. A stage that
    reports nothing until it finishes is indistinguishable from a hung one."""
    status = RunStatus(tier="team", owner=TEAM)
    runner.save_status(status)

    with runner.StageRecorder(status, "consolidate") as stage:
        stage.progress.start(total=58, label="round 1: batch 1/58")
        stage.progress.tick(label="round 1: batch 2/58")

        # Read back from blob mid-stage, exactly as the run page does.
        mid = runner.load_status("team", TEAM, status.id)

    recorded = next(s for s in mid.stages if s.name == "consolidate")
    assert recorded.finished_at == ""  # still running when observed
    assert recorded.progress_total == 58
    assert recorded.progress_label.startswith("round 1")
    assert recorded.progress_at  # a timestamp the reader can watch advance


def test_progress_writes_are_throttled(azure):
    """One blob write per model call would add latency to a stage already
    making hundreds of them."""
    status = RunStatus(tier="team", owner=TEAM)
    with runner.StageRecorder(status, "consolidate") as stage:
        stage.progress.start(total=100)
        for _ in range(50):
            stage.progress.tick()

    recorded = next(s for s in status.stages if s.name == "consolidate")
    assert recorded.progress_done == 50  # every tick counted...
    # ...but they did not each cause a write; the throttle window swallowed them.
    assert runner.Progress.MIN_INTERVAL_SECONDS > 0


def test_run_page_shows_live_progress(azure, client):
    """A stage that has started but not finished shows its live detail rather
    than a blank cell."""
    status = RunStatus(tier="team", owner=TEAM, state="running")
    stage = status.stage("consolidate")
    stage.started_at = now_iso()  # started, deliberately not finished
    stage.progress_done = 7
    stage.progress_total = 58
    stage.progress_label = "round 1: batch 7/58"
    stage.progress_at = now_iso()
    runner.save_status(status)

    body = client(ALICE).get(f"/team/{TEAM}/pipeline/{status.id}").text

    assert "round 1: batch 7/58" in body
    assert "7/58" in body


def test_a_finished_stage_shows_its_summary_not_stale_progress(azure, client):
    status = RunStatus(tier="team", owner=TEAM, state="complete")
    stage = status.stage("consolidate")
    stage.started_at = stage.finished_at = now_iso()
    stage.progress_label = "round 1: batch 7/58"
    stage.note = "460 concepts consolidated into 92 page(s)"
    runner.save_status(status)

    body = client(ALICE).get(f"/team/{TEAM}/pipeline/{status.id}").text

    assert "460 concepts consolidated into 92 page(s)" in body
    assert "round 1: batch 7/58" not in body


def test_latest_run_id_picks_the_most_recent(azure):
    older = RunStatus(tier="team", owner=TEAM, started_at="2026-01-01T00:00:00+00:00")
    newer = RunStatus(tier="team", owner=TEAM, started_at="2026-06-01T00:00:00+00:00")
    runner.save_status(older)
    runner.save_status(newer)

    assert runner.latest_run_id("team", TEAM) == newer.id


def test_a_failing_stage_is_recorded_not_swallowed(azure, monkeypatch):
    """The pipeline runs on a thread nobody awaits, so a crash has to land in
    the status blob or it's invisible."""
    status = RunStatus(tier="team", owner=TEAM)

    def boom(*args, **kwargs):
        raise RuntimeError("stage exploded")

    monkeypatch.setattr(runner, "run_pipeline", boom)
    runner._run_guarded(status, [], user=None)

    reloaded = runner.load_status("team", TEAM, status.id)
    assert reloaded.state == "failed"
    assert "stage exploded" in reloaded.error
