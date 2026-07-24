"""The abstraction layer (build spec: non-negotiable).

Every external-system call goes through these five named functions. At
Copilot API migration time, only this module's internals change:

    call_model()  -> Copilot API model layer
    get_context() -> Retrieval/Context API
    search()      -> Search API
    write_page()  -> Work IQ Tools API
    embed()       -> (whatever embedding surface applies)

Business logic (ingest/query/merge) must not import azure.cosmos,
azure.storage.blob, or openai directly — those live behind this module,
app.db, and app.blob_store.

Storage model: Blob Storage is canonical (page bodies + version snapshots +
raw sources); Cosmos DB is a derived, rebuildable index (metadata + vector
search only). Writes go blob-first, then Cosmos — if the index write fails
after a successful blob write, content is safe and the index is merely
stale, recoverable via reindex(). The reverse order would risk an index
entry pointing at content that was never actually written.
"""

import json
import uuid
from functools import lru_cache

from openai import AzureOpenAI

from app import blob_store, db, frontmatter
from app.config import get_settings
from app.models import (
    WIKILINK_RE,
    Embedding,
    IngestLogEntry,
    Page,
    PageIndex,
    VersionIndex,
    body_hash,
    make_partition_key,
    now_iso,
    slugify,
)

# Azure OpenAI caps inputs per embeddings request; well under it and still
# far cheaper than one round trip per string.
EMBED_BATCH_SIZE = 64

# Reasoning models bill hidden reasoning against max_completion_tokens, so a
# response can come back empty with finish_reason="length". One retry with a
# larger budget costs far less than failing the operation. The floor matters
# because callers with a small budget (lint's verdicts use 100) need absolute
# headroom, not a multiple of something already too small for the reasoning.
LENGTH_RETRY_MULTIPLIER = 3
LENGTH_RETRY_FLOOR = 2000


@lru_cache
def _openai() -> AzureOpenAI:
    s = get_settings()
    return AzureOpenAI(
        azure_endpoint=s.azure_openai_endpoint,
        api_key=s.azure_openai_api_key,
        api_version=s.azure_openai_api_version,
    )


# ---------------------------------------------------------------- call_model


def _temperature_ok(temperature: float | None) -> bool:
    """Only forward an explicit temperature to the SDK when the deployed chat
    model actually accepts it (reasoning-family models reject non-default)."""
    return temperature is not None and get_settings().chat_supports_temperature


def call_model(
    prompt: str,
    context: str = "",
    *,
    system: str = "",
    max_tokens: int = 2000,
    temperature: float | None = None,
) -> str:
    """Single chat completion. `context` is retrieved wiki/source content kept
    separate from the instruction so callers keep scope narrow (cost discipline).

    `temperature` is forwarded only when set AND the deployed chat model
    supports it (`chat_supports_temperature`) — reasoning-family models like
    gpt-5-nano reject `temperature != 1`, so passing it there is a 400. It is
    therefore plumbed but inert on the current model until a swap flips the flag."""
    s = get_settings()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    user_content = f"<context>\n{context}\n</context>\n\n{prompt}" if context else prompt
    messages.append({"role": "user", "content": user_content})

    def _once(budget: int):
        return _openai().chat.completions.create(
            model=s.azure_openai_chat_deployment,
            messages=messages,
            max_completion_tokens=budget,
            reasoning_effort="low",
            **({"temperature": temperature} if _temperature_ok(temperature) else {}),
        )

    resp = _once(max_tokens)
    content = resp.choices[0].message.content
    if not content and resp.choices[0].finish_reason == "length":
        # Reasoning-family models spend `max_completion_tokens` on hidden
        # reasoning before any visible output, so a small budget can be entirely
        # consumed by reasoning and yield an empty response. Retry once with a
        # budget that is large in absolute terms — tripling a tiny budget (e.g.
        # lint's 100) is still tiny, so a floor is what actually helps.
        retry_budget = max(max_tokens * LENGTH_RETRY_MULTIPLIER, LENGTH_RETRY_FLOOR)
        resp = _once(retry_budget)
        content = resp.choices[0].message.content
    if not content:
        raise RuntimeError(
            f"Model returned empty content (finish_reason={resp.choices[0].finish_reason!r})"
        )
    return content


