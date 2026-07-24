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

The Gate 0 pilot puts a name to what the right carve looks like on this corpus: **decision
type**. Should we offer this screen; should we restrict this product; how should we organise
services; what should we prioritise. Each carries its own decision rule, its own standard of
proof and its own vocabulary, and the cancer site turns out to be almost irrelevant to which
applies — the two cervical documents have more in common with the other cancers in their
genre than with each other. None of the four is a document, which is why no amount of tuning
the community detection will find them: the signal is not in the link graph.

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

## 2a. Triage — which documents the model reads closely

*Serves: all five, by not polluting them.*

Argument density across a real corpus varies by an order of magnitude, and length is a bad
proxy for it. `app/corpus/concepts.py:39` sets a flat `TARGET_CONCEPTS_PER_DOCUMENT = 15`
adjusted only by document length; on the pilot corpus that budgets a 93,000-character
questionnaire like a 93,000-character argument.

Forcing a fixed yield of argument nodes out of a document that contains no argument does not
produce a thin result. It produces an invented one — R1's failure mode arriving through the
back door, since a model asked to find reasoning will always find some.

**Three tiers, not two.**

| Tier | To the wiki | Yields claims | Mined for warrants |
|---|---|---|---|
| **Argued** — impact assessments, a legislative case for change | yes | yes | yes |
| **Claim-only** — plans, strategies, policy summaries, consultation results | yes | yes | no |
| **Wiki-only** — questionnaires, pure process documents | yes | no | no |

The middle tier is the one that stops this being a crude filter. The 2010–2015 policy paper
records that bowel screening was extended to ages 70–75 — a dated, attributable claim that
later documents build on — and contains no reasoning whatever about why. Excluding it loses
a link in a five-year chain; mining it for warrants manufactures a rationale that was never
there. It should give up its claims and nothing else.

**Classification is two-stage, and the first stage is free.** Eleven of the thirteen pilot
documents carry the GOV.UK document type as literally the first line of extracted text —
*Impact assessment*, *Policy paper*, *Call for evidence outcome*, *Closed call for evidence*.
Deterministic, no model call.

That gives genre, not density, and the gap is real: three pilot documents are labelled
*Policy paper* and range from a dense strategy to a seven-page summary with one argued claim
in it. So stage two is a single cheap call per document returning `decision_type` and a tier,
run over the title and opening sections rather than the full text. `decision_type` is not
extra work — §5 R2 needs it to qualify recurrence counts and §6 needs it as the warrant merge
key, so one pass earns its keep three times.

**Silent exclusion is the risk, so do not let it be silent.** Dropping a document from the
model is exactly the kind of loss that is invisible in the output — the reasoning that made
the corpus path opt out of `extractors.MAX_CHARS` rather than truncate. Write the tier and
the reason for it out as a stage artifact and show them on the run page, which the README
already treats as the evaluation surface. A wrong "wiki-only" then costs one glance instead
of a lost finding.

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
| `provenance` | document, passage, author, date — where the claim was *found* |
| `asserted_by` | whose claim it is: the institution or group, plus `asserted` / `reported` |
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

**`asserted_by` is not the same as `provenance`, and conflating them corrupts R2.**
Provenance records where a claim was found. `asserted_by` records whose claim it is. On the
pilot corpus (`GATE0_PILOT.md`) those come apart three ways in documents that all say DHSC
on the cover:

- the department asserting something in its own voice
- the department reporting a conclusion that belongs to another body — "UK NSC recommended",
  "NICE guidance sets out", "as per HM Treasury Green Book"
- the department summarising what consultees said — "Results of the National Cancer Plan
  call for evidence" is 138,000 characters of what 11,918 respondents told it

Without the field, the third kind enters the graph as departmental reasoning and inflates
every recurrence count with public opinion. The second kind is where the owner of a warrant
is actually recorded, which is what R2 needs and cannot otherwise get.

The field is affordable because the text nearly always marks it explicitly — "we will", "UK
NSC concluded", "respondents told us". It is a weaker distinction than claim-versus-grounds
but a far stronger one than data-versus-warrant, which is the line §4 declines to draw.

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

