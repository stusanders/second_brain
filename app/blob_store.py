"""Blob Storage client — the canonical content store (build spec: "Storage
model: markdown canonical, index rebuildable").

Only `app.abstractions`, `scripts/provision_cosmos.py`, and modules that
persist non-indexed app metadata alongside the wiki (`app.schema`,
`app.lint`, `app.derived_views`, `app.knowledge_map`, `app.ingest.pipeline`)
may import this module — business logic goes through the abstraction layer.

Two containers:
    wiki    — page markdown + version snapshots (canonical wiki content)
    sources — immutable raw ingested files (never edited or deleted)
"""

import contextlib
import time
from functools import lru_cache

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobLeaseClient, BlobServiceClient

from app.config import get_settings

LEASE_SECONDS = 15  # short-lived; team writes acquire, write, release promptly
LEASE_RETRY_ATTEMPTS = 5
LEASE_RETRY_INITIAL_SECONDS = 0.25  # doubles per attempt: ~4s total before giving up


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
    reindex() recovery path walks this to rebuild the Cosmos index.

    Names starting with "_" are reserved for app metadata (e.g. _schema.md,
    _lint/queue.json) and excluded — they are not wiki pages and must never
    be swept into the page index."""
    prefix = f"{tier}/{owner_id}/"
    return [
        b.name
        for b in _wiki_container().list_blobs(name_starts_with=prefix)
        if b.name.endswith(".md")
        and "/" not in b.name[len(prefix) :]
        and not b.name[len(prefix) :].startswith("_")
    ]


def delete(path: str) -> None:
    """Only for disposable app-metadata blobs (review state, caches). Page
    bodies, version snapshots and raw sources are never deleted."""
    with contextlib.suppress(ResourceNotFoundError):
        _wiki_container().delete_blob(path)


def list_paths(prefix: str) -> list[str]:
    """All blob names under a prefix, any depth — used for app-metadata
    blobs (schema doc version snapshots, lint queue) that live outside the
    page-listing convention above."""
    return [b.name for b in _wiki_container().list_blobs(name_starts_with=prefix)]


# ------------------------------------------------------------ concurrency


class LeaseContentionError(RuntimeError):
    """A team page stayed leased by another writer for the whole retry
    window. Surfaced rather than swallowed: the caller's write did not
    happen, and the user must be told."""


def acquire_lease(path: str) -> str | None:
    """Team-tier writes take a lease before writing and release after.

    Retries on contention rather than failing the first time — leases are
    held for LEASE_SECONDS at most and the append-only rule makes a retry
    cheap (re-read and re-append, never a merge). Returns None if the blob
    doesn't exist yet: there is nothing to lease, and the first writer for a
    new page has no contention to lose to.
    """
    blob_client = _wiki_container().get_blob_client(path)
    if not blob_client.exists():
        return None

    delay = LEASE_RETRY_INITIAL_SECONDS
    for attempt in range(LEASE_RETRY_ATTEMPTS):
        try:
            lease = BlobLeaseClient(blob_client)
            lease.acquire(lease_duration=LEASE_SECONDS)  # returns None; the id is on the client
            return lease.id
        except (ResourceExistsError, HttpResponseError) as e:
            status = getattr(e, "status_code", None)
            if status != 409:
                raise
            if attempt == LEASE_RETRY_ATTEMPTS - 1:
                raise LeaseContentionError(
                    f"Could not acquire a lease on {path} after "
                    f"{LEASE_RETRY_ATTEMPTS} attempts — another write is in progress."
                ) from e
            time.sleep(delay)
            delay *= 2
    return None  # unreachable; keeps the type checker honest


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
