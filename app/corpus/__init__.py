"""The corpus pipeline (docs/MVP_SPEC.md): documents in, wiki + map + frontier
out, in one pass.

Six stages, in an order that matters — the page set must be fixed before any
page is written, or Stage 4 invents [[links]] to pages that don't exist:

    1. Ingest        existing app.ingest.extractors + blob raw-source store
    2. Concepts      concepts.py   — one call per document, with passages
    3. Consolidate   consolidate.py — hierarchical merge + embedding dedup
    4. Synthesis     synthesis.py  — one call per page, over all its passages
    5. Map           existing app.knowledge_map (link graph -> Louvain -> names)
    6. Frontier      frontier.py, plus contents regeneration and a lint pass

Orchestrated by runner.py, which persists a per-stage artifact so the output
can be inspected stage by stage. That inspection is the point: the MVP exists
to answer whether this synthesis is any good, and the decisive stage (3) is
one whose output you have to read directly to judge.
"""