**Count the institution that owns the warrant, not the one that wrote the document
(`asserted_by`, not `provenance`).** The pilot corpus is entirely DHSC-authored, so on the
document-author reading every finding in `GATE0_PILOT.md` counts as one institution and R2
licenses nothing at all. But the documents themselves attribute their warrants to four
separate bodies with separate mandates — the harm–benefit rule to UK NSC's evidence review
criteria, the cost-effectiveness threshold to NICE's HTA methodology, the equality frame to
the Equality Act's public sector equality duty, the appraisal method to HM Treasury's Green
Book. Those are the uncoordinated authors R2 is trying to find. The department is the
courier.

**Recurrence within one decision type is not recurrence across the field.** The pilot's
first pass took four screening appraisals and concluded that the harm–benefit rule was the
field's dominant warrant. Read across all thirteen documents it is nearly absent outside
screening. A recurrence count must be qualified by how many *decision types* it spans, or
it will confidently report a local convention as a principle of the field.

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

**The merge key must be structural, not similarity.** Use embedding similarity only over
text; never let it decide the match on its own.

This needs writing down because the obvious implementation violates §4 by default.
`app/corpus/consolidate.py` merges by cosine over `text-embedding-3-small` at a 0.68
threshold, and that machinery is right there and reusable — but it will score *recommends*
/ *directs* / *advises* at around 0.95 and flatten precisely the distinction §4 says must
never be flattened. Reusing the clustering is fine. Reusing it as the merge decision is not.

**Claims and warrants do not share a merge key.** An earlier draft gave one rule — match
exactly on `qualifier` and `scope` — and applied it to both. On the pilot corpus that rule
is too strict for one and vacuous for the other:

- **Claims carry heavy scope, and the scope is the content.** Cervical screening is every
  3 years for 25–49 and every 5 years for 50–64: same claim shape, different population,
  and the difference *is* the decision. Extended intervals arrived in Scotland in 2020,
  Wales in 2022 and England in 2025 — same claim, different jurisdiction and date, and
  "England was five years behind" is a fact a merge would destroy.
- **Warrants are nearly scopeless.** "Benefits must outweigh harms" carries no jurisdiction,
  date or population anywhere in thirteen documents, and warrants do not take must/should/may
  the way claims do. Demanding an exact match on two fields that are almost always empty is
  a test that always passes.

**Claims: link on scope difference, do not merge across it.** Two nodes asserting the same
claim under different `scope` stay two nodes, joined by a *same claim, different scope*
edge. The difference survives, recurrence is still countable by walking the edge, and a
jurisdictional lag becomes a visible finding instead of a casualty. This dissolves the old
open question — the choice was never claim-alone versus claim-plus-scope, it was merge
versus link.

**Warrants: match on warrant text plus decision type; ignore `scope` and `qualifier`.**
Decision type is the discriminator that scope cannot be here, and §5 R2 needs the same
lookup, so it is one mechanism serving both.

The cost is node count and query cost. Four UK nations can yield four nodes where one was
expected, and every recurrence count becomes a graph walk rather than a lookup. Accepted:
in a corpus where the jurisdictional lag is the interesting part, silently flattening it is
the worse failure.

**Three operations, not one.** Identity merging (two documents assert the same claim),
scope-linking (the same claim under different jurisdiction, date or population), and
chaining (one node's claim is another's grounds). Only chaining is load-bearing — see ADR
0003, which also records that this ordering is what makes the statement safe. Gate 0 in §9
measured the right one.

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

**Gate 0 — manual pilot. No code. → DONE, passed. Results in `GATE0_PILOT.md`.**
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

A third tally was added before the pilot ran, because neither of the two above tests what
Route A in §11 actually depends on:

3. **Does the same jump recur across documents?** Route A's output is "this is relied on
   eleven times across four institutions and stated nowhere", which needs cross-document
   recognition of the same unstated step. Route A was advertised as skipping merging; it
   does not.

**Outcome: all three tallies passed**, on four documents and again on all thirteen. Chain
length is a median of 3 and chains cross documents — the 2026 National Cancer Plan uses the
screening impact assessments' conclusions as its premises. The pilot also overturned its own
first result, corrected R2 (above), added `asserted_by` to §4, and named the carve. Read
`GATE0_PILOT.md` before Gate 1; it changes what Gate 1 should be scored against.

