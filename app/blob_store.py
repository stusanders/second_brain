"""Blob Storage client — the canonical content store (build spec: "Storage
model: markdown canonical, index rebuildable").

Only `app.abstractions`, `scripts/provision_cosmos.py`'s reindex path, and
`app.ingest.pipeline` (for raw source writes) may import this module —
business logic goes through the abstraction layer.

Two containers:
    wiki    — page markdown + version snapshots (canonical wiki content)
    sources — immutable raw ingested files (never edited or deleted)
"""

from functools import lru_cache

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobLeaseClient, BlobServiceClient

from app.config import get_settings

LEASE_SECONDS = 15  # short-lived; team writes acquire, write, release promptly


@lru_cache
def _service_client() -> BlobServiceClient:
    s = get_settings()
    account_url = f"https://{s.blob_account_name}.blob.core.windows.net"
    return BlobServiceClient(account_url=account_url, credential=s.blob_account_key)


def ensure_containers() -> None:
    """Create the wiki/sources containers if they don't exist. Idempotent —
    safe to call at provisioning time or on app startup."""
    s = get_settings()
    client = _service_client()
    for name in (s.blob_wiki_container, s.blob_sources_container):
        container = client.get_container_client(name)
        if not container.exists():
            container.create_container()


def _wiki_container():
    return _service_client().get_container_client(get_settings().blob_wiki_container)


def _sources_container():
    return _service_client().get_container_client(get_settings().blob_sources_container)


# --------------------------------------------------------------- page bodies


def page_blob_path(tier: str, owner_id: str, slug: str) -> str:
    return f"{tier}/{owner_id}/{slug}.md"


def version_blob_path(tier: str, owner_id: str, slug: str, version_number: int) -> str:
    return f"{tier}/{owner_id}/{slug}/v{version_number}.md"


def write_text(path: str, content: str, *, lease_id: str | None = None) -> None:
    _wiki_container().upload_blob(path, content.encode("utf-8"), overwrite=True, lease=lease_id)


def read_text(path: str) -> str | None:
    try:
        data = _wiki_container().download_blob(path).readall()
    except ResourceNotFoundError:
        return None
    return data.decode("utf-8")


def list_page_paths(tier: str, owner_id: str) -> list[str]:
    """All top-level page blobs (not version snapshots) for a scope — the
    reindex() recovery path walks this to rebuild the Cosmos index."""
    prefix = f"{tier}/{owner_id}/"
    return [
        b.name
        for b in _wiki_container().list_blobs(name_starts_with=prefix)
        if b.name.endswith(".md") and "/" not in b.name[len(prefix) :]
    ]


# ------------------------------------------------------------ concurrency


def acquire_lease(path: str) -> str | None:
    """Team-tier writes take a lease before writing and release after, with
    the caller retrying on failure — cheap because the append-only rule
    means a retry is just a re-read-and-re-append, never a merge. Returns
    None if the blob doesn't exist yet (nothing to lease — first writer for
    a new page has no contention)."""
    blob_client = _wiki_container().get_blob_client(path)
    if not blob_client.exists():
        return None
    lease = BlobLeaseClient(blob_client)
    lease.acquire(lease_duration=LEASE_SECONDS)
    return lease.id


def release_lease(path: str, lease_id: str) -> None:
    blob_client = _wiki_container().get_blob_client(path)
    BlobLeaseClient(blob_client, lease_id=lease_id).release()


# ----------------------------------------------------------- raw sources


def write_raw_source(tier: str, owner_id: str, filename: str, data: bytes) -> str:
    """Store an ingested source verbatim, unmodified, never edited or
    deleted. Returns the blob path, used as IngestLogEntry.source_ref for
    provenance ("where did this claim come from" must always be answerable)."""
    from app.models import new_id

    path = f"{tier}/{owner_id}/{new_id()}-{filename}"
    _sources_container().upload_blob(path, data, overwrite=False)
    return path


def read_raw_source(path: str) -> bytes | None:
    try:
        return _sources_container().download_blob(path).readall()
    except ResourceNotFoundError:
        return None
