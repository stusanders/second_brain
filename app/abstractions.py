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

from functools import lru_cache

from openai import AzureOpenAI

from app import blob_store, db
from app.config import get_settings
from app.models import (
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
    json_mode: bool = False,
    deployment: str = "",
) -> str:
    """Single chat completion. `context` is retrieved wiki/source content kept
    separate from the instruction so callers keep scope narrow (cost discipline).

    `temperature` is forwarded only when set AND the deployed chat model
    supports it (`chat_supports_temperature`) — reasoning-family models like
    gpt-5-nano reject `temperature != 1`, so passing it there is a 400. It is
    therefore plumbed but inert on the current model until a swap flips the flag.

    `json_mode` requests a JSON-object response (structured pipeline outputs);
    `deployment` overrides the chat deployment for this call (empty = default)
    — the corpus pipeline passes `corpus_chat_deployment` through here so the
    synthesis stages can run on a stronger model via config alone."""
    s = get_settings()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    user_content = f"<context>\n{context}\n</context>\n\n{prompt}" if context else prompt
    messages.append({"role": "user", "content": user_content})
    resp = _openai().chat.completions.create(
        model=deployment or s.azure_openai_chat_deployment,
        messages=messages,
        max_completion_tokens=max_tokens,
        reasoning_effort="low",
        **({"response_format": {"type": "json_object"}} if json_mode else {}),
        **({"temperature": temperature} if _temperature_ok(temperature) else {}),
    )
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


# --------------------------------------------------------------------- embed


def embed(text: str) -> list[float]:
    s = get_settings()
    resp = _openai().embeddings.create(
        model=s.azure_openai_embed_deployment,
        input=text[:32000],
    )
    return resp.data[0].embedding


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


def write_page(page: Page, *, change_type: str, author_id: str) -> Page:
    """Persist a page: write markdown to blob (canonical), then update the
    Cosmos index (metadata + version pointer), then refresh its embedding if
    the body changed. Team-tier writes take a blob lease around the write —
    append-only means a lease-retry is just a re-read-and-re-append, no
    merge logic needed."""
    if not page.slug:
        page.slug = slugify(page.title)
    page.blob_path = blob_store.page_blob_path(page.tier, page.owner_id, page.slug)
    page.updated_at = now_iso()
    new_hash = body_hash(page.body)
    changed = new_hash != page.body_sha256
    page.body_sha256 = new_hash

    lease_id = blob_store.acquire_lease(page.blob_path) if page.tier == "team" else None
    try:
        blob_store.write_text(page.blob_path, page.body, lease_id=lease_id)
    finally:
        if lease_id:
            blob_store.release_lease(page.blob_path, lease_id)

    version_path = blob_store.version_blob_path(page.tier, page.owner_id, page.slug, page.version)
    blob_store.write_text(version_path, page.body)

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
    db.get_container("version_index").upsert_item(
        VersionIndex(
            partition_key=page.partition_key,
            page_id=page.id,
            version_number=page.version,
            blob_path=version_path,
            change_type=change_type,  # type: ignore[arg-type]
            author_id=author_id,
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


def reindex(tier: str, owner_id: str) -> int:
    """Rebuild the Cosmos index for one scope from blob content — the
    recovery path for any index corruption or drift (see write_page). Cosmos
    can be wiped entirely and rebuilt this way; blob is the only store that
    actually matters for durability. Returns the number of pages reindexed."""
    from app.models import Page as _Page  # local import avoids a cycle at module load

    scope = make_partition_key(tier, owner_id)  # type: ignore[arg-type]
    count = 0
    for path in blob_store.list_page_paths(tier, owner_id):
        body = blob_store.read_text(path)
        if body is None:
            continue
        slug = path.rsplit("/", 1)[-1].removesuffix(".md")
        title = body.splitlines()[0].lstrip("# ").strip() if body else slug
        page = _Page(
            partition_key=scope,
            title=title,
            slug=slug,
            body=body,
            blob_path=path,
            tier=tier,  # type: ignore[arg-type]
            owner_id=owner_id,
            body_sha256="",  # force re-embed since we can't trust prior hash
        )
        write_page(page, change_type="derived_view_regen", author_id="system:reindex")
        count += 1
    return count


# ------------------------------------------------------------- reset_scope


def reset_scope(tier: str, owner_id: str) -> dict:
    """Wipe a scope's generated output for a corpus rebuild: wiki blobs
    (pages, version snapshots, _map/_lint/_schema artifacts, _corpus outputs)
    and the scope's Cosmos rows across all containers.

    Two things deliberately survive: raw `sources/` blobs (immutable by rule —
    delete_prefix only ever touches the wiki container) and the Stage 2
    concept cache (`_corpus/concepts/`), so an eval re-run skips re-paying
    one model call per unchanged document. Returns deletion counts."""
    scope = make_partition_key(tier, owner_id)  # type: ignore[arg-type]
    prefix = f"{tier}/{owner_id}/"
    blobs_deleted = blob_store.delete_prefix(prefix, keep_prefix=f"{prefix}_corpus/concepts/")

    rows_deleted = 0
    for name in db.CONTAINERS:
        container = db.get_container(name)
        ids = [
            r["id"]
            for r in container.query_items(
                query="SELECT c.id FROM c WHERE c.partition_key = @pk",
                parameters=[{"name": "@pk", "value": scope}],
                partition_key=scope,
            )
        ]
        for item_id in ids:
            container.delete_item(item_id, partition_key=scope)
            rows_deleted += 1
    return {"blobs_deleted": blobs_deleted, "rows_deleted": rows_deleted}


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


def read_page(page_id: str, scope: str) -> Page | None:
    idx = _read_page_index(page_id, scope)
    if not idx:
        return None
    body = blob_store.read_text(idx.blob_path)
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
    body = blob_store.read_text(idx.blob_path)
    return _index_to_page(idx, body or "")


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


# --------------------------------------------------------------- raw sources


def read_raw_source(path: str) -> bytes | None:
    """Passthrough so routes can serve stored source documents (provenance
    check on synthesized pages) without importing blob_store directly."""
    return blob_store.read_raw_source(path)


def count_raw_sources(tier: str, owner_id: str) -> int:
    return len(blob_store.list_raw_source_paths(tier, owner_id))
