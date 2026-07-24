# Model Spec — how the five parts get built

Status: **draft, nothing here is built.** Written to be argued with.

This is the "how". It is answerable to `FRAMEWORK.md`, which sets out the problem and what
would count as solving it. Every section below names the framework part it serves. A
section that serves nothing named there should be deleted rather than defended.

It supersedes the map and structure layers of `MVP_SPEC.md`. The wiki itself — ingest,
pages, provenance, version history, search — survives unchanged and underneath.

## 1. The object

A directed graph of **argument nodes** joined by **justification edges**.

An argument node is one piece of reasoning taken from one passage. An edge runs from a node
to whatever supplies its grounds. One node's claim becomes another node's evidence, and
chains terminate in one of three places:

- **Evidence** — a trial, a dataset, a statistic
- **Authority** — statute, regulation, a formal recommendation
- **Nothing** — no stated basis anywhere in the corpus

The third case is the highest-value output the system produces, and §5 R1 governs what may
be done with it.

The wiki stays underneath as the lookup and provenance layer. It answers "what is the UK
NSC". The graph answers "why does this work this way", which is the question a new starter
actually asks and the one the wiki structurally cannot handle — a page is a topic, and a
topic cannot be true, false, or predictive.

## 2. How each of the five parts is produced

The load-bearing section. Three of the five need no extraction of their own, which makes
this design considerably smaller than it first appears.

### Organising principles — one new extraction pass

Extract arguments per §4. The **warrant** — the connecting step between evidence and
conclusion, usually unstated — is the target. A warrant that recurs across enough
uncoordinated sources (§5 R2) is an organising principle.

This is the only genuinely new extraction in the design.

### Chunks — derived from principles, not extracted

Do not carve the space and then fill it. If experts sort a field by what governs it, then
the top-level units *are* the territories of the recurring principles: each principle,
plus the decisions it explains, is a chunk. The count comes out wherever it comes out, but
if it lands above about nine the principles have been split too finely and should be
merged before anything is rendered.

This also explains why the current carve is bad, and it is not a tuning problem.
`app/knowledge_map.py` groups pages by link density; pages link densely when they came from
the same source document; so the groups track provenance. Filing by which document
something came from is the textbook novice sort — see `FRAMEWORK.md` §"Chunks".

### Magnitudes — a filter over grounds, not a pass

Of the evidence supporting claims, keep the quantities, each attached to the decision that
turns on it. Cheap, because the grounds have already been extracted.

The filter to apply is counterfactual, not lexical: keep a number if a different value
would give a different answer. Numbers that appear as context are not magnitudes however
prominent they are.

### Edges — mostly already available

- *Where a principle stops applying* — the `defeaters` and `scope` fields, from the same
  extraction.
- *Genuine disagreement* — the contradiction detector in `app/lint.py`. It needs the fixes
  in §6 before it can be trusted at this job, but it exists and works.
- *Unknowns* — the recorded gaps from R1.

No new extraction.

### Surprises — a comparison, not an extraction

The only part needing genuinely new machinery, and it does not come from reading documents.
A surprise exists only relative to an expectation, and documents are not surprised.

So the expectation has to come from outside the corpus:

1. For each significant claim the corpus makes, ask the model what it expects the position
   to be, **with no corpus in context**. A frontier model's prior is a serviceable stand-in
   for a well-read newcomer.
2. Compare against what the corpus actually says.
3. Keep the mismatches. Those are the surprises, and each carries both the expectation and
   the actual position, because the gap between them is the content.

This has a second use worth as much as the first. If nothing ever comes back as a
mismatch, the model already knew everything the corpus says, and the entire system is an
expensive way of restating common knowledge. That is a result we want early and cheaply,
and it is the only defence against building something whose apparent insight is just
pretraining wearing a citation.

## 3. Storage

No new Azure infrastructure. Argument nodes, edges and derived layers persist as
`_`-prefixed blob artifacts per scope, following the precedent already set twice:
`app/lint.py` keeps its finding queue in a per-scope JSON blob, and `app/corpus/runner.py`
writes stage artifacts under `{tier}/{owner}/_pipeline/{run_id}/`. `blob_store.list_page_paths`
already excludes `_`-prefixed names, so nothing here can be mistaken for wiki content.

