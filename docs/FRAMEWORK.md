# Framework — what we are building and why

This is the "why" document. It sets out the problem, what would count as solving it, and
how to tell whether we have.

It deliberately contains no design and no implementation. `MODEL_SPEC.md` is the "how",
and it is answerable to this document: every part of the design should serve something
named here, and anything that serves nothing named here does not belong in the build.

Read this one first, and read it again when the design starts feeling clever.

## The problem

A new person joining a complex policy area takes about three years to become useful.

Almost none of that is learning facts. The facts are in the briefs and you can read them
in a week. What takes three years is everything around the facts:

- which numbers carry weight and which are decoration
- which constraints are statutory and which are habit that nobody has questioned
- what argument a reviewer will accept, and what will come back marked up
- why a decision was made the way it was, when the obvious alternative looks better on
  paper

None of that is written down. It is written down nowhere, by anyone, for a simple reason:
everyone in the field already knows it, so putting it on paper would be pedantic. It lives
in the heads of people who eventually leave, and it leaves with them.

So the thing that would help most is precisely the thing the documents do not contain.
That is the problem. Any system that gets very good at summarising what the documents say
has, by definition, missed it.

## What would count as solving it

The word for what an experienced officer has and a new one lacks is a **mental model**. We
mean something specific by that, not "a good understanding".

A mental model is a small internal machine you can run. You put a situation into it that
nobody has prepared an answer for, and it tells you what would happen — or here, what the
field would conclude and why. That is what separates knowing a subject from having read
about it.

**The test, and the only one that matters:**

> Can a reader use this to predict what the field would conclude about a case that is not
> in the corpus?

Everything below is in service of that sentence. It is worth being blunt about how the
current build scores against it: the ~270 pages it produces are accurate, well-sourced,
and support no novel inference whatsoever. Reading all of them tells you what thirteen
documents said. It does not let you predict the fourteenth.

That is not a quality problem that better writing would fix. A perfectly written,
perfectly deduplicated, sensibly organised 270-page tree fails the same test in the same
way. Something structurally different has to be produced.

## The five parts

A model that runs is made of five things. Each has a test, because a part you cannot test
is a part you cannot tell you have failed to build.

### 1. Chunks

A handful of top-level units — few enough to hold in your head at once, and the ones an
expert would actually use.

Expertise is not more facts, it is fewer and bigger units. A novice sees twenty things; an
expert sees three, each containing seven. The top level of the model has to be at the
expert's grain, not the novice's, or it is a table of contents.

There is a trap here worth naming, because we have already fallen into it. Novices sort a
field by what things look like; experts sort it by what governs them. Grouping pages by
which topic they resemble, or by which document they came from, produces the novice sort
every time — and it looks completely reasonable until you notice that no expert would ever
carve it that way.

> **Test:** a reader can recite the top level after one pass.

### 2. Organising principles

The reasons that keep turning up underneath decisions. The thing that, once you see it,
makes a set of otherwise arbitrary-looking positions click into place.

These are the highest-value content in the model and the hardest to get, because they are
the part nobody writes down. They are also transformative in a specific way: once you have
one you cannot un-have it, and everything you read afterwards looks different.

> **Test:** each principle explains at least three decisions, drawn from at least two
> different sources. One decision is an anecdote; three across two sources is a principle.

### 3. Surprises

The places where the field does the opposite of what a sensible, well-read outsider would
guess.

This is where learning actually happens. People arrive with a model already — usually a
wrong one — and the useful moments are the ones where their existing model fails and has
to be replaced. Content that confirms what someone already assumed teaches nothing, however
accurate it is.

Note the structure of this: a surprise is not a property of the documents. The documents
are not surprised — they are written by people who already know. A surprise only exists
relative to an expectation, which means producing this layer requires getting an
expectation from somewhere outside the corpus.

> **Test:** a newcomer would have confidently predicted the opposite.

### 4. Magnitudes

The quantities the reasoning actually turns on, attached to the decisions that turn on
them.

Not every number in the documents — most of them are context. The ones that matter are the
ones where, if the number were different, the answer would be different. Knowing which
numbers those are is a large part of what the three years buys you.

> **Test:** present wherever a principle involves a trade-off. A principle that weighs one
> thing against another and shows no quantities has not been captured properly.

### 5. Edges

Where the model stops working. Three kinds: cases the principles do not explain, points
where the field genuinely disagrees with itself, and things nobody knows.

A model without edges is not a stronger model, it is an overconfident one. Someone
navigating a field needs to know where the map runs out, or they will keep walking. This
also disciplines everything above it: a principle presented with no failing cases has not
been tested, only asserted.

> **Test:** every principle shows cases it fails to explain.

## What "grounded" means here

This has caused more confusion than anything else in the design, so it is worth pinning
down.

"Every page cites a source passage" is **traceability**. It is a property of a document,
it is worth having, and it is not what makes a model trustworthy. A model is not a
document, and it is not made grounded by containing no inference — a model that contains no
inference does not run, and a thing that does not run is an index.

