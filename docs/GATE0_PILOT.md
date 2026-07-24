# Gate 0 — manual pilot

Status: **done.** Read by hand, no code, no model calls.

`MODEL_SPEC.md` §9 sets this gate as four overlapping documents read manually, to find out
whether the design is worth building before anything is built. §11's build-order question
added a third tally.

Read in two passes. The first pass took four screening documents and produced a result that
the second pass partly overturned — see "What the four-document pass got wrong" below. Both
are recorded, because the way the small sample failed is itself a finding about the design.

## What was read

### Pass 1 — four screening decisions, all DHSC

| Document | Genre | Date |
|---|---|---|
| Prostate cancer screening — equality impact assessment | EIA | Jun 2026 |
| Lung cancer screening — equality impact assessment | EIA | Jul 2025 |
| Cervical screening risk stratification — impact assessment | Green Book IA | Sep 2024 |
| Cervical screening HPV self-sampling — impact assessment | Green Book IA | May 2025 |

Chosen because they are the same *kind* of decision — should we screen, whom, how often —
applied to different cancers. If reasoning recurs anywhere in this corpus, it recurs here.
Two genres by accident, which turned out to matter (see "The carve" below).

### Pass 2 — the remaining nine

| Document | Genre | Read |
|---|---|---|
| 2010 to 2015 government policy: cancer research and treatment | policy paper | in full |
| Major conditions strategy — call for evidence (2023) | call for evidence | in full |
| Tobacco and vapes — evidence to support legislation (2025) | call for evidence | rationale sections |
| Major conditions strategy — case for change (2023) | strategy | ch.1 + framework |
| The National Cancer Plan for England (2026) | strategy | exec + early-diagnosis ch. |
| Results of the National Cancer Plan call for evidence (2026) | consultation results | structure + summary |
| National Cancer Plan — equality impact assessment | EIA | structure + searches |
| Improving Outcomes: A Strategy for Cancer (2011) | strategy | screening ch. + searches |

Method, stated plainly because it affects how much the numbers are worth: two documents read
end to end, the rest read where the reasoning is and searched everywhere else for the six
candidate warrants from pass 1. The recurrence counts below are from searches over all
thirteen documents; the structural findings are from reading.

## Tally 1 — how often does a document jump?

A jump is a move from a fact to a recommendation with the connecting step missing.

**About 19 substantive jumps across four documents, roughly five each.** Not extraction
noise, and not so many that a question list would be unreadable. Extrapolated across
thirteen documents: sixty or so, which is an afternoon's work for an expert if they are
ranked.

Representative ones:

- Prostate: the 45–61 age range was chosen because it "had one of the lowest rates of
  overdiagnosis". Why minimising overdiagnosis is the selection criterion, rather than
  maximising life-years saved, is never stated.
- Prostate: Black men are excluded because the evidence is "limited and uncertain". The
  step from uncertainty to *not screening* — rather than to screening, or to screening with
  monitoring — is never argued. The same uncertainty could support either.
- Prostate: "Systematic evidence-based screening should be offered to everyone in a target
  group... It is an inherently fair concept that should reduce health disparities."
  Asserted with no grounds at all, and it is load-bearing for the whole equality section.
- Lung: 55–74 because "evidence for the benefits of screening outweighing the harms was
  strongest in the 55 to 74 years cohort". Strength of evidence and size of net benefit are
  different things, and the document slides between them.
- Lung: the workforce need is quantified (75 radiologists, 184 radiographers, 278 nurses),
  the shortage is acknowledged, and the conclusion is that it "has been factored into the
  plans". No argument that the capacity exists.
- Cervical intervals: health effects are not monetised because QALY estimates are
  uncertain, so the entire case rests on £81.5m of cost savings. "Not monetised" quietly
  becomes "not present" in the conclusion.
- Cervical intervals: the risk that the change is "perceived to increase risk... could lower
  trust" is answered with communications, never with reconsideration. That public perception
  is a messaging problem rather than evidence about acceptability is assumed throughout.

## Tally 2 — does anything chain?

**Yes. Median chain length 3, and — the finding that matters — chains cross documents.**

Within a document, the deepest is prostate:

```
screen BRCA2 + family history, 45–61
  ← benefits outweigh harms for this group
      ← general-population harms outweigh benefits
          ← PSA cannot separate aggressive from indolent disease
              ← 75% of raised PSA is not cancer; 15% of normal PSA is  [statistic]
          ← most prostate cancer is indolent
              ← autopsy series                                        [statistic]
          ← treatment harms are severe
              ← 71% bladder difficulty, 66% erectile dysfunction      [statistic]
```

Four argued steps before hitting a number. Lung is shallower (about two). Cervical
intervals is three to four, and includes a step worth noting: three competing estimates of
the cost per hr-HPV test are compared and one is chosen, with a stated reason. That is an
argued methodological choice, and it is exactly the kind of thing a page-shaped wiki
renders as a bare number.

