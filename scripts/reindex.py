"""Rebuild the Cosmos index for one scope from canonical blob content.

The build spec requires this to be "runnable on demand" — it is the recovery
path for index corruption, drift, or a wiped Cosmos account. Blob is never
modified: page bodies, version snapshots and raw sources are read only.

    uv run python scripts/reindex.py --tier individual --owner <user_id> --dry-run
    uv run python scripts/reindex.py --tier team --owner <team_id>

There is also a "Rebuild index" button on each workspace page, which calls
the same abstractions.reindex().
"""

import argparse

from app import abstractions as ab


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", required=True, choices=["individual", "team"])
    parser.add_argument("--owner", required=True, help="user id (individual) or team/group id")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be rebuilt without writing to Cosmos",
    )
    args = parser.parse_args()

    summary = ab.reindex(args.tier, args.owner, dry_run=args.dry_run)

    prefix = "Would reindex" if args.dry_run else "Reindexed"
    print(f"{prefix} {summary['pages']} pages in {summary['scope']}.")
    if summary["legacy_ids"]:
        print(
            f"  {summary['legacy_ids']} page(s) had no frontmatter id and were assigned a "
            "stable derived id. Their next write will persist it."
        )
    if not args.dry_run:
        print(f"  {summary['versions']} version snapshot(s) re-indexed.")


if __name__ == "__main__":
    main()