**Gate 1 — extraction quality.** Automated extraction over the pilot documents, scored
against the hand-built set in `GATE0_PILOT.md`. Score `claim` and `grounds` separately from
`warrant`; warrant recovery will be worse and needs its own baseline rather than dragging
down one blended number.

Add one test the earlier draft did not have, because it is the design's largest untested
assumption (§13 Q4): **can the same warrant be recognised across documents when it is worded
differently each time?** The pilot's counts were search-based and therefore only found
warrants phrased alike — the easy case. Construct the hard case deliberately: the harm–benefit
rule appears as "benefits would outweigh the harms", "more good than harm", and "maximise the
benefits they bring, while minimising harm". If extraction plus merging does not put those
three together, the organising-principles layer does not work and that must surface here, not
at Gate 2.

**Gate 2 — merging at scale.** Thirty documents. Report node degree and chain length.
A median chain length of 1 means fragments, not a graph. Also the place where §13 Q5 and Q7
get their distributions.

**Gate 3 — gap detection, and the first real expert contact.** Produce the question list from
the graph. Put it to one domain expert. The measure is whether the questions were worth their
time. Per ADR 0003 this is where the expert enters, not earlier.

**Gate 4 — surprises.** Run the no-corpus baseline comparison. If nothing comes back, stop
and reconsider — see §2.

**Gate 5 — graph build and traversal UI.**

**Gate 6 — pattern induction.**

Full-corpus runs cost roughly £0.35 on the existing deployment. Running is cheaper than
arguing; prefer measurement wherever a question is measurable.

## 10. Success criteria

Ordered to match the build, so that at every stage there is a live measure rather than one
that cannot be run yet.

**Through Gates 1–2 — agreement with the Gate 0 reference set.** `GATE0_PILOT.md` is a
hand-built reading of all thirteen documents: the two incompatible decision rules, the
unacknowledged scar, six recurrent warrants with their counts, and the chain-length
distribution. Score automated extraction against it. Cheap, repeatable, available from day
one, and it consumes no expert.

**State the limit of this plainly, because it is easy to forget once there is a number.**
This measures agreement between the machine and one careful outside reader. It does not
measure truth. The reference set was built by someone who is not a domain expert, from the
documents alone, and if that reading is wrong in a systematic way then tuning the extractor
to reproduce it will reproduce the error and nothing here will catch it. That is the
strongest argument for a short early expert contact — ten questions from the pilot, purely
to check the questions are the right *kind* — ahead of the real session at Gate 3. Worth
doing if the goodwill is available; not a blocker if it is not.

**From Gate 3 — expert challenge.** Put reconstructions and questions to a domain expert.
Record confirm / correct / "no, the real reason is X". Corrections are worth more than
confirmations; track the ratio over time.

Deliberately not earlier. Per ADR 0003, expert time is the scarce input in this whole
design — a full corpus run costs about £0.35, and an afternoon with a departing officer
happens once. Spending it on questions generated before the graph exists spends the scarcest
input on the weakest questions.

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

## 11. Build order — settled

**Settled in the grill session, after Gate 0. Recorded here with the reasoning; the two
routes are kept because the argument against Route A's advertised cheapness is the load-
bearing part.**

**Decision: build the shared pipeline, run both analyses, finish Route B's end first.**

Three things decided it.

*Route A is not the cheap option it was written up as.* §7's own example question — "this
inference is relied on 11 times across 4 institutions and is stated nowhere" — cannot be
produced without recognising the same unstated warrant in eleven places. That is identity
merging on the hardest possible field. And there is no cheaper fallback, because R5's
sibling R2 says a gap seen once is an author being brisk, so single-document gap-spotting
licenses nothing. Strip merging out and the output is every place all thirteen documents
skipped a step: hundreds of items, unranked, indistinguishable from extraction noise.

*The fork was never in the pipeline anyway.* Both routes need the same extraction, and past
that point they are two queries over one set of nodes — which jumps recur, and which
conclusions rest on other conclusions. Both are cheap once the nodes exist. The fork is in
which **end** gets finished: an elicitation loop, or a graph you can walk.

*Expert time is the scarce input; compute is not.* A full corpus run costs about £0.35. An
afternoon with an officer who is leaving is a one-off. Spending it on questions generated
before the graph exists spends the scarcest input on the weakest questions. Gate 0 made this
concrete: the best question the pilot produced — why does screening have to prove net
benefit while tobacco restriction does not? — was only visible by comparing across
documents and across decision types. Single-document gap-spotting would not have found it.

