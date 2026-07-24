# The unit of the model is the claim; pages are kept beneath it as reference

The wiki is made of pages, and a page is a topic. A topic cannot be true or false, cannot
explain a decision, and cannot be scored against a prediction — so asking "is this page
grounded?" has no answer except "it cites something", and every attempt to build a mental
model out of pages collapsed back into an index. We are therefore extracting **claims**
(with grounds, warrant, qualifier, scope and provenance) as a distinct layer, and keeping
the existing pages underneath as the lookup and provenance surface rather than replacing
them.

## Considered options

**Annotate pages.** Attach principles and surprises to existing pages as metadata. Cheapest
by a wide margin, changes nothing upstream — and reruns the failure, because the underlying
unit still cannot be true or false.

**Claims only.** Drop the page layer entirely. Loses a working, provenance-complete lookup
surface that people can already use, in exchange for nothing the chosen option does not
also deliver.

## Consequences

The change lands **upstream, in Stage 2** (`app/corpus/concepts.py`), not in the map or
synthesis layers where earlier designs put it. Nothing downstream can produce claims
because claims are never extracted; adding structure later cannot recover what was never
taken out of the text.

It adds a genuine new extraction pass, and the quality of the claim extractor becomes a
sharper single point of failure than anything in the current pipeline.

It also makes the framework's prediction test runnable for the first time. You can score a
predicted claim. You cannot score a predicted topic.
