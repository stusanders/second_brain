import re
import uuid

import markdown as md
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import abstractions as ab
from app import auth, push, wiki
from app.ingest import extractors, pipeline
from app.models import User, make_partition_key

app = FastAPI(title="LLM Wiki POC")
templates = Jinja2Templates(directory="templates")

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)\]\]")


def render_markdown(body: str, tier: str, owner: str) -> str:
    def link(match: re.Match) -> str:
        title = match.group(1).strip()
        return f'<a href="/{tier}/{owner}/page-by-title/{title}">{title}</a>'

    return md.markdown(WIKILINK_RE.sub(link, body), extensions=["tables", "fenced_code"])


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
    results = ab.search(q, scope) if q else None
    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "user": user,
            "tier": tier,
            "owner": owner,
            "pages": ab.list_pages(scope),
            "index_title": wiki.INDEX_TITLE,
            "q": q,
            "results": results,
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


@app.get("/{tier}/{owner}/page-by-title/{title}")
def page_by_title(tier: str, owner: str, title: str, user: User = Depends(auth.current_user)):
    scope = _scope_or_403(tier, owner, user)
    page = ab.find_page_by_title(title, scope)
    if not page:
        raise HTTPException(404, f"No page titled '{title}' yet.")
    return RedirectResponse(f"/{tier}/{owner}/page/{page.id}")


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

    if mode == "automatic":
        affected = []
        for source in sources:  # per-source, never combined
            affected += pipeline.ingest_automatic(source, user)
        return RedirectResponse(f"/individual/{user.id}/", status_code=303)

    # Manual: sequential per-source sessions; start with the first, queue rest.
    first = pipeline.start_manual_session(sources[0], user)
    for source in sources[1:]:
        pipeline.start_manual_session(source, user)
    return RedirectResponse(f"/manual/{first.id}", status_code=303)


@app.get("/manual/{session_id}", response_class=HTMLResponse)
def manual_session(session_id: str, request: Request, user: User = Depends(auth.current_user)):
    session = pipeline.get_session(session_id, user)
    if not session:
        raise HTTPException(404, "Session not found (it may have expired).")
    return templates.TemplateResponse(request, "manual.html", {"user": user, "session": session})


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


@app.post("/manual/{session_id}/approve")
def manual_approve(session_id: str, user: User = Depends(auth.current_user)):
    session = pipeline.get_session(session_id, user)
    if not session:
        raise HTTPException(404)
    pipeline.approve(session, user)
    return RedirectResponse(f"/individual/{user.id}/", status_code=303)


@app.post("/manual/{session_id}/discard")
def manual_discard(session_id: str, user: User = Depends(auth.current_user)):
    pipeline.discard(session_id)
    return RedirectResponse(f"/individual/{user.id}/", status_code=303)


# -------------------------------------------------------------------- push


@app.post("/push/prepare")
def push_prepare(
    page_id: str = Form(...), team_id: str = Form(...), user: User = Depends(auth.current_user)
):
    auth.require_team(user, team_id)
    page = ab.read_page(page_id, make_partition_key("individual", user.id))
    if not page:
        raise HTTPException(404, "Page not found in your individual wiki.")
    preview = push.prepare_push(page, team_id, user)
    return RedirectResponse(f"/push/{preview.id}", status_code=303)


@app.get("/push/{preview_id}", response_class=HTMLResponse)
def push_preview(preview_id: str, request: Request, user: User = Depends(auth.current_user)):
    preview = push.get_preview(preview_id, user)
    if not preview:
        raise HTTPException(404, "Preview not found (it may have expired).")
    return templates.TemplateResponse(request, "push.html", {"user": user, "preview": preview})


@app.post("/push/{preview_id}/confirm")
def push_confirm(preview_id: str, user: User = Depends(auth.current_user)):
    preview = push.get_preview(preview_id, user)
    if not preview:
        raise HTTPException(404)
    page = push.confirm_push(preview, user)
    return RedirectResponse(f"/team/{preview.team_id}/page/{page.id}", status_code=303)


@app.post("/push/{preview_id}/discard")
def push_discard(preview_id: str, user: User = Depends(auth.current_user)):
    push.discard_preview(preview_id)
    return RedirectResponse("/", status_code=303)
