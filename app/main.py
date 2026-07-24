import re
import uuid

import markdown as md
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import abstractions as ab
from app import auth, blob_store, derived_views, knowledge_map, lint, push, query, schema, wiki
from app.corpus import concepts as corpus_concepts
from app.corpus import models as corpus_models
from app.corpus import runner as corpus_runner
from app.ingest import extractors, pipeline
from app.models import WIKILINK_RE, User, make_partition_key

app = FastAPI(title="LLM Wiki POC")
templates = Jinja2Templates(directory="templates")


def render_markdown(body: str, tier: str, owner: str) -> str:
    def link(match: re.Match) -> str:
        title = match.group(1).strip()
        return f'<a href="/{tier}/{owner}/page-by-title/{title}">{title}</a>'

    return md.markdown(WIKILINK_RE.sub(link, body), extensions=["tables", "fenced_code"])


def _reject_team_edit(tier: str) -> None:
    """The team-tier append-only substrate has no exceptions (build spec).
    A direct edit route must not become the hole in it — team pages change
    only by appending, via push."""
    if tier == "team":
        raise HTTPException(403, "Team pages are append-only logs and cannot be edited directly.")


def _scope_or_403(tier: str, owner: str, user: User) -> str:
    """Central access control: individual = self only; team = member only."""
    if tier == "individual":
        if owner != user.id:
            raise HTTPException(403, "Individual wikis are private.")
    elif tier == "team":
        auth.require_team(user, owner)
    else:
        raise HTTPException(404)
    return make_partition_key(tier, owner)  # type: ignore[arg-type]


# ------------------------------------------------------------------- auth


@app.get("/auth/login")
def login():
    return RedirectResponse(auth.build_auth_url(state=uuid.uuid4().hex))


@app.get("/auth/callback")
def callback(code: str = ""):
    if not code:
        raise HTTPException(401, "No authorization code returned.")
    user = auth.complete_auth(code)
    resp = RedirectResponse("/")
    resp.set_cookie(
        auth.SESSION_COOKIE, auth.session_cookie_value(user), httponly=True, secure=True
    )
    return resp


# ------------------------------------------------------------------- views


@app.get("/", response_class=HTMLResponse)
def home(request: Request, user: User = Depends(auth.current_user)):
    return RedirectResponse(f"/individual/{user.id}/")


@app.get("/{tier}/{owner}/", response_class=HTMLResponse)
def workspace(
    tier: str, owner: str, request: Request, q: str = "", user: User = Depends(auth.current_user)
):
    scope = _scope_or_403(tier, owner, user)
    pages = ab.list_pages(scope)

    # Bootstrap check (MVP spec): a check on the *scope*, not the user — empty
    # scope offers the corpus pipeline, populated scope is the normal
    # workspace. So a later joiner on a built corpus lands on the wiki rather
    # than being asked for documents that already exist.
    if not pages:
        run_id = corpus_runner.latest_run_id(tier, owner)
        status = corpus_runner.load_status(tier, owner, run_id) if run_id else None
        if status and status.state == "running":
            return RedirectResponse(f"/{tier}/{owner}/pipeline/{run_id}", status_code=303)
        return templates.TemplateResponse(
            request,
            "bootstrap.html",
            {"user": user, "tier": tier, "owner": owner, "last_run": status},
        )

    results = ab.search(q, scope) if q else None
    last_run_id = corpus_runner.latest_run_id(tier, owner)
    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "user": user,
            "tier": tier,
            "owner": owner,
            "pages": pages,
            "index_title": wiki.INDEX_TITLE,
            "q": q,
            "results": results,
            "lint_counts": lint.unreviewed_counts(tier, owner),
            "last_run": (
                corpus_runner.load_status(tier, owner, last_run_id) if last_run_id else None
            ),
        },
    )