The reason is not just consistency. A new Cosmos container has to be created **by hand in
the Azure Portal** — Cosmos's Entra ID/RBAC data-plane auth rejects container creation
outright, whatever role is assigned (see `docs/AZURE_SETUP_GUIDE.md` §4). Every container
is a manual step in a runbook, so the bar for adding one is high.

The one thing that would force it is wanting vector search over claims, which needs a
container with a vector policy. **Default: no.** Revisit only if merging (§6) demonstrably
needs it and lexical plus embedding-in-memory has been tried first.

## 4. Extraction schema

*Serves: organising principles, magnitudes, edges.*

Toulmin's roles, collapsed. Annotators cannot reliably separate data from warrant from
backing — agreement on that distinction is poor throughout the argument-mining literature —
and keeping a distinction the extractor cannot apply consistently costs defensibility and
buys nothing.

| Field | Content |
|---|---|
| `claim` | what is asserted or recommended |
| `grounds` | the evidence or prior claim offered for it |
| `warrant` | the connecting step, usually unstated |
| `warrant_status` | `stated` / `unstated` |
| `qualifier` | force: must, should, may, presumably, in most cases |
| `defeaters` | stated conditions under which the claim lapses |
| `scope` | jurisdiction, date, population, exceptions |
| `provenance` | document, passage, author, date |
| `basis` | `evidence` / `authority` / `none_found` |

**`warrant_status` has two values, not three.** An earlier draft had `absent` alongside
`unstated`, distinguishing "the author left the step out" from "there is no step". Those
are indistinguishable in text — both are a gap on the page — so an extractor cannot apply
the distinction, and `basis: none_found` already carries the part that matters.

**`scope` is not optional metadata.** "NICE's threshold is £20–30k per QALY" is true only
with a jurisdiction and a date attached. Claims without scope contradict each other
spuriously, and a contradiction detector that cries wolf takes the whole artifact's
credibility with it.

**`qualifier` carries real weight and must never be normalised.** *Recommends*, *directs*
and *advises* are nearly identical to an embedding model and sharply distinct in law. The
distinctions between them are a large part of the expertise being captured. See §6 — this
is not a rule that enforces itself.

## 5. Operating rules

**R1 — Characterise gaps, do not fill them.**
Where the warrant is unstated, record that a step is missing, record the surrounding
context, and stop. Do not propose the missing premise.

Two reasons. A model asked to fill a gap always succeeds, fluently, whether or not any
reasoning ever existed there — and some decisions were political, contingent, or a minister
refusing. Filling those in manufactures a justification that never existed, in convincing
departmental prose, which in government is worse than having no system. Second: argument
structure is not fully determined by text and expert readers disagree about it. Output a
proposition and disagreement is fatal; output a question and disagreement is survivable,
because the expert arbitrates and their answer becomes the artifact.

Recorded as ADR 0002.

**R2 — Recurrence licenses inference; single instances do not. Count institutions, not
documents.**
A pattern appearing once is an author being brisk. The same pattern across sources with no
common author is evidence about the field.

The original form of this rule counted documents, and that is too weak for the corpus we
have. DHSC, NHSE and NICE share staff, share templates, and sometimes share paragraphs
verbatim; eleven appearances across six of their documents may be one decision, copied.
The threshold must be on distinct institutions, and the count travels with any finding
derived from it.

Note the honest limit: even across institutions, recurrence may be house style rather than
reasoning (`FRAMEWORK.md` §"The ceiling"). Findings must not overclaim.

**R3 — Every node declares its status.** `stated`, `inferred`, `elicited` (with the named
person and date), or `absent_and_noted`. All four are defensible; `unmarked` is not. This
is what makes the third grounding condition in `FRAMEWORK.md` real rather than aspirational.

**R4 — No node without provenance, except where absence is the point.** A recorded gap
cites the passages where the step was skipped.

**R5 — Contradiction edges only within compatible types.** A factual claim, a
recommendation, and a performative ("NICE recommends X") answer to different standards.
Running conflict detection across the types generates noise indefinitely.

## 6. Merging — the hard problem

*Serves: everything. Nothing else works if this does not.*

