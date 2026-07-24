# LLM Wiki

A system that turns a team's document corpus into a wiki for lookup and a **model** for
understanding. The wiki is built and running; the model is designed but not built (see
`docs/FRAMEWORK.md` and `docs/MODEL_SPEC.md`).

This file is a glossary and nothing else. No design, no implementation detail. Terms have
been used loosely across several design sessions, and this exists to stop that.

## Language

### The wiki layer (built)

**Page**:
A topic in the wiki, written from source passages, addressable and linkable.
_Avoid_: node, entry, article
_Note_: a Page cannot be true or false. This is why it is a lookup layer and not a model.

**Concept**:
A subject extracted from one document, carrying the passages that evidence it. The input
to page consolidation.
_Avoid_: topic, theme, entity

**Passage**:
A verbatim extract from a source document, carrying the reference to that document.

**Community**:
A densely cross-linked group of Pages found by running Louvain over the wikilink graph.
_Avoid_: theme, cluster, group
_Note_: "theme" was used for this in earlier sessions and should not be. A Community is
a graph artifact, not a claim about how the field divides up.

**Scope**:
One tier-and-owner pair (`team/dev-team`), the unit of isolation for content and runs.
_Note_: distinct from the `scope` field on a Claim below. See Flagged ambiguities.

### The model layer (designed, not built)

**Claim**:
One thing asserted or recommended, with a truth value and a stated scope.
_Avoid_: assertion, statement, proposition

**Argument node**:
One instantiated piece of reasoning from one passage — a Claim plus its grounds, warrant,
qualifier, defeaters, scope and provenance.
_Avoid_: node (unqualified), argument, unit

**Grounds**:
The evidence or prior Claim offered in support of a Claim.
_Avoid_: basis, support, premise

**Warrant**:
The connecting step between Grounds and Claim. Usually unstated, and the primary target of
extraction.
_Avoid_: assumption, rationale, logic

**Qualifier**:
The force of a Claim — must, should, may, presumably. Legally load-bearing and never
normalised.

**Defeater**:
A stated condition under which a Claim lapses.
_Avoid_: exception, caveat, rebuttal

**Justification edge**:
A directed link from an Argument node to the node supplying its Grounds.

**Chunk**:
One of the handful of top-level units of the model. Derived from Organising principles,
never extracted directly.
_Avoid_: theme, category, section, community

**Organising principle**:
A Warrant that recurs across enough uncoordinated institutions to count as evidence about
the field rather than about one author.
_Avoid_: axiom, principle (unqualified), theme, tacit knowledge

**Surprise**:
A place where the corpus's position contradicts what a no-corpus baseline predicts.
_Avoid_: insight, finding, counterintuitive result

**Magnitude**:
A quantity a decision turns on — one where a different value would give a different answer.
_Avoid_: metric, figure, number, statistic

**Edge (of the model)**:
A limit of the model: a case the principles fail to explain, a genuine disagreement, or an
unknown.
_Avoid_: gap, boundary
_Note_: unrelated to Justification edge. See Flagged ambiguities.

**Gap**:
A place where a document moves from Grounds to Claim with no Warrant stated.
_Avoid_: hole, missing link, absence

**Elicited answer**:
A Warrant supplied by a named person in response to a Gap, rather than found in text.

## Relationships

- A **Document** yields many **Concepts**; **Concepts** consolidate into **Pages**
- A **Document** yields many **Argument nodes**; each cites one or more **Passages**
- An **Argument node** has one **Claim**, one set of **Grounds**, and at most one **Warrant**
- **Justification edges** connect **Argument nodes**; one node's **Claim** is another's **Grounds**
- A **Warrant** recurring across enough institutions becomes an **Organising principle**
- **Organising principles** determine the **Chunks** — they are not chosen independently
- A **Gap** becomes a question; a question may become an **Elicited answer**
- **Pages** sit beneath the model as the lookup and provenance layer, not as part of it

## Example dialogue

> **Dev:** "The map has six themes. Should each one become a Chunk?"
>
> **Domain expert:** "No — those are **Communities**. They come out of the link graph, which
> tracks which document things came from. A **Chunk** is the territory of an **Organising
> principle**. You can't get from one to the other."
>
> **Dev:** "So where does 'maximise quality-adjusted life years' sit? There's a Page about it."
>
> **Domain expert:** "The Page is the lookup entry. It's an **Organising principle** if the
> same **Warrant** turns up under decisions from NICE, DHSC and the UK NSC independently.
> One document relying on it is just an author being brisk."
>
> **Dev:** "And if a document jumps straight from the incidence figures to 'don't screen'?"
>
> **Domain expert:** "That's a **Gap**. Record it, don't fill it. It becomes a question for
> whoever's leaving, and if they answer it you've got an **Elicited answer**."

## Flagged ambiguities

- **"Theme"** was used across earlier sessions for three different things: a **Community**,
  a **Chunk**, and an **Organising principle**. Resolved: the word is retired. Use the
  specific term.
- **"Node"** meant both a **Page** on the map and an **Argument node**. Resolved: "node"
  unqualified is not used; say which.
- **"Edge"** means a **Justification edge** in the graph and an **Edge of the model** in
  the framework. Resolved: both terms keep their full form; "edge" alone is not used.
- **"Scope"** is both a **Scope** (tier/owner partition) and the `scope` field on an
  **Argument node** (jurisdiction, date, population). Resolved: unrelated, both keep the
  name, disambiguate by context — but never in the same sentence.
- **"Grounded"** meant both "cites a source passage" (traceability, a property of a
  document) and "trustworthy as a model". Resolved: these are different. See
  `docs/FRAMEWORK.md` §"What grounded means here". Conflating them is what collapsed two
  earlier designs into an index.
- **"Tension"** was used for both a genuine disagreement between sources and a surface
  contradiction dissolved by an unstated principle. Resolved: the first is an **Edge of the
  model**; the second is evidence for an **Organising principle**. The word "tension" is
  retired.
