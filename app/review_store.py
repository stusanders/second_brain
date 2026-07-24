"""Durable storage for in-flight review state — manual-mode ingest sessions
and pending changesets.

These were module-level dicts, which the deployment shape makes untenable: the
build spec puts this app on Azure Container Apps, which scales to zero and can
run several replicas. In-process state means an in-flight discussion or a push
awaiting confirmation dies on restart, and an approve POST can land on a
replica that has never seen the changeset — a 404 in the middle of review.

Stored as non-indexed blobs following the `_`-prefix convention already used
by the lint queue, the knowledge map cache and derived views;
`blob_store.list_page_paths` excludes those names, so none of this is ever
mistaken for wiki content.

Everything is filed under the **owning user's** individual scope, including
team-tier changesets. Lookups arrive with only an id and the authenticated
user (`/manual/{id}`, `/push/{id}`), so scoping by user makes the ownership
check structural — you can only read a path built from your own id — rather
than an equality test someone can forget to write.
"""

import json
import time

from app import blob_store

# Abandoned reviews shouldn't accumulate forever; the manual-session 404 copy
# ("it may have expired") already promises this.
MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _path(kind: str, user_id: str, item_id: str) -> str:
    return f"individual/{user_id}/_{kind}/{item_id}.json"


def save(kind: str, user_id: str, item_id: str, payload: dict) -> None:
    payload = {**payload, "_saved_at": time.time()}
    blob_store.write_text(_path(kind, user_id, item_id), json.dumps(payload, indent=2))


def load(kind: str, user_id: str, item_id: str) -> dict | None:
    raw = blob_store.read_text(_path(kind, user_id, item_id))
    if raw is None:
        return None
    payload = json.loads(raw)
    if time.time() - payload.get("_saved_at", 0) > MAX_AGE_SECONDS:
        delete(kind, user_id, item_id)
        return None
    payload.pop("_saved_at", None)
    return payload


def delete(kind: str, user_id: str, item_id: str) -> None:
    blob_store.delete(_path(kind, user_id, item_id))