def call_model_chat(
    messages: list[dict], *, max_tokens: int = 2000, temperature: float | None = None
) -> str:
    """Multi-turn variant for the manual-mode agent-led discussion. Same
    temperature caveat as call_model — forwarded only when the model supports it."""
    s = get_settings()
    resp = _openai().chat.completions.create(
        model=s.azure_openai_chat_deployment,
        messages=messages,
        max_completion_tokens=max_tokens,
        reasoning_effort="low",
        **({"temperature": temperature} if _temperature_ok(temperature) else {}),
    )
    content = resp.choices[0].message.content
    if not content:
        raise RuntimeError(
            f"Model returned empty content (finish_reason={resp.choices[0].finish_reason!r})"
        )
    return content


def call_model_json(
    prompt: str,
    context: str = "",
    *,
    system: str = "",
    max_tokens: int = 4000,
) -> dict:
    """Structured-output variant: asks for JSON and returns the parsed object.

    Every corpus-pipeline stage wants structured data back — concept lists,
    merge verdicts, page sets. Parsing prose for those is the wrong failure
    mode: it fails silently and halfway. Uses the provider's JSON mode, and on
    a parse failure makes exactly one repair attempt (handing the model its own
    malformed output) before giving up loudly.

    The caller's system prompt must itself ask for JSON and describe the shape;
    JSON mode guarantees syntactic validity, not the schema you wanted.
    """
    s = get_settings()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    user_content = f"<context>\n{context}\n</context>\n\n{prompt}" if context else prompt
    messages.append({"role": "user", "content": user_content})

    def _complete(msgs: list[dict]) -> str:
        resp = _openai().chat.completions.create(
            model=s.azure_openai_chat_deployment,
            messages=msgs,
            max_completion_tokens=max_tokens,
            reasoning_effort="low",
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""

    raw = _complete(messages)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        repaired = _complete(
            [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "That was not valid JSON. Reply with the same content as valid JSON only."
                    ),
                },
            ]
        )
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Model did not return valid JSON after a repair attempt: {e}"
            ) from e


# --------------------------------------------------------------------- embed


def embed(text: str) -> list[float]:
    s = get_settings()
    resp = _openai().embeddings.create(
        model=s.azure_openai_embed_deployment,
        input=text[:32000],
    )
    return resp.data[0].embedding


def embed_many(texts: list[str]) -> list[list[float]]:
    """Batched embedding — Stage 3 embeds every extracted concept in the corpus,
    which is thousands of short strings and one round trip per string would
    dominate the stage's wall time. Batches are capped because the endpoint
    limits inputs per request."""
    vectors: list[list[float]] = []
    s = get_settings()
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = [t[:32000] for t in texts[i : i + EMBED_BATCH_SIZE]]
        resp = _openai().embeddings.create(model=s.azure_openai_embed_deployment, input=batch)
        vectors.extend(item.embedding for item in sorted(resp.data, key=lambda d: d.index))
    return vectors


def embedding_model_version() -> str:
    return get_settings().azure_openai_embed_deployment


# -------------------------------------------------------------------- search


def search(query: str, scope: str, top_k: int = 8) -> list[dict]:
    """Vector similarity search scoped to one tenant partition.

    `scope` is a partition key (`individual:{user_id}` or `team:{team_id}`).
    Returns [{page_id, title, score}] best-first. Same Cosmos VectorDistance
    path for both tiers."""
    vector = embed(query)
    return search_by_vector(vector, scope, top_k)


def search_by_vector(vector: list[float], scope: str, top_k: int = 8) -> list[dict]:
    container = db.get_container("embeddings")
    rows = list(
        container.query_items(
            query=(
                "SELECT TOP @k c.page_id, VectorDistance(c.vector, @vec) AS score "
                "FROM c WHERE c.partition_key = @pk "
                "ORDER BY VectorDistance(c.vector, @vec)"
            ),
            parameters=[
                {"name": "@k", "value": top_k},
                {"name": "@vec", "value": vector},
                {"name": "@pk", "value": scope},
            ],
            partition_key=scope,
        )
    )
    results = []
    for row in rows:
        idx = _read_page_index(row["page_id"], scope)
        if idx:
            results.append({"page_id": idx.id, "title": idx.title, "score": row["score"]})
    return results


# --------------------------------------------------------------- get_context


def get_context(query: str, scope: str, top_k: int = 5, max_chars: int = 24000) -> str:
    """Retrieve the most relevant page bodies for a query, concatenated and
    capped — the narrow-context input to call_model()."""
    hits = search(query, scope, top_k=top_k)
    parts: list[str] = []
    used = 0
    for hit in hits:
        page = read_page(hit["page_id"], scope)
        if not page:
            continue
        chunk = f"## {page.title}\n{page.body}\n"
        if used + len(chunk) > max_chars:
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n".join(parts)


