"""In-memory stand-ins for the three external systems, so the app can be
tested without Azure.

Nothing in `app/` is testable against real Blob Storage / Cosmos / Azure
OpenAI in CI, and the storage model makes the interesting behaviour
(blob-first writes, lease contention, reindex recovery) invisible to a test
that can't see both stores at once. These fakes replace `app.blob_store` and
`app.db` at the module level — the two modules the abstraction layer imports
as objects — so production code is exercised unmodified.

The fakes are deliberately faithful where behaviour is load-bearing: the
blob fake enforces leases, and the Cosmos fake honours partition-key
isolation. They are deliberately crude where it isn't: `query_items` matches
on the handful of query shapes `app.abstractions` actually issues rather
than parsing SQL.
"""

import hashlib
import json
import math
import threading
import time

import pytest

from app import blob_store, db
from app.config import Settings, get_settings
from app.db import EMBED_DIMENSIONS

DEV_TEAM_ID = "dev-team"
OTHER_TEAM_ID = "other-team"


# --------------------------------------------------------------- blob storage


class LeaseHeld(Exception):
    """Stands in for the Azure 409 LeaseAlreadyPresent."""


class FakeBlob:
    """Mirrors the `app.blob_store` surface. `wiki` and `sources` are separate
    dicts because the real module uses two containers with different rules —
    sources are write-once and must never be overwritten."""

    def __init__(self) -> None:
        self.wiki: dict[str, bytes] = {}
        self.sources: dict[str, bytes] = {}
        self.leases: dict[str, str] = {}  # path -> lease id currently held
        self.lease_attempts = 0
        self._lease_counter = 0

    # -- paths (pure; same logic as the real module) --

    def page_blob_path(self, tier: str, owner_id: str, slug: str) -> str:
        return f"{tier}/{owner_id}/{slug}.md"

    def version_blob_path(self, tier: str, owner_id: str, slug: str, version_number: int) -> str:
        return f"{tier}/{owner_id}/{slug}/v{version_number}.md"

    # -- page bodies --

    def write_text(self, path: str, content: str, *, lease_id: str | None = None) -> None:
        held = self.leases.get(path)
        if held and held != lease_id:
            raise LeaseHeld(f"Blob {path} is leased by another writer.")
        self.wiki[path] = content.encode("utf-8")

    def read_text(self, path: str) -> str | None:
        data = self.wiki.get(path)
        return None if data is None else data.decode("utf-8")

    def list_page_paths(self, tier: str, owner_id: str) -> list[str]:
        prefix = f"{tier}/{owner_id}/"
        return sorted(
            name
            for name in self.wiki
            if name.startswith(prefix)
            and name.endswith(".md")
            and "/" not in name[len(prefix) :]
            and not name[len(prefix) :].startswith("_")
        )

    def list_paths(self, prefix: str) -> list[str]:
        return sorted(name for name in self.wiki if name.startswith(prefix))

    def delete(self, path: str) -> None:
        self.wiki.pop(path, None)

    # -- concurrency --

    def acquire_lease(self, path: str) -> str | None:
        self.lease_attempts += 1
        if path not in self.wiki:
            return None  # nothing to lease; first writer has no contention
        if path in self.leases:
            raise LeaseHeld(f"Blob {path} already leased.")
        self._lease_counter += 1
        lease_id = f"lease-{self._lease_counter}"
        self.leases[path] = lease_id
        return lease_id

    def release_lease(self, path: str, lease_id: str) -> None:
        if self.leases.get(path) == lease_id:
            del self.leases[path]

    # -- raw sources (immutable: written once, never edited or deleted) --

    def write_raw_source(self, tier: str, owner_id: str, filename: str, data: bytes) -> str:
        from app.models import new_id

        path = f"{tier}/{owner_id}/{new_id()}-{filename}"
        if path in self.sources:
            raise AssertionError(f"Raw sources are immutable; {path} already exists.")
        self.sources[path] = data
        return path

    def read_raw_source(self, path: str) -> bytes | None:
        return self.sources.get(path)

    def ensure_containers(self) -> None:
        pass


# ------------------------------------------------------------------ cosmos db


