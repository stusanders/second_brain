"""Verifies Cosmos DB + Blob Storage are ready, and ensures the blob
containers exist. Run after creating both accounts and filling in .env:

    uv run python scripts/provision_cosmos.py

Cosmos database/containers are NOT created here. Cosmos DB's Entra ID/RBAC
data-plane auth does not permit metadata operations (create database or
container) — only control-plane paths (Portal Data Explorer, Azure CLI,
IaC) can create them; the data-plane SDK used here is rejected under AAD
auth regardless of assigned role. Since key-based auth is disabled on this
account, create the database (`llmwiki`) and four containers (page_index,
version_index, ingest_log, embeddings — the last with a vector policy: path
/vector, float32, cosine, 1536 dimensions) once via the Portal's Data
Explorer (see docs/AZURE_SETUP_GUIDE.md §4). This script then just confirms
they're reachable with the app's actual runtime credentials.

Cosmos holds only the derived index (page_index, version_index, embeddings,
ingest_log) — see docs/BUILD_SPEC.md storage model. Blob Storage (wiki +
sources containers) is the canonical store; if Cosmos is ever wiped, run
`scripts/reindex.py` per scope to rebuild it from blob.
"""

from app import blob_store
from app.config import get_settings
from app.db import CONTAINERS, get_container


def main() -> None:
    s = get_settings()
    if not s.cosmos_endpoint:
        raise SystemExit("Set COSMOS_ENDPOINT in .env first.")
    if not s.blob_account_name or not s.blob_account_key:
        raise SystemExit("Set BLOB_ACCOUNT_NAME and BLOB_ACCOUNT_KEY in .env first.")

    missing = []
    for name in CONTAINERS:
        try:
            get_container(name).read()
        except Exception:
            missing.append(name)
    if missing:
        raise SystemExit(
            f"Missing Cosmos containers: {', '.join(missing)}. "
            "Create the database + containers via Portal Data Explorer first "
            "(see docs/AZURE_SETUP_GUIDE.md §4) — this script only verifies "
            "and can't create them under Entra ID/RBAC auth."
        )
    print(
        f"Cosmos database '{s.cosmos_database}' reachable with containers: {', '.join(CONTAINERS)}"
    )

    blob_store.ensure_containers()
    print(f"Blob containers ready: {s.blob_wiki_container}, {s.blob_sources_container}")


if __name__ == "__main__":
    main()