# ---------------------------------------------------------------- write_page


def page_frontmatter(page: Page) -> str:
    """The metadata block prepended to a page's canonical blob. Exactly the
    fields `reindex()` cannot otherwise recover once Cosmos is gone — identity,
    version, creation date, and provenance."""
    return frontmatter.serialize(
        {
            "id": page.id,
            "title": page.title,
            "created_at": page.created_at,
            "version": page.version,
            "source_refs": page.source_refs,
        },
        "",
    )


def acquire_page_lease(tier: str, owner_id: str, title: str) -> tuple[str, str | None]:
    """Take a write lease on the blob a page with this title would occupy,
    returning (blob_path, lease_id). Exists so business logic can hold a lease
    across a read-modify-write without importing blob_store directly — the
    abstraction boundary is non-negotiable per the build spec.

    A None lease_id means the page doesn't exist yet, so there is nothing to
    contend over. Raises blob_store.LeaseContentionError if another writer
    holds the lease for the whole retry window."""
    blob_path = blob_store.page_blob_path(tier, owner_id, slugify(title))
    return blob_path, blob_store.acquire_lease(blob_path)


def release_page_lease(blob_path: str, lease_id: str | None) -> None:
    if lease_id:
        blob_store.release_lease(blob_path, lease_id)


def upsert_page_index(page: Page) -> None:
    """Write one page's row into the derived Cosmos index. Shared by the
    normal write path and by reindex(), which rebuilds rows without touching
    blob content."""
    db.get_container("page_index").upsert_item(
        PageIndex(
            id=page.id,
            partition_key=page.partition_key,
            title=page.title,
            slug=page.slug,
            blob_path=page.blob_path,
            tier=page.tier,
            owner_id=page.owner_id,
            created_at=page.created_at,
            updated_at=page.updated_at,
            source_refs=page.source_refs,
            links_out=page.links_out,
            current_version=page.version,
            body_sha256=page.body_sha256,
        ).model_dump()
    )


def write_page(
    page: Page,
    *,
    change_type: str,
    author_id: str,
    lease_id: str | None = None,
    source_ref: str = "",
) -> Page:
    """Persist a page: write markdown to blob (canonical), then update the
    Cosmos index (metadata + version pointer), then refresh its embedding if
    the body changed.

    `lease_id` is for callers that must hold a team-page lease across a
    read-modify-write, not just the write — `wiki.apply_changeset` re-reads and
    re-appends under one lease so two concurrent pushes can't clobber each
    other. When it isn't supplied, a team write takes and releases its own
    lease here, which is sufficient for a write that doesn't depend on a body
    read earlier in the same operation."""
    if not page.slug:
        page.slug = slugify(page.title)
    page.blob_path = blob_store.page_blob_path(page.tier, page.owner_id, page.slug)
    page.updated_at = now_iso()
    new_hash = body_hash(page.body)
    changed = new_hash != page.body_sha256
    page.body_sha256 = new_hash

    stored = page_frontmatter(page) + page.body

    held = lease_id is not None
    if not held and page.tier == "team":
        lease_id = blob_store.acquire_lease(page.blob_path)
    try:
        blob_store.write_text(page.blob_path, stored, lease_id=lease_id)
    finally:
        if lease_id and not held:
            blob_store.release_lease(page.blob_path, lease_id)

    version_path = blob_store.version_blob_path(page.tier, page.owner_id, page.slug, page.version)
    blob_store.write_text(version_path, stored)

    upsert_page_index(page)
    db.get_container("version_index").upsert_item(
        VersionIndex(
            partition_key=page.partition_key,
            page_id=page.id,
            version_number=page.version,
            blob_path=version_path,
            change_type=change_type,  # type: ignore[arg-type]
            author_id=author_id,
            source_ref=source_ref,
        ).model_dump()
    )

    if changed:
        _upsert_embedding(page)
    return page