def _param(parameters: list[dict], name: str, default=None):
    for p in parameters or []:
        if p["name"] == name:
            return p["value"]
    return default


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


class FakeContainer:
    """Enough of a Cosmos container to serve the six query shapes in
    `app.abstractions`. Documents are keyed by (partition_key, id), which is
    also what enforces the tenant isolation the access-control tests rely on."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.docs: dict[tuple[str, str], dict] = {}

    def upsert_item(self, body: dict) -> dict:
        self.docs[(body["partition_key"], body["id"])] = dict(body)
        return body

    def read_item(self, item: str, partition_key: str) -> dict:
        try:
            return dict(self.docs[(partition_key, item)])
        except KeyError as e:
            from azure.cosmos.exceptions import CosmosResourceNotFoundError

            raise CosmosResourceNotFoundError(message=f"{item} not found") from e

    def delete_item(self, item: str, partition_key: str) -> None:
        self.docs.pop((partition_key, item), None)

    def query_items(self, query: str, parameters: list[dict] | None = None, **kwargs):
        parameters = parameters or []
        pk = _param(parameters, "@pk")
        rows = [dict(d) for (p, _), d in self.docs.items() if p == pk]

        if "@pid" in query:
            pid = _param(parameters, "@pid")
            rows = [r for r in rows if r.get("page_id") == pid]
        if "LOWER(c.title)" in query:
            title = _param(parameters, "@t")
            rows = [r for r in rows if r.get("title", "").lower() == title]

        if "VectorDistance" in query:
            vec = _param(parameters, "@vec")
            for r in rows:
                r["score"] = _cosine(vec, r["vector"])
            rows.sort(key=lambda r: r["score"], reverse=True)  # most similar first
            rows = [{"page_id": r["page_id"], "score": r["score"]} for r in rows]
        elif "ORDER BY c.title" in query:
            rows.sort(key=lambda r: r.get("title", ""))
        elif "ORDER BY c.version_number DESC" in query:
            rows.sort(key=lambda r: r.get("version_number", 0), reverse=True)
        elif "ORDER BY c.timestamp DESC" in query:
            rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

        limit = _param(parameters, "@k") or _param(parameters, "@n")
        if limit is not None and "TOP" in query:
            rows = rows[:limit]

        if query.strip().startswith("SELECT c.id "):
            rows = [{"id": r["id"]} for r in rows]
        return iter(rows)


class FakeCosmos:
    def __init__(self) -> None:
        self.containers: dict[str, FakeContainer] = {}

    def get_container(self, name: str) -> FakeContainer:
        return self.containers.setdefault(name, FakeContainer(name))

    def wipe(self) -> None:
        """Simulate the Cosmos-is-gone scenario reindex() exists to recover
        from. Blob is untouched, which is the whole point."""
        self.containers.clear()


# ---------------------------------------------------------------- model calls


class FakeModel:
    """Deterministic model stand-in with a call recorder, so a test can assert
    that a code path made *no* model call — which is the actual contract for
    the query no-coverage rule and for lint's mechanical checks."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.replies: list[str] = []  # queued; falls back to a canned reply
        self.json_replies: list[dict] = []
        self.default_reply = "Fake model reply."
        self.default_json: dict = {}
        self.max_concurrent = 0  # peak in-flight calls, for concurrency-cap tests
        self._in_flight = 0
        self._lock = threading.Lock()

    def queue(self, *replies: str) -> None:
        self.replies.extend(replies)

    def queue_json(self, *replies: dict) -> None:
        self.json_replies.extend(replies)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _record(self, entry: dict) -> None:
        with self._lock:
            self.calls.append(entry)

    def _enter(self) -> None:
        with self._lock:
            self._in_flight += 1
            self.max_concurrent = max(self.max_concurrent, self._in_flight)

    def _exit(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def _next_reply(self) -> str:
        return self.replies.pop(0) if self.replies else self.default_reply

    def call_model(self, prompt: str, context: str = "", **kwargs) -> str:
        self._record({"kind": "call_model", "prompt": prompt, "context": context, **kwargs})
        return self._next_reply()

    def call_model_chat(self, messages: list[dict], **kwargs) -> str:
        self._record({"kind": "call_model_chat", "messages": messages, **kwargs})
        return self._next_reply()

    def call_model_json(self, prompt: str, context: str = "", **kwargs) -> dict:
        """Tracks peak concurrency as well as recording the call — Stage 2 fans
        out across documents and the rate-limit cap needs to be assertable."""
        self._enter()
        try:
            self._record(
                {"kind": "call_model_json", "prompt": prompt, "context": context, **kwargs}
            )
            time.sleep(0.001)  # widen the window so overlap is actually observable
            with self._lock:
                return self.json_replies.pop(0) if self.json_replies else dict(self.default_json)
        finally:
            self._exit()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def embed(self, text: str) -> list[float]:
        """Lexical bag-of-words vector: tokens hash to dimensions, so texts
        sharing words score higher under cosine. Crude, but it makes retrieval
        tests assert on real relevance ordering rather than on a fixed stub."""
        self._record({"kind": "embed", "text": text})
        vector = [0.0] * EMBED_DIMENSIONS
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % EMBED_DIMENSIONS] += 1.0
        return vector


