# Azure & Cloud Setup Guide — LLM Wiki POC

This covers the manual account/infrastructure setup — the parts Claude Code can't do for you because they require your own credentials, billing, and admin access. Do these roughly in order; later steps depend on earlier ones.

## Progress

- [x] **1. Entra ID App Registration** — done 2026-07-17. App `llm_wiki_poc` (client ID `4f7df273-e3b5-47c0-b52d-7fad49c4a511`), Web platform + localhost redirect URI, confidential client secret created, groups claim ("All groups") added, API permissions = `User.Read` only. Tenant ID, client ID, client secret in `.env`.
- [x] **2. Azure OpenAI resource (EU region)** — done 2026-07-18. Foundry resource `llm-wiki-poc-ai` in Sweden Central (`llm-wiki-poc-rg`). Deployed `gpt-5-nano` as `llm-wiki-chat` and `text-embedding-3-small` as `llm-wiki-embed`, both Data Zone Standard (EUR Data Zone). Endpoint + API key in `.env` (key rotated once after being pasted in chat during setup).
- [x] **3. Azure Blob Storage (canonical content store)** — done 2026-07-18. Storage account `llmwikipocstorage` in Sweden Central (`llm-wiki-poc-rg`), Standard performance, LRS redundancy, soft delete for blobs (7-day retention). Containers `wiki` and `sources` created, private access. Account name + key in `.env`.
- [x] **4. Cosmos DB (derived index)** — done 2026-07-18. Account `llm-wiki-poc-cosmos` in Sweden Central (`llm-wiki-poc-rg`), NoSQL API, Serverless, vector search enabled, key-based auth disabled (Entra ID/RBAC only via `llm_wiki_poc`'s Cosmos DB Built-in Data Contributor role assignment). Database `llmwiki` + all four containers created by hand via Data Explorer (container *creation* isn't permitted under data-plane RBAC — see `app/db.py` docstring); `embeddings` has the vector policy (path `/vector`, float32, cosine, 1536 dims, diskANN index). Endpoint in `.env`, no key.
- [ ] **5. Container Apps (hosting)** — not started, not blocking local dev.
- [ ] **6. Confirm team membership structure** — pending first sign-in.
- [ ] **7. Cost monitoring** — not started.

## 1. Entra ID App Registration

This is a config object, not hosted infrastructure — no cost, no server. **No tenant-admin consent is required anywhere in this setup.**

1. Azure Portal → Microsoft Entra ID → App registrations → New registration. Name it (e.g. "llm_wiki_poc").
2. Under **Authentication**, add a **Web** platform. This app runs entirely server-side, so it is a **confidential client** — not a public client.
3. Under **Certificates & secrets**, create a **client secret**. Copy the value immediately; it's only shown once.
4. **Redirect URI**: add `http://localhost:8000/auth/callback` now for local dev. Add the production `https://<your-container-app>.azurecontainerapps.io/auth/callback` after deployment (step 5).
5. Under **Token configuration** → Add groups claim → select **Security groups** (this includes Microsoft 365 Groups, which is what backs Teams). Under "Customize token properties by type", check **Group ID** for both **ID** and **Access** tokens; leave the on-premises AD identifier options and "emit as role claims" unchecked, and leave SAML alone entirely. This resolves team membership from the token with no admin consent needed.
6. Under **API permissions**, keep only the delegated default:
   - Microsoft Graph: `User.Read`
   - **Do not add `Sites.ReadWrite.All`** — SharePoint/OneDrive integration is explicitly out of scope, and wiki content lives in Blob Storage (which authenticates via account key or managed identity, not Graph).
   - **`GroupMember.Read.All` is not needed** — the groups claim carries the IDs; this would only be for resolving group *display names*, and it requires admin consent. Team names can be configured in the app instead.
   - `Mail.Read` — **deferred**, only needed if/when email ingest is built.
7. Note the **Application (client) ID**, **Directory (tenant) ID**, and **client secret value**.

> Note on the groups claim: the overage threshold for a standard OIDC confidential client is ~200 groups, not the lower limit that applies to older SAML/implicit configurations. Not a practical concern outside very large tenants with heavy group sprawl.

## 2. Azure OpenAI resource (EU region)

This is the model backend for **both** individual and team tiers, and needs to satisfy your EU residency requirement. There's no AWS/Bedrock dependency in this design — one provider, one cloud, no cross-cloud setup needed.

1. Azure Portal → Create a resource → Azure OpenAI (via Microsoft Foundry).
2. **Region: select an EU region** (e.g. Sweden Central, France Central) — this is the residency-critical choice, don't default to a US region.
3. Deploy a chat model and an embedding model as separate deployments within the resource, using the **Data Zone Standard (EU)** deployment type — this keeps processing within the EU data zone (satisfying residency) while opening up model/quota choice beyond a single region. Browse what's available under that deployment type and pick the cheapest capable option for each role rather than committing to a specific model name in advance — the catalog and pricing shift over time.
4. Note the endpoint URL and API key (or set up managed identity auth via Entra ID for a more production-appropriate setup).