@app.post("/{tier}/{owner}/bootstrap")
async def bootstrap(
    tier: str,
    owner: str,
    files: list[UploadFile] | None = None,
    user: User = Depends(auth.current_user),
):
    """Stage 1 (store verbatim + extract), then hand off to the background
    pipeline. Extraction happens in-request so unsupported files are rejected
    while the user is still looking at the upload form; everything after that
    is far too slow to hold a request open for."""
    _scope_or_403(tier, owner, user)

    documents: list[dict] = []
    errors: list[str] = []
    for f in files or []:
        if not f.filename:
            continue
        data = await f.read()
        try:
            # No truncation: Stage 2 chunks before extraction, and silently
            # dropping the back half of a 150-page policy document would be
            # invisible in the output.
            source = extractors.extract_file(f.filename, data, max_chars=None)
        except extractors.ExtractionError as e:
            errors.append(f"{f.filename}: {e}")
            continue
        doc_ref = blob_store.write_raw_source(tier, owner, f.filename, data)
        documents.append(
            {
                "doc_ref": doc_ref,
                "doc_name": f.filename,
                "source_type": source.source_type,
                "text": source.text,
                "chars": len(source.text),
                # Each chunk is one extraction call, so this is the per-document
                # cost driver and worth showing before anyone runs a big corpus.
                "chunks": len(corpus_concepts._chunks(source.text)),
            }
        )

    if not documents:
        detail = "No usable documents. " + (" ".join(errors) if errors else "")
        raise HTTPException(422, detail.strip())

    # Partial failures don't stop the run — a folder drop of two hundred files
    # will contain some the extractors can't read — but they are recorded, so
    # a document count lower than what was dropped has a visible reason.
    status = corpus_runner.start_run(tier, owner, documents, user, skipped=errors)
    return RedirectResponse(f"/{tier}/{owner}/pipeline/{status.id}", status_code=303)


@app.get("/{tier}/{owner}/pipeline/{run_id}", response_class=HTMLResponse)
def pipeline_run(
    tier: str, owner: str, run_id: str, request: Request, user: User = Depends(auth.current_user)
):
    """Run progress plus per-stage inspection. The inspection panels are the
    point, not a debug affordance — the MVP's central question is answered by
    reading Stage 2/3 output directly."""
    _scope_or_403(tier, owner, user)
    status = corpus_runner.load_status(tier, owner, run_id)
    if not status:
        raise HTTPException(404, "Pipeline run not found.")
    return templates.TemplateResponse(
        request,
        "pipeline_run.html",
        {
            "user": user,
            "tier": tier,
            "owner": owner,
            "status": status,
            "stages": corpus_models.STAGES,
            "artifacts": {
                name: corpus_runner.load_artifact(tier, owner, run_id, name)
                for name in corpus_models.STAGES
            },
        },
    )


@app.get("/{tier}/{owner}/page/{page_id}", response_class=HTMLResponse)
def view_page(
    tier: str, owner: str, page_id: str, request: Request, user: User = Depends(auth.current_user)
):
    scope = _scope_or_403(tier, owner, user)
    page = ab.read_page(page_id, scope)
    if not page:
        raise HTTPException(404, "Page not found.")
    links_out = [p for pid in page.links_out if (p := ab.read_page(pid, scope))]
    return templates.TemplateResponse(
        request,
        "page.html",
        {
            "user": user,
            "tier": tier,
            "owner": owner,
            "page": page,
            "html": render_markdown(page.body, tier, owner),
            "links_out": links_out,
            "backlinks": wiki.backlinks(page.id, scope),
            "versions": ab.list_versions(page.id, scope),
        },
    )


@app.get("/{tier}/{owner}/page/{page_id}/history", response_class=HTMLResponse)
def page_history(
    tier: str, owner: str, page_id: str, request: Request, user: User = Depends(auth.current_user)
):
    """Chronological history for one page: each version with its date,
    triggering source, and what changed. Derived on demand from the version
    snapshots — the build spec's read-side fix for the individual tier having
    no append log to read back."""
    scope = _scope_or_403(tier, owner, user)
    page = ab.read_page(page_id, scope)
    if not page:
        raise HTTPException(404, "Page not found.")
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "user": user,
            "tier": tier,
            "owner": owner,
            "page": page,
            "entries": wiki.page_history(page_id, scope),
        },
    )


@app.get("/{tier}/{owner}/page/{page_id}/edit", response_class=HTMLResponse)
def edit_page_form(
    tier: str, owner: str, page_id: str, request: Request, user: User = Depends(auth.current_user)
):
    scope = _scope_or_403(tier, owner, user)
    _reject_team_edit(tier)
    page = ab.read_page(page_id, scope)
    if not page:
        raise HTTPException(404, "Page not found.")
    return templates.TemplateResponse(
        request, "edit.html", {"user": user, "tier": tier, "owner": owner, "page": page}
    )


