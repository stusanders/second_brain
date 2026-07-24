"""Behaviour of the three core operations where getting it wrong is expensive:
query fabricating an answer, automatic ingest running unattended past its
safety threshold, and the schema doc's one machine-read parameter.
"""

from app import lint, query, schema
from app.models import Page, User, make_partition_key

ALICE = "alice"
SCOPE = make_partition_key("individual", ALICE)
USER = User(id=ALICE, name="Alice")


def _page(title: str, body: str) -> Page:
    from app import abstractions as ab

    return ab.write_page(
        Page(partition_key=SCOPE, title=title, body=body, tier="individual", owner_id=ALICE),
        change_type="ingest",
        author_id=ALICE,
    )


# ---------------------------------------------------------------- query


def test_empty_wiki_returns_the_no_coverage_answer(azure):
    answer = query.ask("What is our revenue?", "individual", ALICE)

    assert answer.answer == query.NO_COVERAGE_MESSAGE
    assert answer.sources == []


def test_no_coverage_makes_no_model_call(azure):
    """The whole point of the rule: with nothing retrieved there is no
    grounded answer to give, so the model is never asked for one and can't
    fabricate from its priors."""
    query.ask("What is our revenue?", "individual", ALICE)

    chat_calls = [c for c in azure.model.calls if c["kind"].startswith("call_model")]
    assert chat_calls == []


def test_a_covered_question_does_reach_the_model(azure):
    _page("Revenue", "Quarterly revenue rose to 4.2m.")
    azure.model.queue("Revenue rose to 4.2m [[Revenue]].")

    answer = query.ask("quarterly revenue", "individual", ALICE)

    assert answer.answer != query.NO_COVERAGE_MESSAGE
    assert any(c["kind"] == "call_model" for c in azure.model.calls)
    assert answer.sources


# ----------------------------------------------------------------- lint


def _queue_findings(n: int, origin_mode: str = "automatic") -> None:
    lint._append_findings(
        "individual",
        ALICE,
        [
            lint.LintFinding(summary=f"finding {i}", origin_mode=origin_mode)  # type: ignore[arg-type]
            for i in range(n)
        ],
    )


def test_automatic_ingest_allowed_below_the_threshold(azure):
    _queue_findings(3)
    assert lint.is_automatic_ingest_allowed("individual", ALICE) is True


def test_automatic_ingest_blocked_at_the_threshold(azure):
    """Build spec: once the unreviewed queue overflows, automatic mode is
    disabled until it's worked down — capture is never blocked, only the
    unattended write path."""
    _queue_findings(schema.DEFAULT_AUTOMATIC_LINT_QUEUE_THRESHOLD)
    assert lint.is_automatic_ingest_allowed("individual", ALICE) is False


def test_only_automatic_origin_findings_count_toward_the_threshold(azure):
    """The isolating filter: manually-reviewed content shouldn't be able to
    switch off automatic mode."""
    _queue_findings(schema.DEFAULT_AUTOMATIC_LINT_QUEUE_THRESHOLD, origin_mode="manual")
    assert lint.is_automatic_ingest_allowed("individual", ALICE) is True


def test_automatic_lint_mode_exempts_the_threshold(azure):
    """In automatic lint mode mechanical findings clear themselves, so nothing
    accumulates and the downgrade would be spurious."""
    _queue_findings(schema.DEFAULT_AUTOMATIC_LINT_QUEUE_THRESHOLD)
    lint.set_lint_mode("individual", ALICE, "automatic")
    assert lint.is_automatic_ingest_allowed("individual", ALICE) is True


def test_ingest_downgrades_to_manual_when_blocked(azure, client):
    _queue_findings(schema.DEFAULT_AUTOMATIC_LINT_QUEUE_THRESHOLD)

    resp = client(ALICE).post(
        "/ingest",
        data={"mode": "automatic", "pasted": "Some text to ingest."},
        follow_redirects=False,
    )

    # Downgraded into the manual-session flow rather than silently writing.
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/manual/")
    assert "downgraded=1" in resp.headers["location"]


# --------------------------------------------------------------- schema


def test_threshold_is_read_from_frontmatter(azure):
    schema.write_schema("individual", ALICE, "---\nautomatic_lint_queue_threshold: 7\n---\n# Mine")
    assert schema.automatic_lint_queue_threshold("individual", ALICE) == 7


def test_malformed_threshold_falls_back_to_the_default(azure):
    schema.write_schema("individual", ALICE, "---\nautomatic_lint_queue_threshold: lots\n---\n")
    assert (
        schema.automatic_lint_queue_threshold("individual", ALICE)
        == schema.DEFAULT_AUTOMATIC_LINT_QUEUE_THRESHOLD
    )


def test_missing_schema_doc_uses_the_shipped_default(azure):
    assert schema.read_schema("individual", ALICE) == schema.DEFAULT_SCHEMA
    assert (
        schema.automatic_lint_queue_threshold("individual", ALICE)
        == schema.DEFAULT_AUTOMATIC_LINT_QUEUE_THRESHOLD
    )


def test_saving_a_schema_snapshots_the_previous_version(azure):
    schema.write_schema("individual", ALICE, "# First")
    schema.write_schema("individual", ALICE, "# Second")

    assert schema.read_schema("individual", ALICE) == "# Second"
    versions = azure.blob.list_paths(f"individual/{ALICE}/_schema_versions/")
    assert len(versions) == 1


def test_schema_doc_is_not_a_wiki_page(azure):
    """It's governance, not content — "_"-prefixed and excluded from the index."""
    schema.write_schema("individual", ALICE, "# Mine")
    assert azure.blob.list_page_paths("individual", ALICE) == []


def test_system_prefix_puts_the_stable_schema_block_first(azure):
    """Prompt-cache discipline: the owner-stable block must lead so ingest,
    query and lint share one cacheable prefix."""
    prefix = schema.system_prefix("OPERATION INSTRUCTION", "individual", ALICE)
    assert prefix.startswith("<schema_doc>")
    assert prefix.index("<schema_doc>") < prefix.index("OPERATION INSTRUCTION")


# --------------------------------------------------- background lint (no hang)


def test_lint_run_returns_immediately(azure, client):
    """The bug this fixes: run_lint was called inline, so the request hung for
    the whole pass. It must fire a background thread and redirect at once."""
    from app import abstractions as ab
    from app.models import Page

    ab.write_page(
        Page(partition_key=SCOPE, title="A", body="Body.", tier="individual", owner_id=ALICE),
        change_type="ingest",
        author_id=ALICE,
    )
    azure.model.default_json = {"contradictions": []}

    resp = client(ALICE).post(f"/individual/{ALICE}/lint/run", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/individual/{ALICE}/lint"
    lint.wait_for_lint("individual", ALICE)


def test_lint_status_clears_when_a_pass_finishes(azure):
    azure.model.default_json = {"contradictions": []}
    lint.start_lint("individual", ALICE, USER, max_judgment_pages=5)
    lint.wait_for_lint("individual", ALICE)

    assert lint.lint_status("individual", ALICE) is None  # no longer running


def test_a_second_run_does_not_start_while_one_is_running(azure):
    lint._set_lint_status("individual", ALICE, {"state": "running", "started_at": "now"})

    lint.start_lint("individual", ALICE, USER)  # should be a no-op

    # Still the original running marker; no second thread launched.
    assert lint.lint_status("individual", ALICE)["state"] == "running"


def test_lint_running_marker_is_not_a_wiki_page(azure):
    lint._set_lint_status("individual", ALICE, {"state": "running"})
    assert azure.blob.list_page_paths("individual", ALICE) == []
