"""Stage 6 — the knowledge frontier.

One model call over the completed wiki, predicting territory the corpus
*implies* but does not cover: the neighbouring subjects a reader would expect
to find given what is here, and which are absent.

**Hard rule: a frontier entry never enters the wiki, the index, search, or
export. It is a map layer and nothing else.** This module therefore returns
data and never calls `write_page` — the entries live only inside the cached map
artifact, which is a non-indexed blob. That is not a stylistic preference: a
frontier entry is a *prediction*, with no source documents behind it. Letting
one become a page would put unsourced content into a wiki whose entire claim to
trustworthiness is that every page traces back to an immutable document.
"""

from app import abstractions as ab

# Cost discipline, and a longer list is less useful anyway — a frontier of
# eighty suggestions is noise.
MAX_FRONTIER_ENTRIES = 12
MAX_TITLES_SHOWN = 200

FRONTIER_SYSTEM = (
    "You are looking at the page list of a knowledge wiki built from one "
    "organisation's documents, grouped into subject areas.\n\n"
    "Identify subjects that are conspicuously MISSING: territory this material "
    "clearly implies, that a knowledgeable reader would expect to find "
    "alongside it, and that no existing page covers. Think about what sits "
    "adjacent to the strong areas, and what a body of work like this normally "
    "addresses but this one does not.\n\n"
    "Be specific and defensible. A vague suggestion that could apply to any "
    "wiki is worthless — each entry should be something a practitioner in this "
    "domain would recognise as a genuine gap. Suggest nothing that an existing "
    "page already covers under a different name. Prefer few, good entries.\n\n"
    'Reply with JSON only: {"frontier": [{"title": "...", "why": "one sentence '
    'on why this material implies it", "near": "the subject area it sits '
    'closest to"}]}'
)


def predict(page_titles: list[str], community_names: list[str]) -> tuple[list[dict], int]:
    """Returns (frontier entries, model calls). Entries are plain dicts destined
    for the map artifact — deliberately not `Page` objects, so there is no path
    from here into the wiki."""
    if not page_titles:
        return [], 0

    context = "Subject areas:\n" + "\n".join(f"- {n}" for n in community_names)
    context += "\n\nPages:\n" + "\n".join(f"- {t}" for t in page_titles[:MAX_TITLES_SHOWN])

    payload = ab.call_model_json(
        f"What is missing from this wiki? At most {MAX_FRONTIER_ENTRIES} entries.",
        context=context,
        system=FRONTIER_SYSTEM,
    )

    existing = {t.lower() for t in page_titles}
    entries: list[dict] = []
    for item in payload.get("frontier") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        # A "gap" that already exists is a retrieval failure, not a prediction.
        if not title or title.lower() in existing:
            continue
        entries.append(
            {
                "title": title,
                "why": str(item.get("why", "")).strip(),
                "near": str(item.get("near", "")).strip(),
            }
        )
        if len(entries) >= MAX_FRONTIER_ENTRIES:
            break
    return entries, 1
