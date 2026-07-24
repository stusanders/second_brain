"""Measure the concept-similarity distribution from a completed pipeline run,
to set consolidate.DEDUP_SIMILARITY_THRESHOLD from data rather than by guess.

    uv run python scripts/measure_similarity.py --tier individual --owner <id> [--run <id>]

Re-embeds the run's Stage 2 concepts (a handful of embedding calls, no chat
calls, so cents not dollars) and reports the pairwise-cosine distribution plus,
for a range of candidate thresholds, how many concepts would land in a cluster
where the model can judge them. It also prints the strongest cross-concept
pairs so you can eyeball where "genuinely the same subject" actually sits for
this corpus and embedding model.

Motivation: the threshold was first set at 0.82, then 0.72, both guesses. The
real distribution (UK cancer-policy corpus, text-embedding-3-small) had a
median of 0.35 and genuine matches up at 0.85-0.95 — nothing like the guesses.
"""

import argparse

import numpy as np

from app import abstractions as ab
from app import blob_store
from app.corpus import consolidate, runner


def _resolve_run(tier: str, owner: str, run: str | None) -> str:
    if run:
        return run
    rid = runner.latest_run_id(tier, owner)
    if not rid:
        raise SystemExit(f"No pipeline runs found for {tier}/{owner}.")
    return rid


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tier", required=True, choices=["individual", "team"])
    ap.add_argument("--owner", required=True)
    ap.add_argument("--run", help="run id; defaults to the most recent")
    args = ap.parse_args()

    run_id = _resolve_run(args.tier, args.owner, args.run)
    # A run id may be a prefix; resolve it against the stored artifacts.
    prefix = f"{args.tier}/{args.owner}/_pipeline/"
    matches = {
        p.split("/")[3] for p in blob_store.list_paths(prefix) if p.split("/")[3].startswith(run_id)
    }
    if not matches:
        raise SystemExit(f"No run matching {run_id!r}.")
    run_id = sorted(matches)[0]

    artifact = runner.load_artifact(args.tier, args.owner, run_id, "concepts")
    if not artifact:
        raise SystemExit("That run has no concepts artifact (did Stage 2 finish?).")
    concepts = [
        (x["name"], x.get("description", "")) for d in artifact["documents"] for x in d["concepts"]
    ]
    print(f"run {run_id[:8]} — embedding {len(concepts)} concepts…")
    vectors = ab.embed_many([f"{n}\n{d}" for n, d in concepts])

    matrix = np.array(vectors, dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    sim = matrix @ matrix.T
    off = sim[np.triu_indices(len(matrix), k=1)]

    print()
    print(
        f"pairwise cosine: median={np.median(off):.3f} "
        f"p90={np.percentile(off, 90):.3f} p99={np.percentile(off, 99):.3f} max={off.max():.3f}"
    )
    print()
    print(f"{'threshold':>9}  {'pairs':>6}  {'concepts clustered':>18}")
    for t in (0.60, 0.65, 0.68, 0.70, 0.72, 0.75, 0.80, 0.85):
        adj = sim >= t
        np.fill_diagonal(adj, False)
        clustered = int(adj.any(axis=1).sum())
        marker = "  <- current" if abs(t - consolidate.DEDUP_SIMILARITY_THRESHOLD) < 1e-9 else ""
        print(f"{t:>9.2f}  {int((off >= t).sum()):>6}  {clustered:>7}/{len(matrix)}{marker}")

    print()
    print("strongest cross-concept pairs (are these the same subject?):")
    order = np.dstack(np.unravel_index(np.argsort(-sim, axis=None), sim.shape))[0]
    shown = 0
    for i, j in order:
        if i >= j or sim[i, j] > 0.97:  # skip self and near-identical
            continue
        print(f"  {sim[i, j]:.3f}  {concepts[i][0][:40]!r}  ~  {concepts[j][0][:40]!r}")
        shown += 1
        if shown >= 15:
            break


if __name__ == "__main__":
    main()