## 3. Azure Blob Storage (canonical content store)

This holds the actual wiki — markdown files and raw sources. Cosmos is only an index over it, so **this is the store that matters most for backup and durability**.

1. Azure Portal → Create a resource → **Storage account** (Microsoft's first-party "Storage account" service — the marketplace search surfaces several unrelated third-party listings too, ignore those).
2. **Region: same EU region** as your other resources (Sweden Central).
3. **Primary service**: Azure Blob Storage or Azure Data Lake Storage Gen 2 — not Azure Files or the other options; only blob containers are used.
4. **Performance**: Standard (Premium is for low-latency scenarios this project doesn't need, at extra cost).
5. Redundancy: LRS (locally redundant) is fine and cheapest for a POC; ZRS if you want more durability for little extra.
6. Under **Data protection** (during creation or after): enable **soft delete for blobs**, 7-day retention — cheap insurance against accidental deletion, worth having given this store is the source of truth. Skip container soft delete, blob versioning, point-in-time restore, and change feed — all redundant or unnecessary cost for a POC (the app already does its own version snapshots at the application layer).
7. Create two containers within the account, both **Private** access:
   - `wiki` — canonical page markdown and version snapshots
   - `sources` — immutable raw ingested files (PDFs, docs, extracted article text)
   - Leave encryption scope and version-level immutability at their defaults — not needed here.
8. Note the account name and access key (`BLOB_ACCOUNT_NAME`, `BLOB_ACCOUNT_KEY` in `.env`). Blob Storage stays on key-based auth in this build — see the note at the end of this step for why that's a deliberate, narrower decision than the Cosmos DB approach below, not an oversight.

> **Why Blob Storage still uses a key, but Cosmos doesn't**: both could in principle use Entra ID auth via the `llm_wiki_poc` app registration. Cosmos DB got the Entra ID treatment during this build (see step 4) because it was a small, contained code change (`app/db.py`, one file). Doing the same for Blob Storage would be a reasonable follow-up but wasn't done in this pass — track it as a deliberate deferral, not a decision that blob-storage-key is architecturally required.

## 4. Cosmos DB (derived index)

This is the **derived index** over the blob-stored markdown — not the source of truth. It holds embeddings and queryable metadata, and can be rebuilt from blob content if lost.

1. Azure Portal → Create a resource → Azure Cosmos DB → choose **Azure Cosmos DB for NoSQL** specifically (not the MongoDB-compatible API — see the note below for why).
2. **Basics tab**: region Sweden Central; **Availability Zones: Disable** (unneeded HA cost for a POC); **Capacity mode: Serverless**. The "Apply Free Tier Discount" toggle only applies to provisioned-throughput accounts, not Serverless — don't switch capacity modes just to chase it.
3. **Networking tab**: **All networks** (public endpoint) — consistent with the simple, no-VNet setup used for Blob Storage and Azure OpenAI; revisit only if this ever moves toward production.
4. **Backup Policy tab**: **Continuous (7 days)** — free, zero-configuration, and more than sufficient given Cosmos here is a disposable, rebuildable index (see storage model in `BUILD_SPEC.md`) rather than a store whose backup policy actually matters for data safety. Blob Storage's soft delete is what protects the real source of truth.
5. **Security tab**: **Key-based Authentication: Disable**; **Data Encryption: Service-managed key**. This account is Entra ID/RBAC-only — no `COSMOS_KEY` exists anywhere in this project. See the note below for what that implies for provisioning.
6. After creation, enable the **Vector Search for NoSQL API** capability (Settings → Features) **before creating any containers**.
7. **Assign the data-plane RBAC role.** Cosmos DB's data-plane roles (like "Cosmos DB Built-in Data Contributor") do **not** appear in the resource's normal Access control (IAM) blade — that only lists control-plane roles. Assign it via Azure CLI, using Cloud Shell (bash mode; PowerShell mode doesn't handle multi-line `\`-continued commands the same way) since it's not available in the portal UI:
   ```bash
   # Get the app registration's service principal object ID:
   az ad sp show --id <ENTRA_CLIENT_ID> --query id -o tsv

   # Confirm the built-in role's actual ID on your account (don't assume — verify):
   az cosmosdb sql role definition list \
     --account-name <cosmos-account-name> \
     --resource-group llm-wiki-poc-rg \
     -o table

   # Assign it (role-definition-id is Cosmos's fixed, well-known "Data Contributor" GUID):
   az cosmosdb sql role assignment create \
     --account-name <cosmos-account-name> \
     --resource-group llm-wiki-poc-rg \
     --scope "/" \
     --principal-id <output from az ad sp show> \
     --role-definition-id 00000000-0000-0000-0000-000000000002
   ```
8. **Create the database and containers by hand, via the Portal's Data Explorer** — not via `scripts/provision_cosmos.py`. This is a real Azure platform limitation, not a preference: Cosmos DB's Entra ID/RBAC data-plane auth rejects metadata operations (create/delete database or container) regardless of assigned role — only control-plane tools (Portal, CLI, ARM/IaC) can create them. In Data Explorer: **New Database** named `llmwiki` (uncheck "provision throughput" — Serverless account), then **New Container** four times, all with partition key `/partition_key`:
   - `page_index`, `version_index`, `ingest_log` — no special settings, Analytical Store off, legacy-partitioning checkbox left unchecked.
   - `embeddings` — additionally set the **Container Vector Policy**: path `/vector`, data type `float32`, distance function `cosine`, dimensions `1536` (matches `text-embedding-3-small`), index type **diskANN** (an approximate-nearest-neighbor index — trades a little recall precision for speed at scale, the right default here and what `app/db.py`'s indexing policy already assumes).
9. Note the account **URI** only — no key — into `.env` as `COSMOS_ENDPOINT`.
10. Run `uv run python scripts/provision_cosmos.py` — with containers already created by hand, this now just verifies Cosmos is reachable under the RBAC credentials and finishes creating the blob containers if needed.

> **Why NoSQL API, not MongoDB API**: Cosmos DB is one underlying engine exposed through multiple wire-protocol "APIs" (NoSQL, MongoDB, Cassandra, Gremlin, Table). NoSQL is the native one; MongoDB API is a compatibility shim for migrating *existing* MongoDB workloads — its value is portability for legacy Mongo code, which doesn't apply to a greenfield build. The native API also gets new features (like vector search) first, with no translation-layer semantic gaps. The code (`VectorDistance()` queries, `azure.cosmos` SDK) is written against NoSQL API specifically.

## 5. Container Apps (hosting)

1. Azure Portal → Create a resource → Container Apps.
2. Create a Container Apps Environment in the same EU region as your other resources.
3. **Configure scale-to-zero** (minimum replicas = 0) unless you have a reason not to. For an intermittently-used POC, leaving a minimum replica running 24/7 will likely cost more than all your storage combined.
4. You'll deploy the actual app image here once built — this step is mainly about provisioning the environment and confirming region/networking now, so it's ready.
5. Configure the app's environment variables/secrets (Blob Storage account name+key, Cosmos DB endpoint+key, Azure OpenAI endpoint+key+deployment names, Entra ID client ID / tenant ID / client secret, session secret) via Container Apps' secrets management — don't hardcode any of these into the built image.
6. After the app has a URL, go back to the app registration and add the production redirect URI.

## 6. Confirm team membership structure

Not something to set up — just verify, since the build spec assumes it:

1. In Teams, open your team's channel → check if there's a corresponding Microsoft 365 Group (usually visible via the Team's settings, or by checking if a matching SharePoint site exists at a predictable URL).
2. After signing into the app once, the app can show you the group IDs from your token's groups claim; map the relevant group ID(s) to team display names in the app's team config. This avoids needing any admin-consented Graph permission for the POC.

## 7. Cost monitoring

Given the cost-conscious POC goal:

1. Set up an Azure budget alert (Cost Management + Billing → Budgets) at a threshold you're comfortable with, so you get notified rather than discovering spend after the fact.
2. Azure OpenAI token usage will dominate the bill — storage (blob + Cosmos serverless) is marginal at this scale. The build spec already bakes in prompt/context optimization practices; the number worth watching is model spend, not storage.
3. Check Container Apps is actually scaling to zero when idle — this is the other cost that can quietly exceed expectations.

## What you do NOT need to do

- No need to provision any "model hosting" yourself beyond the deployments above — you're calling managed APIs, not standing up GPUs or model servers.
- No need to set up SharePoint/OneDrive integration infrastructure — explicitly dropped; wiki content lives in Blob Storage, and no `Sites.*` Graph permission is needed.
- No need for tenant-admin consent on anything — the groups-claim approach and `User.Read` are both user-consentable.
- No need to touch Microsoft 365 Copilot licensing for the POC — that's only relevant at migration time, and is a separate conversation with whoever manages your tenant's licensing (E3/E5 + Copilot add-on license, admin-assigned, ~24hr propagation delay after assignment).

## Order of operations summary

1. Entra ID app registration (needed before anything else, since auth threads through everything, and Cosmos DB's data-plane RBAC depends on it too) — confidential client, groups claim, localhost redirect URI
2. Azure OpenAI (EU) — endpoint, key, and both deployment names (chat + embeddings)
3. **Blob Storage — the canonical content store.** Two containers (`wiki`, `sources`), soft delete enabled, key-based auth
4. Cosmos DB — provision the account (key-based auth disabled), enable vector search, assign the data-plane RBAC role via CLI, then create the database + containers **by hand via Data Explorer** (not the provisioning script — Cosmos's Entra ID/RBAC auth can't create containers, only control-plane tools can)
5. Container Apps environment — provision, set scale-to-zero, wire up secrets
6. Hand off all connection strings/keys/IDs to drop into `.env` / Container Apps secrets (no Cosmos key — Entra ID/RBAC only)
7. After first deploy: add the production redirect URI to the app registration