**Cross-document chaining is common and is the strongest result here.** Conclusions from
one document are used as premises in another, routinely:

- The 2015 move to hr-HPV primary testing is a conclusion elsewhere and a premise in both
  cervical documents.
- The 2019 interval recommendation is the *conclusion* of the risk-stratification IA and a
  *premise* in the self-sampling IA.
- The 2013 decision to raise the starting age from 20 to 25 — itself argued, from 3,000
  unnecessary treatments and the premature-birth risk of repeat treatment — is reused as a
  premise in both.
- Prostate borrows the cervical programme's trans opt-in mechanism as precedent, and the
  AAA programme's inequalities coverage standard.
- Lung and prostate both use the *bowel* screening uptake gap as grounds for predicting
  their own.

These documents argue by precedent from other programmes constantly. That is the network
§1 hoped for, and it is real.

**Gate 2's failure case did not occur.** Support is not "nearly always a statistic".

## Tally 3 — does the same jump recur across documents?

Yes, and this is where the highest-value questions are. Ranked by how many documents lean
on the step versus how many state it:

| Warrant | Relied on | Stated |
|---|---|---|
| Benefits must outweigh harms | 4 of 4 | 3 of 4 — but *how* the balance is struck, never |
| Unequal benefit is acceptable when it tracks risk, not treatment | 3–4 | once (lung, on sex) |
| Under evidential uncertainty, default to not screening | 3 | never |
| Operational identifiability constrains clinical eligibility | 3 | once (lung: smoking is "the most accessible record of risk") |
| £20,000–£30,000 per QALY | 3+ | once — and that one says it is not binding |
| Those at highest risk attend least; proceed with mitigations | 4 of 4 | never argued, only observed |

Three of these are worth pulling out.

**The harm–benefit rule is stated everywhere and specified nowhere.** Every one of these
four decisions turns on it. Not one document says how a death averted trades against an
unnecessary treatment, or against a case of erectile dysfunction. The rule is doing all the
work and carries no content. That is the single best question to put to a departing expert,
and no amount of further reading will answer it, because the answer was never written down.

**The cost-effectiveness threshold governs decisions that never mention it.** £20–30k per
QALY appears once, in the self-sampling IA, imported from NICE's HTA methodology — and in
the same paragraph the document notes that "UK NSC is not bound by any specific
cost-effectiveness methodology in its terms of reference". So a threshold that is formally
optional is functionally binding, and it does not appear at all in the prostate EIA even
though that decision rests on a commissioned cost-effectiveness model.

**One recurrent warrant has a contradicting instance.** "Under uncertainty, don't screen"
governs the prostate exclusions and the 2013 age change. The self-sampling IA states the
opposite outright: "For those who do not attend their appointments, any test is better than
no test." Same institution, same programme family, one year apart. That is either a real
boundary — the rule applies to *offering* screening but not to *how* an already-agreed
screen is collected — or an inconsistency. Either way it is an edge of the model in the
`FRAMEWORK.md` sense, and no topic-shaped wiki would ever put those two sentences next to
each other.

## What the four-document pass got wrong

**The harm–benefit rule does not recur across the field. It recurs within one decision
type.** Searched across all thirteen documents, it appears substantively in the four
screening appraisals and essentially nowhere else — zero hits in the 2022 cancer call for
evidence, the Major Conditions call for evidence, the Major Conditions case for change, the
National Cancer Plan EIA, the consultation results, and the tobacco evidence pack. One
passing use in the 2026 National Cancer Plan (applied to new technologies), and four in the
2011 strategy, all inside its screening chapter.

Pass 1 called this the strongest recurrent warrant in the corpus. It was an artefact of
having picked four documents of one type. The rule is real and it is unspecified, but it
belongs to screening appraisal, not to UK cancer policy.

Same for the £20–30k QALY threshold: outside the two Green Book IAs it barely exists.

**Corpus-wide, the number of documents that contain extractable argument at all is lower
than the pilot assumed.** The 2010–2015 policy paper is a list of actions with money
attached and contains one argued claim in seven pages. The Major Conditions call for
evidence is a questionnaire. Roughly a third of this corpus is argument-poor, and a design
that assumes every document yields fifteen argument nodes will produce noise from those.

**One document is a trap.** "Results of the National Cancer Plan call for evidence" reports
what 11,918 respondents said. Extract claims from it and the provenance says DHSC while the
claim belongs to the public. Nothing in `MODEL_SPEC.md` §4 currently distinguishes *the
department asserts X* from *the department reports that respondents asserted X*, and on this
corpus that omission would corrupt the recurrence counts directly.

## What only appeared once the genres were mixed

**The corpus contains two incompatible decision rules and never reconciles them.**