The graph exists only if claims from different documents can be recognised as the same
claim. Get it wrong and the output is a heap of disconnected fragments — worse than the
wiki, not better. This is the failure that killed the previous version at the concept
level, moved up one layer, and it is not obviously easier here.

**The merge key must be structural, not similarity.** Match exactly on `qualifier` and
`scope`; use embedding similarity only over `claim` text.

This needs writing down because the obvious implementation violates §4 by default.
`app/corpus/consolidate.py` merges by cosine over `text-embedding-3-small` at a 0.68
threshold, and that machinery is right there and reusable — but it will score *recommends*
/ *directs* / *advises* at around 0.95 and flatten precisely the distinction §4 says must
never be flattened. Reusing the clustering is fine. Reusing it as the merge decision is not.

**Two operations, not one.** Identity merging (two documents assert the same claim) and
chaining (one node's claim is another's grounds) are different, and only chaining is
load-bearing. Identity merging is optional — duplicate nodes carrying two provenance
records are perfectly usable. Gate 0 in §9 measures the right one.

**Fix the contradiction detector before relying on it for edges.** `app/lint.py:331`
shortlists candidate pairs by vector adjacency at `top_k=3`, so only semantically similar
pages are ever compared — and the prompt asks only whether they state "incompatible facts".
Genuine disagreement in a policy corpus is frequently between passages that are not
adjacent and are not incompatible as facts. Scope-aware comparison (§4) and a wider
shortlist are both prerequisites.

## 7. Elicitation

*Serves: organising principles, edges — the parts documents structurally cannot hold.*

Text cannot supply what was never written. A 2018 proposal that died leaves a submission
recommending it and nothing at all recording why it failed, because the reason travelled by
meeting and corridor.

So the system's first user-facing output is a targeted question list:

> This inference is relied on 11 times across 4 institutions and is stated nowhere. What is
> the warrant?

> This constraint is universally respected and has no statutory basis. Who decided it, and
> why?

That turns elicitation from unbounded interviewing into something an outgoing officer can
finish in an afternoon. Answers are written back into the graph marked `elicited` with the
person named (R3).

This also closes the only validation loop available. The corpus generated the
reconstruction, so the corpus cannot check it. The expert is the tribunal.

## 8. Later — the pattern library

*Serves: chunks, organising principles. Ships last; do not read this spec as promising it
early.*

Once enough arguments are in the graph, the recurring *shapes* of the edges are worth
naming: argument from cost-effectiveness threshold, argument from comparable jurisdiction,
argument from delivery feasibility — each with the defeat conditions that recur alongside
it.

Walton's published schemes seed the classifier; they do not define the vocabulary. The
moves that carry weight in health policy are not in his list, and his taxonomy is contested
among its own authors.

## 9. Gates

Each gate can stop the build. That is the point of having them.

**Gate 0 — manual pilot. No code.**
Four documents known to overlap. Extract argument nodes from each by hand. Then two tallies,
kept separate:

1. **How often does a document jump?** Every place it goes from a fact to a recommendation
   without stating the connecting step. If these are numerous and interesting — the kind of
   thing you would want to ask a departing colleague — the elicitation route (§7) is real.
2. **Does anything chain?** For each conclusion, is its support itself something argued
   elsewhere in the set, or is it just a number? Report the distribution of **chain length**.

The second tally is the one that can kill the design, and it replaces an earlier version of
this gate that counted how many nodes merged. Merging duplicates is not the point; depth
is, because "walk backwards from a decision to its evidence" needs somewhere to walk. Sixty
nodes merging into forty-five with every chain one step deep passes a merge-count gate and
has produced a list.

If support is nearly always a statistic, there is no network. That is a real possibility —
policy documents cite evidence, they do not cite each other's reasoning — and it should
surface here for the cost of an afternoon rather than at Gate 2 for the cost of a build.
What survives in that case is still worth something: a claim-to-evidence structure showing
which single dataset forty decisions are leaning on is itself an audit finding. But §1's
pitch would need rewriting, and traversal would buy little.

**Gate 1 — extraction quality.** Automated extraction over the same four documents, against
the hand-built set. Score `claim` and `grounds` separately from `warrant`; warrant recovery
will be worse and needs its own baseline rather than dragging down one blended number.

**Gate 2 — merging at scale.** Thirty documents. Report node degree and chain length.
A median chain length of 1 means fragments, not a graph.