@app.post("/{tier}/{owner}/page/{page_id}/edit")
def edit_page(
    tier: str,
    owner: str,
    page_id: str,
    body: str = Form(...),
    user: User = Depends(auth.current_user),
):
    scope = _scope_or_403(tier, owner, user)
    _reject_team_edit(tier)
    page = ab.read_page(page_id, scope)
    if not page:
        raise HTTPException(404, "Page not found.")
    page.body = body
    page.version += 1
    page.links_out = wiki.resolve_links(body, scope)
    ab.write_page(page, change_type="manual_edit", author_id=user.id)
    return RedirectResponse(f"/{tier}/{owner}/page/{page_id}", status_code=303)


@app.get("/{tier}/{owner}/page-by-title/{title}")
def page_by_title(tier: str, owner: str, title: str, user: User = Depends(auth.current_user)):
    scope = _scope_or_403(tier, owner, user)
    page = ab.find_page_by_title(title, scope)
    if not page:
        raise HTTPException(404, f"No page titled '{title}' yet.")
    return RedirectResponse(f"/{tier}/{owner}/page/{page.id}")


@app.get("/{tier}/{owner}/export")
def export_wiki(tier: str, owner: str, user: User = Depends(auth.current_user)):
    _scope_or_403(tier, owner, user)
    archive = wiki.export_wiki(tier, owner)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{tier}-{owner}-wiki-export.zip"'},
    )


@app.post("/{tier}/{owner}/reindex")
def reindex_scope(tier: str, owner: str, user: User = Depends(auth.current_user)):
    """Rebuild the Cosmos index from canonical blob content — the recovery
    path for index drift or corruption. Blob is never modified; see
    scripts/reindex.py for the same operation with a --dry-run option."""
    _scope_or_403(tier, owner, user)
    ab.reindex(tier, owner)
    return RedirectResponse(f"/{tier}/{owner}/", status_code=303)


@app.get("/team/{team_id}/derived/{slug}", response_class=HTMLResponse)
def derived_view(
    team_id: str, slug: str, request: Request, user: User = Depends(auth.current_user)
):
    _scope_or_403("team", team_id, user)
    body = derived_views.read_derived_view(team_id, slug)
    if body is None:
        raise HTTPException(404, "No derived view yet for this page — run lint or regenerate it.")
    return templates.TemplateResponse(
        request,
        "derived.html",
        {
            "user": user,
            "team_id": team_id,
            "slug": slug,
            "html": render_markdown(body, "team", team_id),
        },
    )


@app.post("/team/{team_id}/derived/{slug}/regenerate")
def derived_view_regenerate(
    team_id: str, slug: str, page_id: str = Form(...), user: User = Depends(auth.current_user)
):
    _scope_or_403("team", team_id, user)
    scope = make_partition_key("team", team_id)
    page = ab.read_page(page_id, scope)
    if not page:
        raise HTTPException(404, "Page not found.")
    derived_views.regenerate_derived_view(page, schema.context_block("team", team_id))
    return RedirectResponse(f"/team/{team_id}/derived/{slug}", status_code=303)


@app.get("/{tier}/{owner}/contents")
def contents(tier: str, owner: str, user: User = Depends(auth.current_user)):
    """Convenience entry to the generated contents/Index page (a real wiki
    page); redirects to the workspace if the wiki has no pages yet."""
    scope = _scope_or_403(tier, owner, user)
    page = ab.find_page_by_title(wiki.INDEX_TITLE, scope)
    if page:
        return RedirectResponse(f"/{tier}/{owner}/page/{page.id}")
    return RedirectResponse(f"/{tier}/{owner}/")


@app.get("/{tier}/{owner}/map", response_class=HTMLResponse)
def knowledge_map_view(
    tier: str, owner: str, request: Request, user: User = Depends(auth.current_user)
):
    """Radial knowledge map: whole-wiki structure grouped by [[link]]-graph
    communities (not embeddings). Builds and caches the map on first view."""
    _scope_or_403(tier, owner, user)
    wiki_map = knowledge_map.get_or_build_map(tier, owner)
    return templates.TemplateResponse(
        request,
        "map.html",
        {"user": user, "tier": tier, "owner": owner, "map": wiki_map},
    )


@app.post("/{tier}/{owner}/map/regenerate")
def knowledge_map_regenerate(tier: str, owner: str, user: User = Depends(auth.current_user)):
    _scope_or_403(tier, owner, user)
    knowledge_map.build_map(tier, owner)
    return RedirectResponse(f"/{tier}/{owner}/map", status_code=303)