def _upsert_embedding(page: Page) -> None:
    container = db.get_container("embeddings")
    vector = embed(f"{page.title}\n\n{page.body}")
    existing = list(
        container.query_items(
            query="SELECT c.id FROM c WHERE c.partition_key = @pk AND c.page_id = @pid",
            parameters=[
                {"name": "@pk", "value": page.partition_key},
                {"name": "@pid", "value": page.id},
            ],
            partition_key=page.partition_key,
        )
    )
    doc = Embedding(
        partition_key=page.partition_key,
        page_id=page.id,
        vector=vector,
        model_version=embedding_model_version(),
        body_sha256=page.body_sha256,
    )
    if existing:
        doc.id = existing[0]["id"]
    container.upsert_item(doc.model_dump())


# ------------------------------------------------------------- reindex


def _legacy_page_id(scope: str, slug: str) -> str:
    """Stable id for a page written before frontmatter existed. Derived from
    scope+slug so repeated reindex runs converge on the same id instead of
    minting a new uuid each pass (which would duplicate every index row and
    orphan every links_out reference)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"llmwiki/{scope}/{slug}").hex


def _page_from_blob(path: str, tier: str, owner_id: str, scope: str) -> Page | None:
    """Reconstruct a Page from its canonical blob alone — no Cosmos reads.
    This is the whole premise of the storage model: blob is sufficient."""
    raw = blob_store.read_text(path)
    if raw is None:
        return None
    meta, body = frontmatter.parse(raw)
    slug = path.rsplit("/", 1)[-1].removesuffix(".md")
    title = meta.get("title") or (body.splitlines()[0].lstrip("# ").strip() if body else slug)

    # Version: trust frontmatter, but never below what snapshots prove exists.
    snapshot_versions = [
        int(p.rsplit("/v", 1)[-1].removesuffix(".md"))
        for p in blob_store.list_paths(f"{tier}/{owner_id}/{slug}/v")
        if p.endswith(".md") and p.rsplit("/v", 1)[-1].removesuffix(".md").isdigit()
    ]
    version = max([int(meta.get("version", 1) or 1), *snapshot_versions], default=1)

    return Page(
        id=meta.get("id") or _legacy_page_id(scope, slug),
        partition_key=scope,
        title=title,
        slug=slug,
        body=body,
        blob_path=path,
        tier=tier,  # type: ignore[arg-type]
        owner_id=owner_id,
        created_at=meta.get("created_at") or now_iso(),
        source_refs=meta.get("source_refs", []),
        version=version,
        body_sha256=body_hash(body),
    )


def reindex(tier: str, owner_id: str, *, dry_run: bool = False) -> dict:
    """Rebuild the Cosmos index for one scope from blob content — the recovery
    path for any index corruption or drift (see write_page). Cosmos can be
    wiped entirely and rebuilt this way; blob is the only store that actually
    matters for durability.

    Rebuilds the index *in place*: it never rewrites page bodies, never bumps
    a version, and never overwrites a version snapshot. Page identity, created
    date and source refs come from frontmatter, so a rerun is idempotent and
    provenance survives — the build spec makes provenance a hard requirement,
    and it is unrecoverable from a bare markdown body.

    Returns a summary dict; with dry_run=True nothing is written.
    """
    scope = make_partition_key(tier, owner_id)  # type: ignore[arg-type]

    pages: list[Page] = []
    for path in blob_store.list_page_paths(tier, owner_id):
        page = _page_from_blob(path, tier, owner_id, scope)
        if page:
            pages.append(page)

    # Two passes: every page must be in the title map before links resolve,
    # or a link to a page later in the walk would silently drop.
    by_title = {p.title.lower(): p.id for p in pages}
    for page in pages:
        seen: list[str] = []
        for title in WIKILINK_RE.findall(page.body):
            target = by_title.get(title.strip().lower())
            if target and target not in seen:
                seen.append(target)
        page.links_out = seen

    summary = {
        "scope": scope,
        "pages": len(pages),
        "legacy_ids": sum(1 for p in pages if p.id == _legacy_page_id(scope, p.slug)),
        "versions": 0,
        "dry_run": dry_run,
    }
    if dry_run:
        return summary

    for page in pages:
        upsert_page_index(page)
        _upsert_embedding(page)
        summary["versions"] += _restore_version_index(page)
    return summary


def _restore_version_index(page: Page) -> int:
    """Re-create VersionIndex rows from the version snapshots already in blob.
    Only the snapshots can attest which versions exist; change_type and author
    are not recoverable from blob, so they are recorded honestly as a reindex."""
    container = db.get_container("version_index")
    count = 0
    for path in blob_store.list_paths(f"{page.tier}/{page.owner_id}/{page.slug}/v"):
        tail = path.rsplit("/v", 1)[-1].removesuffix(".md")
        if not path.endswith(".md") or not tail.isdigit():
            continue
        container.upsert_item(
            VersionIndex(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"{page.id}/v{tail}").hex,
                partition_key=page.partition_key,
                page_id=page.id,
                version_number=int(tail),
                blob_path=path,
                change_type="reindex",
                author_id="system:reindex",
            ).model_dump()
        )
        count += 1
    return count


# ------------------------------------------------------- plain reads / log


def _read_page_index(page_id: str, scope: str) -> PageIndex | None:
    try:
        item = db.get_container("page_index").read_item(page_id, partition_key=scope)
        return PageIndex(**{k: v for k, v in item.items() if not k.startswith("_")})
    except Exception:
        return None


def _index_to_page(idx: PageIndex, body: str) -> Page:
    return Page(
        id=idx.id,
        partition_key=idx.partition_key,
        title=idx.title,
        slug=idx.slug,
        body=body,
        blob_path=idx.blob_path,
        tier=idx.tier,
        owner_id=idx.owner_id,
        created_at=idx.created_at,
        updated_at=idx.updated_at,
        source_refs=idx.source_refs,
        links_out=idx.links_out,
        version=idx.current_version,
        body_sha256=idx.body_sha256,
    )


def read_page_body(blob_path: str) -> str | None:
    """Read a page blob and strip its frontmatter. `Page.body` is
    frontmatter-free everywhere in the app — only the blob carries it — so
    diffing, rendering, embedding and changesets never see it."""
    raw = blob_store.read_text(blob_path)
    if raw is None:
        return None
    _, body = frontmatter.parse(raw)
    return body


def read_page(page_id: str, scope: str) -> Page | None:
    idx = _read_page_index(page_id, scope)
    if not idx:
        return None
    body = read_page_body(idx.blob_path)
    if body is None:
        return None
    return _index_to_page(idx, body)


def find_page_by_title(title: str, scope: str) -> Page | None:
    rows = list(
        db.get_container("page_index").query_items(
            query="SELECT * FROM c WHERE c.partition_key = @pk AND LOWER(c.title) = @t",
            parameters=[
                {"name": "@pk", "value": scope},
                {"name": "@t", "value": title.lower()},
            ],
            partition_key=scope,
        )
    )
    if not rows:
        return None
    idx = PageIndex(**{k: v for k, v in rows[0].items() if not k.startswith("_")})
    return _index_to_page(idx, read_page_body(idx.blob_path) or "")


def list_pages(scope: str) -> list[Page]:
    """Lightweight listing (title/id only — templates don't need bodies
    here); avoids an N-blob-read fan-out for a simple page list."""
    rows = db.get_container("page_index").query_items(
        query="SELECT * FROM c WHERE c.partition_key = @pk ORDER BY c.title",
        parameters=[{"name": "@pk", "value": scope}],
        partition_key=scope,
    )
    return [
        _index_to_page(PageIndex(**{k: v for k, v in r.items() if not k.startswith("_")}), "")
        for r in rows
    ]


def list_versions(page_id: str, scope: str) -> list[VersionIndex]:
    rows = db.get_container("version_index").query_items(
        query=(
            "SELECT * FROM c WHERE c.partition_key = @pk AND c.page_id = @pid "
            "ORDER BY c.version_number DESC"
        ),
        parameters=[
            {"name": "@pk", "value": scope},
            {"name": "@pid", "value": page_id},
        ],
        partition_key=scope,
    )
    return [VersionIndex(**{k: v for k, v in r.items() if not k.startswith("_")}) for r in rows]


def append_ingest_log(entry: IngestLogEntry) -> None:
    db.get_container("ingest_log").upsert_item(entry.model_dump())


def list_ingest_log(scope: str, limit: int = 200) -> list[IngestLogEntry]:
    rows = db.get_container("ingest_log").query_items(
        query="SELECT TOP @n * FROM c WHERE c.partition_key = @pk ORDER BY c.timestamp DESC",
        parameters=[
            {"name": "@n", "value": limit},
            {"name": "@pk", "value": scope},
        ],
        partition_key=scope,
    )
    return [IngestLogEntry(**{k: v for k, v in r.items() if not k.startswith("_")}) for r in rows]


def scope_for(tier: str, owner: str) -> str:
    return make_partition_key(tier, owner)  # type: ignore[arg-type]
