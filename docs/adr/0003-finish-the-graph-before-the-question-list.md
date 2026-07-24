# Finish the graph before the question list

Two build orders were live: ship the elicitation question list first (it looked far cheaper,
skipping merging, chaining and the graph entirely), or build the justification graph first
and treat the question list as its payoff. We chose the graph first, because Route A's
cheapness turned out to be illusory — and because the input it consumes is the one we cannot
buy more of.

## Considered options

**Route A — question list first.** `MODEL_SPEC.md` §7 says the system's first user-facing
output is a targeted question list for a departing expert, and §11 claimed this needed only
gap-spotting within single documents.

It does not. §7's own example question — *"this inference is relied on 11 times across 4
institutions and is stated nowhere"* — requires recognising the same unstated warrant in
eleven separate places. That is identity merging, on warrants, which is the hardest matching
problem in the design. And there is no cheaper fallback, because R2 holds that a gap seen
once is an author being brisk. Strip the merging out and the output is every place every
document skipped a step: hundreds of items, unranked, indistinguishable from extraction
noise. Nobody spends an afternoon on that.

**Route B — graph first.** The gate sequence in §9 as written, with elicitation as the
payoff rather than the appetiser.

## Why

*The fork was never in the pipeline.* Both routes need the same extraction pass. Past that
they are two queries over one set of nodes — which jumps recur, and which conclusions rest on
other conclusions. Both are cheap once the nodes exist. The only real fork is which **end**
gets finished first.

*Expert time is the scarce input.* A full corpus run costs about £0.35. An afternoon with an
officer who is leaving happens once. Generating questions before the graph exists spends the
scarcest input on the weakest questions.

*The Gate 0 pilot made that concrete.* The best question it produced — why must screening
prove net benefit before being offered, while tobacco restriction needs no such balance? —
was only visible by comparing across documents *and* across decision types. Single-document
gap-spotting could not have found it. See `docs/GATE0_PILOT.md`.

## Consequences

`MODEL_SPEC.md` §6's rule that *identity merging is optional, only chaining is load-bearing*
survives — but only because Route B's end ships first. That rule is Route B's view of the
world. Route A inverts it: identity merging is the entire product and chaining is decoration.

**If this decision is ever reversed, §6 must be rewritten before any code is written.** The
two routes need opposite halves of the merging design, and the spec currently backs one of
them.