@app.get("/{tier}/{owner}/log", response_class=HTMLResponse)
def ingest_log(tier: str, owner: str, request: Request, user: User = Depends(auth.current_user)):
    scope = _scope_or_403(tier, owner, user)
    return templates.TemplateResponse(
        request,
        "log.html",
        {"user": user, "tier": tier, "owner": owner, "entries": ab.list_ingest_log(scope)},
    )


# ------------------------------------------------------------------ ingest


@app.post("/ingest")
async def ingest(
    request: Request,
    mode: str = Form(...),  # whatever mode the toggle showed at submission
    url: str = Form(""),
    pasted: str = Form(""),
    files: list[UploadFile] | None = None,
    user: User = Depends(auth.current_user),
):
    sources: list[extractors.ExtractedSource] = []
    try:
        for f in files or []:
            if f.filename:
                sources.append(extractors.extract_file(f.filename, await f.read()))
        if url.strip():
            sources.append(extractors.extract_url(url.strip()))
        if pasted.strip():
            sources.append(extractors.extract_pasted(pasted))
    except extractors.ExtractionError as e:
        raise HTTPException(422, str(e)) from e
    if not sources:
        raise HTTPException(422, "Nothing to ingest.")

    downgraded = False
    if mode == "automatic" and not lint.is_automatic_ingest_allowed("individual", user.id):
        # Threshold-triggered downgrade (build spec): ingest capture itself
        # is never blocked, only automatic mode — falls through to the
        # manual-session flow below instead of a silent unattended write.
        mode, downgraded = "manual", True

    if mode == "automatic":
        for source in sources:  # per-source, never combined
            pipeline.ingest_automatic(source, user)
        return RedirectResponse(f"/individual/{user.id}/", status_code=303)

    # Manual: sequential per-source sessions; start with the first, queue rest.
    first = pipeline.start_manual_session(sources[0], user)
    for source in sources[1:]:
        pipeline.start_manual_session(source, user)
    suffix = "?downgraded=1" if downgraded else ""
    return RedirectResponse(f"/manual/{first.id}{suffix}", status_code=303)


@app.get("/manual/{session_id}", response_class=HTMLResponse)
def manual_session(
    session_id: str,
    request: Request,
    downgraded: bool = False,
    user: User = Depends(auth.current_user),
):
    session = pipeline.get_session(session_id, user)
    if not session:
        raise HTTPException(404, "Session not found (it may have expired).")
    return templates.TemplateResponse(
        request,
        "manual.html",
        {
            "user": user,
            "session": session,
            "changeset": pipeline.get_changeset(session, user),
            "downgraded": downgraded,
        },
    )


@app.post("/manual/{session_id}/message")
def manual_message(
    session_id: str, message: str = Form(...), user: User = Depends(auth.current_user)
):
    session = pipeline.get_session(session_id, user)
    if not session:
        raise HTTPException(404)
    pipeline.discuss(session, message)
    return RedirectResponse(f"/manual/{session_id}", status_code=303)


@app.post("/manual/{session_id}/propose")
def manual_propose(session_id: str, user: User = Depends(auth.current_user)):
    session = pipeline.get_session(session_id, user)
    if not session:
        raise HTTPException(404)
    pipeline.propose_page(session, user)
    return RedirectResponse(f"/manual/{session_id}", status_code=303)


@app.post("/manual/{session_id}/approve-all")
def manual_approve_all(session_id: str, user: User = Depends(auth.current_user)):
    session = pipeline.get_session(session_id, user)
    if not session:
        raise HTTPException(404)
    pipeline.approve(session, user, None)
    return RedirectResponse(f"/individual/{user.id}/", status_code=303)


@app.post("/manual/{session_id}/approve-selected")
async def manual_approve_selected(
    session_id: str, request: Request, user: User = Depends(auth.current_user)
):
    session = pipeline.get_session(session_id, user)
    if not session:
        raise HTTPException(404)
    form = await request.form()
    selected = {int(v) for v in form.getlist("selected")}
    pipeline.approve(session, user, selected)
    return RedirectResponse(f"/individual/{user.id}/", status_code=303)


@app.post("/manual/{session_id}/discard")
def manual_discard(session_id: str, user: User = Depends(auth.current_user)):
    session = pipeline.get_session(session_id, user)
    if not session:
        raise HTTPException(404)
    pipeline.discard(session, user)
    return RedirectResponse(f"/individual/{user.id}/", status_code=303)


# ------------------------------------------------------------------- query


