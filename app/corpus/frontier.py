"""Stage 6 — the predicted knowledge frontier (MVP spec).

One model call over the completed wiki predicting territory the corpus
implies but does not cover. Stored as a map-layer artifact only
(`_corpus/frontier.json`): nothing else reads it, so it never enters the
wiki, index, search, or export by construction. Rendered as visually
distinct greyed nodes on the map."""

from app.corpus import artifacts
from app.models import now_iso

FRONTIER_SYSTEM = (
    "You are looking at the complete page set of a knowledge wiki built from "
    "a document corpus. Predict the adjacent territory: topics the corpus "
    "clearly implies, borders on, or depends on but does NOT cover with a "
    "page of its own. Name genuinely missing, genuinely real territory — "
    "specific to this corpus, not generic adjacent fields. Reply with a JSON "
    'object exactly of the form {"items": [{"title": "<missing topic, Title '
    'Case>", "note": "<one line: why the corpus implies it>"}]} with 5 to 12 '
    "items. Output only the JSON object."
)


def build_frontier(tier: str, owner: str, pageset: dict, map_data: dict) -> dict:
    """Predict and cache the frontier from page titles + descriptions +
    community names. Failure degrades to an empty frontier — the map renders
    without a frontier layer rather than the pipeline failing at its last step."""
    listing = "\n".join(
        f"- {p['title']}: {p.get('description', '')}" for p in pageset.get("pages", [])
    )
    communities = ", ".join(c["name"] for c in map_data.get("communities", []))
    prompt = f"Communities: {communities}\n\nPages:\n{listing}"
    try:
        data = artifacts.call_json(prompt, system=FRONTIER_SYSTEM, max_tokens=1500)
        items = [
            {"title": str(i.get("title", "")).strip(), "note": str(i.get("note", "")).strip()}
            for i in data.get("items", [])
            if str(i.get("title", "")).strip()
        ][:12]
    except ValueError:
        items = []
    result = {"generated_at": now_iso(), "items": items}
    artifacts.write_json(artifacts.frontier_path(tier, owner), result)
    return result


def load_frontier(tier: str, owner: str) -> dict | None:
    return artifacts.read_json(artifacts.frontier_path(tier, owner))