Screening reasons *proportionately*: prove net benefit before offering anything, and where
the evidence is uncertain, do not screen. Tobacco reasons *categorically*: "Tobacco is a
uniquely harmful product... No other consumer product kills up to two-thirds of its users."
No balance is struck, no threshold is cited, no QALY appears. Youth appeal alone is
sufficient grounds for restriction.

Same department, same disease, opposite standards of proof. A newcomer would confidently
predict one government applies one decision rule to cancer prevention. It does not, and
nothing in the corpus says why. This is a surprise in the `FRAMEWORK.md` sense and it is the
best single finding in the pilot — and four screening documents could not have produced it.

**The same programme is described two ways depending on the document's job.** The screening
IAs treat screening as a delicate balance that can easily do more harm than good. The
National Cancer Plan calls lung screening "transformational" and commits to completing
rollout, with no harms discussed at all. Both are DHSC, both about the same programme.

**A reversal is detectable, and it is not acknowledged anywhere.** The 2023 Major Conditions
strategy argues explicitly for moving "away from single disease strategies". The 2026
National Cancer Plan is a single disease strategy and never mentions the Major Conditions
strategy once. The argument *for* the abandoned approach survives in the corpus; the
argument against it exists nowhere.

`MODEL_SPEC.md` §12 says the system "may detect the scar — an option conspicuously
unaddressed — but not what was in it". This pilot found one by hand. The detection pattern
is concrete: document A argues X, later document B does not-X, and no document links them.
That is a graph query, not a judgement call, and it is worth adding to §12 as something the
design can actually do rather than a limitation it must accept.

## A finding that changes the spec: R2 counts the wrong institution

`MODEL_SPEC.md` §5 R2 says recurrence licenses inference only across uncoordinated authors,
and to count institutions rather than documents, because DHSC, NHSE and NICE share staff
and templates.

All four documents here are DHSC. On R2 as written, everything in tally 3 counts as **one
institution** and licenses nothing.

But that is the wrong count. The documents themselves attribute the warrants to different
bodies: the harm–benefit rule to UK NSC's evidence review criteria, the threshold to NICE's
HTA methodology, the equality frame to the Equality Act's public sector equality duty, the
appraisal method to HM Treasury's Green Book. The author is DHSC; the *owners* of the
warrants are four different institutions with separate mandates.

**R2 needs to count the institution that owns the warrant, not the one that wrote the
document.** As it stands the rule would suppress every finding in this pilot.

## The carve

Warrants sort by **genre**, not by cancer.

The economic warrants — merit good, positive externality, 3.5% discount rate, willingness
to pay per QALY — appear only in the two Green Book IAs. The equality warrants — the public
sector equality duty, protected characteristics, the risk-tracking defence of unequal
benefit — do their real work only in the two EIAs. Cervical cancer appears in both genres
and the reasoning in each has more in common with the other cancer in its genre than with
itself.

This is direct evidence for `FRAMEWORK.md` §"Chunks": grouping by topic, or by which
document something came from, is the novice sort. The expert carve here looks like
*decision type* — is this a new programme, a change to an existing one, a change to how a
sample is collected — because that is what determines which body of rules applies.

## Verdict

- **Tally 1 passes**, with a correction: jump density varies by an order of magnitude across
  the corpus. Screening appraisals are dense; strategies are moderate; calls for evidence and
  policy summaries are near-empty. Extraction cost should not be spent evenly.
- **Tally 2 passes, and got stronger.** The 2026 National Cancer Plan uses the conclusions of
  the screening impact assessments as its premises. That is the chain the design is for,
  running across genres and across five years.
- **Tally 3 passes, but not the way pass 1 said.** Warrants recur strongly *within* a
  decision type and weakly *across* the corpus. The valuable finding is not that a warrant
  recurs — it is that two decision types use incompatible ones and nothing reconciles them.

No route is killed.

### The carve, restated

Pass 1 said warrants sort by genre rather than by cancer. Nine more documents sharpen that:
they sort by **decision type**. Should we offer this screen; should we restrict this product;
how should we organise services; what should we prioritise. Each carries its own decision
rule, its own standard of proof, and its own vocabulary, and the cancer site is almost
irrelevant to which applies.

The current build groups pages by link density, which tracks which document things came from.
Not one of these four decision types is a document. This is `FRAMEWORK.md` §"Chunks"
confirmed against the corpus rather than asserted: the expert carve is by decision type, and
no amount of tuning the community detection will find it, because the signal it needs is not
in the link graph.

### Honest limits

Thirteen documents, one department, one policy area, and two of the thirteen read end to end.
The recurrence counts are search-based and would miss a warrant expressed in different words
each time — which is precisely the case the design's merging step exists to handle, and which
this pilot therefore cannot rule on. R2's institution question remains untestable on this
corpus at any sample size, because everything in it is DHSC.