@app.get("/{tier}/{owner}/query", response_class=HTMLResponse)
def query_form(tier: str, owner: str, request: Request, user: User = Depends(auth.current_user)):
    _scope_or_403(tier, owner, user)
    return templates.TemplateResponse(
        request, "query.html", {"user": user, "tier": tier, "owner": owner, "qa": None}
    )


@app.post("/{tier}/{owner}/query", response_class=HTMLResponse)
def query_ask(
    tier: str,
    owner: str,
    request: Request,
    question: str = Form(...),
    user: User = Depends(auth.current_user),
):
    _scope_or_403(tier, owner, user)
    qa = query.ask(question, tier, owner)
    return templates.TemplateResponse(
        request, "query.html", {"user": user, "tier": tier, "owner": owner, "qa": qa}
    )


@app.post("/query/save")
def query_save_prepare(
    tier: str = Form(...),
    owner: str = Form(...),
    question: str = Form(...),
    answer: str = Form(...),
    user: User = Depends(auth.current_user),
):
    _scope_or_403(tier, owner, user)
    qa = query.QueryAnswer(question=question, answer=answer, tier=tier, owner=owner)
    changeset = query.prepare_save(qa, user)
    return RedirectResponse(f"/query/save/{changeset.id}", status_code=303)


@app.get("/query/save/{changeset_id}", response_class=HTMLResponse)
def query_save_preview(
    changeset_id: str, request: Request, user: User = Depends(auth.current_user)
):
    changeset = query.get_changeset(changeset_id, user)
    if not changeset:
        raise HTTPException(404, "Changeset not found (it may have expired).")
    return templates.TemplateResponse(
        request, "query_save.html", {"user": user, "changeset": changeset}
    )


@app.post("/query/save/{changeset_id}/approve-all")
def query_save_approve_all(changeset_id: str, user: User = Depends(auth.current_user)):
    changeset = query.get_changeset(changeset_id, user)
    if not changeset:
        raise HTTPException(404)
    pages = query.approve(changeset, user, None)
    dest = pages[0] if pages else None
    if dest:
        return RedirectResponse(f"/{changeset.tier}/{changeset.owner}/page/{dest.id}", 303)
    return RedirectResponse(f"/{changeset.tier}/{changeset.owner}/query", status_code=303)


@app.post("/query/save/{changeset_id}/approve-selected")
async def query_save_approve_selected(
    changeset_id: str, request: Request, user: User = Depends(auth.current_user)
):
    changeset = query.get_changeset(changeset_id, user)
    if not changeset:
        raise HTTPException(404)
    form = await request.form()
    selected = {int(v) for v in form.getlist("selected")}
    pages = query.approve(changeset, user, selected)
    dest = pages[0] if pages else None
    if dest:
        return RedirectResponse(f"/{changeset.tier}/{changeset.owner}/page/{dest.id}", 303)
    return RedirectResponse(f"/{changeset.tier}/{changeset.owner}/query", status_code=303)


@app.post("/query/save/{changeset_id}/discard")
def query_save_discard(changeset_id: str, user: User = Depends(auth.current_user)):
    changeset = query.get_changeset(changeset_id, user)
    if changeset:
        query.discard(changeset)
        return RedirectResponse(f"/{changeset.tier}/{changeset.owner}/query", status_code=303)
    return RedirectResponse("/", status_code=303)


# -------------------------------------------------------------------- push


@app.post("/push/prepare")
def push_prepare(
    page_id: str = Form(...), team_id: str = Form(...), user: User = Depends(auth.current_user)
):
    auth.require_team(user, team_id)
    page = ab.read_page(page_id, make_partition_key("individual", user.id))
    if not page:
        raise HTTPException(404, "Page not found in your individual wiki.")
    changeset = push.prepare_push(page, team_id, user)
    return RedirectResponse(f"/push/{changeset.id}", status_code=303)


@app.get("/push/{changeset_id}", response_class=HTMLResponse)
def push_preview(changeset_id: str, request: Request, user: User = Depends(auth.current_user)):
    changeset = push.get_changeset(changeset_id, user)
    if not changeset:
        raise HTTPException(404, "Changeset not found (it may have expired).")
    return templates.TemplateResponse(request, "push.html", {"user": user, "changeset": changeset})


