"""YAML frontmatter on canonical page blobs.

Cosmos is a *derived* index — the build spec says it can be wiped and
rebuilt from blob. That promise only holds if blob carries enough to rebuild
with, and until now it didn't: a page's markdown body has no id, no version,
and crucially no source refs, so `reindex()` could not restore provenance
("where did this claim come from" must always be answerable) or stable page
identity. Frontmatter is what closes that gap, and the build spec's storage
discipline rule explicitly allows it: "YAML frontmatter for genuinely
document-level metadata (title, tags, date, source refs) is acceptable — it's
a markdown-native convention."

Handled strictly at the blob boundary in `app.abstractions` (serialized on
write, stripped on read), so `Page.body` stays frontmatter-free everywhere in
the app — diffs, rendering, embeddings and changesets are untouched.

Parsing is hand-rolled over the few scalar-and-string-list fields below
rather than taking a YAML dependency, following the precedent already set by
`app.schema`'s regex frontmatter read. Values are read verbatim to end of
line (split on the first colon only), so titles containing colons need no
quoting.
"""

DELIMITER = "---"
LIST_FIELDS = frozenset({"source_refs"})


def serialize(meta: dict, body: str) -> str:
    """Prepend a frontmatter block to `body`. Empty/None values are omitted
    rather than written as blanks, so a page never gains meaningless keys."""
    lines = [DELIMITER]
    for key, value in meta.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.append(DELIMITER)
    return "\n".join(lines) + "\n\n" + body


def parse(raw: str) -> tuple[dict, str]:
    """Split a blob into (metadata, body).

    Tolerant by design: content with no frontmatter returns ({}, raw)
    unchanged. Pages written before this existed, and pages a user has
    hand-edited in a markdown editor, must keep working — the storage
    discipline rule promises the files are useful with no knowledge of this
    app, which cuts both ways.
    """
    if not raw.startswith(DELIMITER + "\n"):
        return {}, raw

    end = raw.find("\n" + DELIMITER, len(DELIMITER))
    if end == -1:
        return {}, raw  # unterminated block — treat the whole thing as body

    block = raw[len(DELIMITER) + 1 : end]
    rest = raw[end + len(DELIMITER) + 1 :].lstrip("\n")

    meta: dict = {}
    current_list: str | None = None
    for line in block.splitlines():
        if line.startswith("  - ") and current_list:
            meta[current_list].append(line[4:].strip())
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not value and key in LIST_FIELDS:
            meta[key] = []
            current_list = key
        else:
            meta[key] = value
            current_list = None
    return meta, rest