Consequence for §6: "identity merging is optional, only chaining is load-bearing" survives,
but narrowly, and only because Route B's end ships first. If that order is ever reversed,
§6 has to be rewritten before anything is built.

Recorded as ADR 0003.

---

The original framing is kept below, because the grill should be able to see what was
decided against.

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

- **Dead options are unrecoverable — but the scar is detectable, and that is now a feature
  rather than a consolation.** Documents are written by the surviving proposal. The pilot
  found a scar by hand: the 2023 Major Conditions strategy argues explicitly for moving
  "away from single disease strategies"; the 2026 National Cancer Plan is one, and does not
  mention the Major Conditions strategy anywhere. The argument for the abandoned approach
  survives in the corpus; the argument against it exists nowhere.

  The pattern is mechanical: document A argues X, a later document B does not-X, and no
  document connects them. That is a graph query over claim, `scope` (date) and contradiction
  edges — not a judgement call. Build it. It will not recover what was in the dead option,
  but naming the reversal and its date gives an expert something specific to answer, which
  is exactly the shape §7 needs.
- **Confirmation bias at corpus scale.** A structure reconstructed from documents written
  by the decision-makers will produce a coherent rationale for current policy. Scar
  detection is a partial answer and should not be oversold as a full one: it finds
  reversals *between* documents, and does nothing about an option that was killed before
  anyone wrote it down. Hunting the genuinely absent counter-argument remains unsolved and
  is still the largest known weakness in the design.
- **Reasoning versus house style.** See R2 and `FRAMEWORK.md` §"The ceiling". Tolerable for
  onboarding, not tolerable for published findings.
- **Not an evaluator.** The system maps what is relied on and where it bottoms out. It does
  not judge whether the arguments are any good.
- **The wiki is not replaced.** Lookup stays the wiki's job.

## 13. Open questions

**Settled**

1. ~~Build order~~ — settled in §11: shared pipeline, both analyses, Route B's end first.
2. ~~Is a claim-to-evidence structure with no chaining worth shipping alone?~~ — moot. Gate 0
   found chaining, including across documents and across five years.
3. ~~Should §4 record who a claim belongs to?~~ — yes, `asserted_by`. See §4.

**Still open**

4. ~~Does a node merge on `claim` alone, or on claim plus scope?~~ — dissolved in §6. The
   choice was merge versus link, not which key to merge on. Claims link across scope;
   warrants merge on text plus decision type.

   What remains untested underneath it: **can warrants worded differently be recognised as
   the same warrant at all?** Gate 0's recurrence counts were search-based, so they only
   found warrants phrased similarly — the easy case. This is the largest untested assumption
   in the design, and Gate 1 should be scored against it directly rather than leaving it to
   Gate 2.
5. What recurrence threshold licenses a finding? Still arbitrary until Gate 2 supplies a
   distribution — but the count must now be qualified by how many **decision types** it
   spans, not just how many institutions (§5 R2).
6. Who owns a correction when one expert disagrees with another expert's elicited answer?
7. **Is asymmetry a better detector than absence?** Hypothesis from Gate 0, explicitly *not*
   adopted: the most useful warrants may be the ones stated in one place and relied on
   silently elsewhere, because the single statement is what makes the silent uses
   recognisable and the finding defensible. Rests on a comparison of two categories with
   three and one members, which is not enough to decide anything. Gate 2 supplies the
   distribution that would settle it. If it holds, `warrant_status` becomes a property of
   the merged warrant (`stated_somewhere` / `stated_nowhere`) rather than of a node, and §6
   loses "identity merging is optional".
8. ~~How should extraction budget be allocated across a corpus of uneven argument density?~~
   — settled in §2a. Not a budget question: a triage question. Three tiers, classified from
   the free GOV.UK type line plus one cheap call, with the tier recorded as a reviewable
   artifact.

   Left open underneath it: **what fraction of a corpus has to be argued before this design
   is worth running at all?** On the pilot corpus roughly a third is claim-only or wiki-only.
   At two thirds the graph would be thin enough to question the premise. Gate 2's thirty
   documents give the first honest read.
