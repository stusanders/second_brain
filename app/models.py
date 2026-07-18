"""Core entities.

Storage model (see docs/BUILD_SPEC.md "Storage model: markdown canonical,
index rebuildable"): Blob Storage holds canonical page/version markdown and
immutable raw sources. Cosmos DB holds only a derived, rebuildable index
(PageIndex, VersionIndex, Embedding, IngestLog) — no page body lives in
Cosmos. `Page` below is the runtime/business-logic representation (body
included); `app.abstractions` is responsible for splitting it across blob
(body) and Cosmos (index) on write, and reassembling it on read.

All Cosmos documents carry a computed `partition_key` of the form
`individual:{user_id}` or `team:{team_id}` so tenant data is isolated."""

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

Tier = Literal["individual", "team"]
ChangeType = Literal["ingest", "merge_append", "manual_edit", "derived_view_regen"]
IngestMode = Literal["manual", "automatic"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def make_partition_key(tier: Tier, owner: str) -> str:
    return f"{tier}:{owner}"


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def slugify(title: str) -> str:
    slug = _SLUG_RE.sub("-", title.strip().lower()).strip("-")
    return slug or "untitled"


class Page(BaseModel):
    """Runtime representation. Persisted body lives in Blob Storage
    (blob_path below); everything else is mirrored into the Cosmos
    PageIndex/VersionIndex for search and structured queries."""

    id: str = Field(default_factory=new_id)
    partition_key: str
    title: str
    slug: str = ""
    body: str = ""  # markdown — canonical copy lives in blob, not Cosmos
    blob_path: str = ""  # e.g. individual/{user_id}/{slug}.md
    tier: Tier
    owner_id: str  # user id (individual) or team/group id (team)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    source_refs: list[str] = []  # blob paths of raw sources that contributed
    links_out: list[str] = []  # page ids referenced via [[wikilinks]]
    version: int = 1
    body_sha256: str = ""


class PageIndex(BaseModel):
    """Cosmos storage schema for a page — metadata only, no body."""

    id: str
    partition_key: str
    title: str
    slug: str
    blob_path: str
    tier: Tier
    owner_id: str
    created_at: str
    updated_at: str
    source_refs: list[str] = []
    links_out: list[str] = []
    current_version: int = 1
    body_sha256: str = ""


class VersionIndex(BaseModel):
    """Cosmos storage schema for a version snapshot — points at the blob
    holding the full snapshot body, doesn't store it."""

    id: str = Field(default_factory=new_id)
    partition_key: str
    page_id: str
    version_number: int
    blob_path: str
    timestamp: str = Field(default_factory=now_iso)
    change_type: ChangeType
    author_id: str


class Embedding(BaseModel):
    id: str = Field(default_factory=new_id)
    partition_key: str
    page_id: str
    vector: list[float]
    model_version: str
    body_sha256: str  # hash of the page body this vector was computed from


class IngestLogEntry(BaseModel):
    id: str = Field(default_factory=new_id)
    partition_key: str
    timestamp: str = Field(default_factory=now_iso)
    source_type: str
    source_ref: str  # blob path of the raw source in the sources container,
    # or a free-text description for non-file-backed entries (e.g. a push)
    mode: IngestMode
    pages_affected: list[str] = []
    tier: Tier


class User(BaseModel):
    """Resolved at sign-in from the ID token; never persisted."""

    id: str
    name: str
    email: str = ""
    # group id -> team display name, filtered to configured TEAM_GROUPS
    teams: dict[str, str] = {}
