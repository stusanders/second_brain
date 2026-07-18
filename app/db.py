"""Cosmos DB client and container definitions.

Cosmos is a derived index only (see docs/BUILD_SPEC.md storage model) — page
and version bodies live in Blob Storage (app.blob_store); these containers
hold just the metadata needed for structured queries and vector search, and
can be fully rebuilt from blob content via app.abstractions.reindex().

Database/container creation is NOT done here. Cosmos DB's Entra ID/RBAC
data-plane auth does not permit metadata operations (create/delete database
or container) — only control-plane tools (Azure CLI/ARM/Portal) can create
them; the SDK's create_database_if_not_exists/create_container_if_not_exists
are rejected under AAD auth regardless of assigned role. Since key-based
auth is disabled on this account (see docs/AZURE_SETUP_GUIDE.md §4), the
database and containers are created via the `az cosmosdb sql` CLI instead —
see scripts/provision_cosmos.py for the exact commands. Once containers
exist, ordinary read/write/query calls work fine through the RBAC role
assigned to the llm_wiki_poc app registration; only creation is restricted.

Only `app.abstractions` and `scripts/provision_cosmos.py` may import this
module — business logic goes through the abstraction layer.
"""

from functools import lru_cache

from azure.cosmos import CosmosClient
from azure.cosmos.database import DatabaseProxy
from azure.identity import ClientSecretCredential

from app.config import get_settings

CONTAINERS = ["page_index", "version_index", "embeddings", "ingest_log"]

# text-embedding-3-small dimensionality (deployment: llm-wiki-embed)
EMBED_DIMENSIONS = 1536

VECTOR_EMBEDDING_POLICY = {
    "vectorEmbeddings": [
        {
            "path": "/vector",
            "dataType": "float32",
            "distanceFunction": "cosine",
            "dimensions": EMBED_DIMENSIONS,
        }
    ]
}

VECTOR_INDEXING_POLICY = {
    "indexingMode": "consistent",
    "includedPaths": [{"path": "/*"}],
    # vectors must be excluded from the regular index
    "excludedPaths": [{"path": "/vector/*"}, {"path": '/"_etag"/?'}],
    "vectorIndexes": [{"path": "/vector", "type": "diskANN"}],
}


@lru_cache
def _credential() -> ClientSecretCredential:
    """Entra ID auth via the llm_wiki_poc app registration's own credentials
    — the account has key-based auth disabled, so this is the only path in.
    Requires the Cosmos DB Built-in Data Contributor role assigned to this
    app registration (data-plane RBAC, separate from sign-in permissions)."""
    s = get_settings()
    return ClientSecretCredential(s.entra_tenant_id, s.entra_client_id, s.entra_client_secret)


@lru_cache
def get_database() -> DatabaseProxy:
    s = get_settings()
    client = CosmosClient(s.cosmos_endpoint, credential=_credential())
    return client.get_database_client(s.cosmos_database)


def get_container(name: str):
    return get_database().get_container_client(name)