# ------------------------------------------------------------------- fixtures


@pytest.fixture
def fake_blob(monkeypatch) -> FakeBlob:
    fake = FakeBlob()
    for name in (
        "page_blob_path",
        "version_blob_path",
        "write_text",
        "read_text",
        "delete",
        "list_page_paths",
        "list_paths",
        "acquire_lease",
        "release_lease",
        "write_raw_source",
        "read_raw_source",
        "ensure_containers",
    ):
        monkeypatch.setattr(blob_store, name, getattr(fake, name))
    return fake


@pytest.fixture
def fake_cosmos(monkeypatch) -> FakeCosmos:
    fake = FakeCosmos()
    monkeypatch.setattr(db, "get_container", fake.get_container)
    return fake


@pytest.fixture
def fake_model(monkeypatch) -> FakeModel:
    from app import abstractions

    fake = FakeModel()
    monkeypatch.setattr(abstractions, "call_model", fake.call_model)
    monkeypatch.setattr(abstractions, "call_model_chat", fake.call_model_chat)
    monkeypatch.setattr(abstractions, "call_model_json", fake.call_model_json)
    monkeypatch.setattr(abstractions, "embed", fake.embed)
    monkeypatch.setattr(abstractions, "embed_many", fake.embed_many)
    return fake


@pytest.fixture
def settings(monkeypatch) -> Settings:
    """Test settings that never read the developer's real .env."""
    get_settings.cache_clear()
    monkeypatch.setattr(
        Settings,
        "model_config",
        {**Settings.model_config, "env_file": None},
    )
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv(
        "TEAM_GROUPS", json.dumps({DEV_TEAM_ID: "Dev Team", OTHER_TEAM_ID: "Other Team"})
    )
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    yield get_settings()
    get_settings.cache_clear()


class Azure:
    """Handle onto all three fakes at once, for tests that need to reach
    across stores (e.g. wipe Cosmos, assert blob survived)."""

    def __init__(self, blob: FakeBlob, cosmos: FakeCosmos, model: FakeModel, settings) -> None:
        self.blob = blob
        self.cosmos = cosmos
        self.model = model
        self.settings = settings


@pytest.fixture
def azure(fake_blob, fake_cosmos, fake_model, settings) -> Azure:
    """The usual bundle: all three externals faked, settings pinned."""
    return Azure(fake_blob, fake_cosmos, fake_model, settings)


@pytest.fixture
def client(azure):
    """Returns a factory, not a client: cross-user access-control tests need
    to act as a second identity against the same faked stores."""
    from fastapi.testclient import TestClient

    from app import auth
    from app.main import app
    from app.models import User

    def _make(user_id: str = "dev-user", teams: dict[str, str] | None = None) -> TestClient:
        user = User(
            id=user_id,
            name=user_id,
            email=f"{user_id}@example.com",
            teams={DEV_TEAM_ID: "Dev Team"} if teams is None else teams,
        )
        app.dependency_overrides[auth.current_user] = lambda: user
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()