A model is grounded when four things are true:

1. **It says where it does not apply.** Scope, jurisdiction, date, population. A claim
   with no stated boundary will collide with other claims for no reason and take the
   artifact's credibility down with it.
2. **It is falsifiable.** There is something that would show it wrong, and that something
   is stated rather than left implicit.
3. **Its own claims are separable from its sources'.** A reader can always tell what was
   read off the page from what was inferred, and by what.
4. **It carries provenance.** Every element points back at where it came from — including,
   where the answer is "nowhere", saying so.

The failure mode this guards against is specific and we have already hit it: "grounded"
quietly degraded into "traceable", which forced everything inferential out of the design,
which left an index, which then needed a special layer bolted on top to put the
understanding back. Getting the definition right up front is what stops that loop.

A model is grounded not by refusing to infer, but by making its inferences visible and
easy to knock down.

## Why this solves the problem

Line the five parts up against the tacit knowledge the three years buys, and they map
almost exactly:

| What takes three years to learn | Which part carries it |
|---|---|
| How the field is actually divided up, as opposed to how its documents are filed | Chunks |
| Why decisions went the way they did | Organising principles |
| The things that trip up everyone who is new | Surprises |
| Which numbers carry weight | Magnitudes |
| Where the rules stop applying, and what is genuinely contested | Edges |

That is not a coincidence — the five were derived from asking what an expert has that a
newcomer does not. But it is worth checking, because it is the argument that this framework
addresses the actual problem rather than an adjacent, more tractable one.

The last row also explains why the sequencing matters. Surprises and edges are what make a
model *usable under pressure*: they tell a new officer when to stop trusting themselves and
go and ask. That is worth more on day one than a complete map.

## The ceiling

Two limits are structural. Neither is a reason not to build this, and both must be stated
in anything the system publishes.

**Documents are written by whoever won the argument.** Reconstructing a field's reasoning
from its own output will produce a coherent, confident rationale for current policy,
because that is what the documents were written to produce. What this builds is therefore a
model of *how the field justifies itself*, which overlaps with but is not the same as how
it decides. Some decisions were political, or contingent, or a minister said no. No
document will ever say so.

This has a direct consequence for the design: the only route to the part documents cannot
hold is asking a person. That makes eliciting answers from the outgoing expert a core
component rather than a nice extra.

**Recurring patterns may be reasoning or may be house style.** If the same move appears in
twenty documents, that might be how the field thinks, or it might be how the field packages
decisions for ministers. From documents alone the two are indistinguishable. This is
tolerable for onboarding — a new officer needs to know the house style anyway — and is not
tolerable for anything published as a finding about the field. Findings must not overclaim
here.

---

## Appendix — where this comes from

Included so a sceptical reader can check the foundations, and so it is clear which parts
are borrowed and which are ours.

**Borrowed.**

- *A mental model is defined by being runnable.* Craik (1943), formalised by Johnson-Laird
  (1983). This is where the prediction test comes from, and it is the load-bearing idea in
  the whole framework.
- *Expertise is fewer, larger units.* Chase and Simon (1973), on chess. The source of the
  constraint that the top level must be small enough to hold at once.
- *Novices sort by surface features, experts sort by governing principle.* Chi, Feltovich
  and Glaser (1981), on physics problems. This is the finding that condemns topic-shaped
  and provenance-shaped carves, and it is the most directly damaging result for what the
  current build produces.
- *Threshold concepts.* Meyer and Land (2003): a few ideas per field that are
  transformative, integrative, irreversible, and characteristically troublesome. The
  organising-principles layer is an attempt to find these; "troublesome" is why the
  surprises layer is the diagnostic for them.
- *People arrive with wrong models, and learning is replacement rather than accumulation.*
  Carey (1985), Vosniadou (1994). The reason surprises are weighted so heavily.
- *Toulmin (1958)* supplies the vocabulary for taking an argument apart — claim, grounds,
  warrant, qualifier, rebuttal — and the reason not to use formal logic for it. See
  `MODEL_SPEC.md`, which uses it directly.

**Ours, and standing on their own.**

- Chaining arguments across a corpus into a structure you can walk. Toulmin analysed one
  argument at a time and gave no rules for composition. This is an extrapolation and it is
  the least certain part of the design.
- Treating recurrence across many uncoordinated authors as evidence about a field, rather
  than about a document.
- Reading an empty slot — a step that should be there and is not — as a finding in its own
  right, rather than as a gap in extraction.
- Deriving surprises by comparing the corpus against a no-corpus baseline. This is not from
  any of the above; it follows from noticing that a surprise cannot be read off a document
  because the document is not surprised.

**Considered and rejected.** Hunter's work on decoding enthymemes by abduction. It requires
the shared background knowledge as an *input*, and that background knowledge is exactly the
artifact we are trying to build. It is also monotonic, which contradicts the defeasibility
the rest of this rests on.