@app.post("/push/{changeset_id}/approve-all")
def push_approve_all(changeset_id: str, user: User = Depends(auth.current_user)):
    changeset = push.get_changeset(changeset_id, user)
    if not changeset:
        raise HTTPException(404)
    pages = push.confirm_push(changeset, user, None)
    dest = pages[0] if pages else None
    if dest:
        return RedirectResponse(f"/team/{changeset.owner}/page/{dest.id}", status_code=303)
    return RedirectResponse(f"/team/{changeset.owner}/", status_code=303)


@app.post("/push/{changeset_id}/approve-selected")
async def push_approve_selected(
    changeset_id: str, request: Request, user: User = Depends(auth.current_user)
):
    changeset = push.get_changeset(changeset_id, user)
    if not changeset:
        raise HTTPException(404)
    form = await request.form()
    selected = {int(v) for v in form.getlist("selected")}
    pages = push.confirm_push(changeset, user, selected)
    dest = pages[0] if pages else None
    if dest:
        return RedirectResponse(f"/team/{changeset.owner}/page/{dest.id}", status_code=303)
    return RedirectResponse(f"/team/{changeset.owner}/", status_code=303)


@app.post("/push/{changeset_id}/discard")
def push_discard(changeset_id: str, user: User = Depends(auth.current_user)):
    changeset = push.get_changeset(changeset_id, user)
    if changeset:
        push.discard_changeset(changeset)
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------ schema


@app.get("/{tier}/{owner}/schema", response_class=HTMLResponse)
def schema_edit(tier: str, owner: str, request: Request, user: User = Depends(auth.current_user)):
    _scope_or_403(tier, owner, user)
    return templates.TemplateResponse(
        request,
        "schema.html",
        {"user": user, "tier": tier, "owner": owner, "body": schema.read_schema(tier, owner)},
    )


@app.post("/{tier}/{owner}/schema")
def schema_save(
    tier: str,
    owner: str,
    body: str = Form(...),
    user: User = Depends(auth.current_user),
):
    _scope_or_403(tier, owner, user)
    schema.write_schema(tier, owner, body)
    return RedirectResponse(f"/{tier}/{owner}/schema", status_code=303)


# -------------------------------------------------------------------- lint


@app.get("/{tier}/{owner}/lint", response_class=HTMLResponse)
def lint_queue(
    tier: str,
    owner: str,
    request: Request,
    origin: str = "",
    user: User = Depends(auth.current_user),
):
    _scope_or_403(tier, owner, user)
    findings = lint.list_findings(tier, owner, status="pending", origin_mode=origin or None)
    return templates.TemplateResponse(
        request,
        "lint.html",
        {
            "user": user,
            "tier": tier,
            "owner": owner,
            "lint_mode": lint.get_lint_mode(tier, owner),
            "counts": lint.unreviewed_counts(tier, owner),
            "findings": findings,
            "origin": origin,
            "lint_status": lint.lint_status(tier, owner),
        },
    )


@app.post("/{tier}/{owner}/lint/mode")
def lint_set_mode(
    tier: str, owner: str, mode: str = Form(...), user: User = Depends(auth.current_user)
):
    _scope_or_403(tier, owner, user)
    lint.set_lint_mode(tier, owner, mode)  # type: ignore[arg-type]
    return RedirectResponse(f"/{tier}/{owner}/lint", status_code=303)


@app.post("/{tier}/{owner}/lint/run")
def lint_run(tier: str, owner: str, user: User = Depends(auth.current_user)):
    scope = _scope_or_403(tier, owner, user)
    # Background thread, not inline: a lint pass over a corpus wiki is dozens of
    # model calls and would hang the request. Cap at the actual page count so a
    # manual run covers the whole wiki, not the per-ingest default of 15.
    page_count = len(ab.list_pages(scope))
    lint.start_lint(tier, owner, user, max_judgment_pages=page_count)
    return RedirectResponse(f"/{tier}/{owner}/lint", status_code=303)


@app.post("/{tier}/{owner}/lint/{finding_id}/apply")
def lint_apply(tier: str, owner: str, finding_id: str, user: User = Depends(auth.current_user)):
    _scope_or_403(tier, owner, user)
    lint.apply_finding(tier, owner, finding_id, user)
    return RedirectResponse(f"/{tier}/{owner}/lint", status_code=303)


@app.post("/{tier}/{owner}/lint/{finding_id}/dismiss")
def lint_dismiss(tier: str, owner: str, finding_id: str, user: User = Depends(auth.current_user)):
    _scope_or_403(tier, owner, user)
    lint.dismiss_finding(tier, owner, finding_id)
    return RedirectResponse(f"/{tier}/{owner}/lint", status_code=303)
