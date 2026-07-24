"""Delete everything for one scope — a true clean slate.

    uv run python scripts/wipe_scope.py --tier team --owner dev-team --yes

DESTRUCTIVE and irreversible. Removes, for the given scope:
  - every wiki-container blob (pages, version snapshots, and all `_`-prefixed
    metadata: pipeline runs, map cache, lint queue, schema doc, review state)
  - every raw source blob (the immutable source store)
  - every Cosmos index row (page_index, version_index, embeddings, ingest_log)

Blob is canonical, so deleting blob is the real removal; the Cosmos sweep just
stops the index pointing at content that no longer exists. Without --yes it only
reports what it would delete.

Intended for development resets — throwing away a bad pipeline run and starting
over. It is deliberately scope-scoped: it can wipe one tenant's data and cannot
touch another's, because every path and every query is filtered by the scope.
"""

import argparse

from app import blob_store, db
from app.blob_store import _sources_container
from app.models import make_partition_key


def _plan(tier: str, owner: str) -> dict:
    scope = make_partition_key(tier, owner)  # type: ignore[arg-type]
    prefix = f"{tier}/{owner}/"
    wiki_blobs = blob_store.list_paths(prefix)
    source_blobs = [b.name for b in _sources_container().list_blobs(name_starts_with=prefix)]
    cosmos = {
        name: [
            r["id"]
            for r in db.get_container(name).query_items(
                query="SELECT c.id FROM c WHERE c.partition_key = @pk",
                parameters=[{"name": "@pk", "value": scope}],
                partition_key=scope,
            )
        ]
        for name in db.CONTAINERS
    }
    return {"scope": scope, "wiki": wiki_blobs, "sources": source_blobs, "cosmos": cosmos}


def _report(plan: dict) -> None:
    print(f"  {len(plan['wiki'])} wiki blobs, {len(plan['sources'])} raw sources")
    for name, ids in plan["cosmos"].items():
        if ids:
            print(f"  {len(ids)} cosmos {name} rows")


def _execute(plan: dict) -> None:
    scope = plan["scope"]
    for path in plan["wiki"]:
        blob_store.delete(path)
    sources = _sources_container()
    for path in plan["sources"]:
        sources.delete_blob(path)
    for name, ids in plan["cosmos"].items():
        container = db.get_container(name)
        for item_id in ids:
            container.delete_item(item_id, partition_key=scope)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tier", required=True, choices=["individual", "team"])
    ap.add_argument("--owner", required=True)
    ap.add_argument("--yes", action="store_true", help="actually delete; otherwise dry-run")
    args = ap.parse_args()

    plan = _plan(args.tier, args.owner)
    print(f"scope {plan['scope']}:")
    _report(plan)

    if not args.yes:
        print("\nDry run — nothing deleted. Re-run with --yes to delete.")
        return

    _execute(plan)
    after = _plan(args.tier, args.owner)
    remaining = (
        len(after["wiki"]) + len(after["sources"]) + sum(len(v) for v in after["cosmos"].values())
    )
    print(f"\nDeleted. {remaining} items remain (should be 0).")


if __name__ == "__main__":
    main()
