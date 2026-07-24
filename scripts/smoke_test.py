"""Verify the three external systems before spending on a corpus run.

    uv run python scripts/smoke_test.py

Two tiny chat calls, one embedding, one Cosmos read and one blob round trip —
fractions of a cent. Worth running after any deployment, credential or
API-version change: a corpus pass is hundreds of calls, and discovering a
misconfigured deployment on call three hundred is an expensive way to find out.

Checks `call_model_json` specifically, because every stage of the corpus
pipeline depends on JSON mode and reasoning-family deployments have not always
supported it on older API versions.
"""

from app import abstractions as ab
from app import blob_store, db
from app.config import get_settings

SMOKE_BLOB = "_smoke/check.txt"


def _check(label: str, fn) -> bool:
    try:
        print(f"{label:<17} OK   -> {fn()}")
        return True
    except Exception as e:  # noqa: BLE001 — this script exists to report failures
        print(f"{label:<17} FAIL -> {type(e).__name__}: {str(e)[:300]}")
        return False


def _blob_round_trip() -> str:
    blob_store.write_text(SMOKE_BLOB, "ok")
    assert blob_store.read_text(SMOKE_BLOB) == "ok"
    blob_store.delete(SMOKE_BLOB)
    return "write/read/delete round trip"


def _cosmos() -> str:
    for name in db.CONTAINERS:
        db.get_container(name).read()
    return ", ".join(db.CONTAINERS)


def main() -> None:
    s = get_settings()
    print(f"chat deployment : {s.azure_openai_chat_deployment}")
    print(f"embed deployment: {s.azure_openai_embed_deployment}")
    print(f"api version     : {s.azure_openai_api_version}")
    print(f"cosmos database : {s.cosmos_database}")
    print()

    results = [
        _check(
            "call_model",
            lambda: repr(ab.call_model("Reply with the single word: ok", max_tokens=200).strip()),
        ),
        _check(
            "call_model_json",
            lambda: ab.call_model_json(
                'Reply with {"status": "ok"}',
                system='Reply with JSON only, of the form {"status": "ok"}',
                max_tokens=200,
            ),
        ),
        _check(
            "embed_many",
            lambda: f"{len(v := ab.embed_many(['a', 'b']))} vectors of {len(v[0])} dims",
        ),
        _check("cosmos", _cosmos),
        _check("blob", _blob_round_trip),
    ]

    print()
    if all(results):
        print("All externals reachable — safe to run a corpus build.")
    else:
        raise SystemExit("One or more externals failed; fix before running a corpus build.")


if __name__ == "__main__":
    main()