**Gate 3 — gap detection.** Produce the question list. Put it to one domain expert. The
measure is whether the questions were worth their time.

**Gate 4 — surprises.** Run the no-corpus baseline comparison. If nothing comes back, stop
and reconsider — see §2.

**Gate 5 — graph build and traversal UI.**

**Gate 6 — pattern induction.**

Full-corpus runs cost roughly £0.35 on the existing deployment. Running is cheaper than
arguing; prefer measurement wherever a question is measurable.

## 10. Success criteria

Ordered to match the build, so that at every stage there is a live measure rather than one
that cannot be run yet.

**Through Gates 0–3 — expert challenge.** Put reconstructions and questions to a domain
expert. Record confirm / correct / "no, the real reason is X". Corrections are worth more
than confirmations; track the ratio over time. This is the only criterion available early,
and it does all the work until Gate 6.

**From Gate 4 — the no-corpus baseline.** Does the artifact contain anything a good model
did not already know? A hard pass/fail on whether the corpus is contributing at all.

**From Gate 6 — held-out documents.** Hold documents out of the build. For each, predict
which reasoning moves it will use, and score it. When a document uses a move the graph does
not contain, the structure has to change, and that restructuring event is the system's real
output.

This is the criterion that most directly implements the framework's prediction test, and it
is deliberately not listed first: predicting *reasoning moves* needs the pattern vocabulary
from §8, which ships last. Predicting *claims* is the wrong grain — new briefs say new
things, so claim prediction fails uninformatively.

**Throughout — onboarding.** A new starter answers "why does the UK not screen for prostate
cancer" by traversal, faster and more completely than by reading the briefs.

## 11. The open question — build order

**This is the decision the grill session has to settle, and it comes before Gate 0, because
it determines what Gate 0 is for.**

§7 asserts that the first output is a question list, not a map. If that is true, the build
order in §9 is wrong, and most of it is not on the critical path.

**Route A — question-list first.** Ship the elicitation questions. Needs only gap-spotting
within single documents: no merging, no chaining, no graph. Skips Gates 1, 2 and 5
entirely. It is the smaller of the two ideas, it does not depend on the graph working, and
if it produces poor questions that is obvious immediately without needing a metric to tell
you. Risk: it is a tool for the departing expert, not a model for the arriving one, and it
may not be enough on its own to be worth anyone's budget.

**Route B — graph first.** §9 as written. Everything downstream stays as specced, and the
artifact at the end is the thing the framework describes. Risk: the two hardest gates are
on the critical path, and Gate 0 might kill the whole thing.

Both routes need Gate 0. They need different things measured by it — Route A cares about
tally 1, Route B about tally 2 — which is why this cannot be deferred past it.

An earlier draft framed this as a question about users ("is a question list acceptable on
day one?"). It is not. It decides whether half the build sequence is necessary.

## 12. Non-goals and known blind spots

- **Dead options are unrecoverable.** Documents are written by the surviving proposal. The
  system may detect the scar — an option conspicuously unaddressed — but not what was in it.
- **Confirmation bias at corpus scale.** A structure reconstructed from documents written
  by the decision-makers will produce a coherent rationale for current policy. The design
  needs something that hunts the absent counter-argument and currently **has nothing**.
  This is the largest unresolved weakness in the design and is recorded here rather than
  solved.
- **Reasoning versus house style.** See R2 and `FRAMEWORK.md` §"The ceiling". Tolerable for
  onboarding, not tolerable for published findings.
- **Not an evaluator.** The system maps what is relied on and where it bottoms out. It does
  not judge whether the arguments are any good.
- **The wiki is not replaced.** Lookup stays the wiki's job.

## 13. Open questions for the grill

1. **Build order** — §11. Everything else is downstream of this.
2. Does a node merge on `claim` alone, or on claim plus scope? Claim alone collapses
   jurisdictions; both together may prevent any merging at all.
3. What recurrence threshold, across how many institutions, licenses a finding? Arbitrary
   until Gate 2 supplies a distribution.
4. Who owns a correction when one expert disagrees with another expert's elicited answer?
5. Is a claim-to-evidence structure with no chaining (the Gate 0 failure case in §9) worth
   shipping on its own?
