---
name: plan-synthesis
version: 0.1.0
description: Plan how to make a material the scale-catal documents may not hold whole (a composition, a phase or order, a shape, a size, a support) by synthesis adaptive reasoning on top of query-graph — each requirement treated as a sub-goal with its evidence graded, the records followed where they meet it, and the graph's controlled experiments (a preparation and its variant that changed one thing) used where they do not; the draft checked by chemistry rules, quoted, reviewed once, and ended with what to measure and what to change if it fails. Also answers what one change to a synthesis will do, what a procedure will give, and what to change after a failed one. Use when the user asks how to make, adapt or fix the synthesis of a material from scale-catal documents.
scale_release: ">=v0.4.0"
inputs:
  - The target material and its requirements (composition and ratio, phase or order, shape, size, support, route), or a synthesis question (what a change will do, what a procedure gives, what to change after a failure)
  - The scale-catal documents to plan from (each pins its schema version)
allowed-tools:
  - Bash(scale graph describe:*)   # what the schema can express
  - Bash(scale graph stats:*)   # the units in use and the link patterns of a document
  - Bash(scale graph query:*)   # plans: the precedents plan, the variant-pairs plan, others
  - Bash(scale graph search:*)   # evidence by words and meaning
  - Bash(scale graph recipe:*)   # a preparation read whole
  - Bash(scale call describe:*)
  - Bash(scale call query:*)
  - Bash(scale call search:*)
  - Bash(scale call recipe:*)
  - Bash(python3 -I ${CLAUDE_SKILL_DIR}/scripts/*)   # scripts/check_chemistry.py (rule checks on a draft)
  - Bash(python3 -I ${CLAUDE_SKILL_DIR}/../query-graph/scripts/*)   # precedents.py and check_quotes.py of query-graph
  # write access to the working directory only
outputs:
  - The requirements as sub-goals, each with its evidence grade (direct, analog, none) and the records behind it
  - For each analog or unsupported sub-goal, the lever it relies on, the controlled experiments (variant pairs) and claims for and against it, and the operation chosen
  - One procedure a person can run as written, each step tagged retrieved, adapted or hypothesis with its record and quote (as query-graph)
  - The output of check_chemistry.py on the draft and how each error was settled; check_quotes.py on the final answer
  - A failure plan for the sub-goals without direct evidence - what to measure that tells it failed, the change to make by failure kind, its evidence
  - For a question about one change, one procedure or one failure, the prediction, its basis (retrieved, analog, hypothesis), a confidence and the records relied on
validation:
  - Every requirement is a sub-goal with a stated evidence grade; none is dropped silently
  - A direct sub-goal follows its records; an analog or unsupported one names the lever it relies on and the variant pairs or claims that support or contradict it
  - The route stays one coherent procedure - a step comes from another record only when an operation needs it, and says which
  - check_chemistry.py reports no error on the final draft; every warning is answered in one line in the step it names
  - Every step and quotation passes query-graph's check_quotes.py
  - Every sub-goal without direct evidence has a line in the failure plan
  - A prediction states its basis and its confidence, and a lower confidence when its basis is analog or hypothesis
human-review:
  - Always before a proposed procedure is run in a laboratory, with a hazard assessment
  - Whenever a step or a prediction rests on an analog or a hypothesis
---

# Plan a synthesis

A synthesis the records do not hold whole has to be reasoned, not assembled. This skill is query-graph's proposal
(its step 6: precedents, the choice among the options, the record each step comes from, the quotation check, one
review) with a layer of adaptive reasoning for materials: each requirement is a sub-goal of its own, the effort follows
the evidence, and the graph's controlled experiments carry the chemistry where no record meets a requirement.
Read [query-graph](../query-graph/SKILL.md) first; this skill does not repeat its rules and changes none of them.

## Procedure

1. **Precedents, as query-graph does.** Write the requirements as tests of the precedents plan, hardest first, and run
   `../query-graph/scripts/precedents.py` with `--consensus 50`. Choose the route as query-graph says (the route the
   question names, else the one that suits the support, phase, size and shape; a standard, well-controlled route over
   one that is hard to control) and read whole the one or two records most choices come from.

2. **Grade the evidence of each sub-goal.** The sub-goals are the requirements: the composition and its ratio, the
   phase or order, the shape, the size, the support and how it is loaded. For each, from the precedents:
   - *direct*: a record of the chosen route meets it. Follow the record; no more search.
   - *analog*: records meet it with other metals, supports or routes.
   - *none*: no record meets it.

3. **For an analog or unsupported sub-goal, reason from the controlled experiments.**
   - State in one line the lever you rely on: what sets this property in this chemistry (a reduction rate, a capping
     agent or a halide that favours a facet, a seed, an ordering anneal, how and when the support is loaded).
   - Run `references/variant-pairs-plan.json` (`scale graph query <graph> <plan>`): every preparation that is a
     variant of another, the one change the source states (`changed`, `varied`) and what each obtained. Filter it to
     the changes that pull your lever (the same reagent added, removed or replaced; a temperature, a time, an amount)
     and read what they did, in which chemistry. Look also for the authors' claims about the lever (`Claim` records)
     and for records that contradict it.
   - Choose the operation the evidence best supports — substitute a precursor, change the order of addition, change
     the reducing regime, add a seeding or growth stage, change when and how the support is loaded, add or move an
     ordering treatment — and say what it risks. An operation transferred from other metals or another route is
     *adapted* when its variant pairs are close (the same family of metals and route) and a *hypothesis* otherwise.

4. **Keep the route coherent.** Apply the operations to the chosen route. A step comes from another record only when an
   operation needs it, and then it says which operation. The procedure stays one a person can run as written.

5. **Check the chemistry by rules.** Write the draft as JSON in the form of `references/procedure-schema.json` and run
   `scripts/check_chemistry.py <draft.json> --question <question.md>`. Settle each **error** by changing the step it
   names (an error is a rule published procedures break only exceptionally). Answer each **warning** in one line in the
   step it names: a warning is a rule real procedures break for good reasons (a dealloyed or electrodeposited material
   needs no reducing agent; a material ordered in solution needs no anneal). Run it again on the final draft.

6. **Quote, check and review as query-graph does:** `../query-graph/scripts/check_quotes.py` on the proposal, one review
   by a second agent with a fresh context (chemistry that would fail in this context first), the findings settled by
   changing the steps, the quotation check run again.

7. **Plan the adaptation to a failed experiment.** End with a short table, one row per sub-goal that rests on an analog
   or on no evidence: what to measure that tells it failed, and the change to make if it does, by failure kind — the
   alloy did not form; it formed but did not order; the phase is right but the shape or size is lost; the loading or
   dispersion on the support is wrong — each change citing its evidence (a variant pair, a record, a claim). A failure
   changes the step that sets that property, not the whole route.

8. **Present** as query-graph does: the supported answer first, the sub-goals with their grades, the steps with their
   records, the check's outputs, the failure plan, and a Limits line.

## Questions about one change, one procedure or one failure

The same reasoning answers three shorter questions without a full proposal:

- *What will this one change do?* (a reagent added, removed or replaced; a temperature, a time, an amount): find the
  variant pairs that made the same or a similar change and what they obtained, and the records closest to the base.
- *What will this procedure give?* (shape, order, size): find the records closest to it and what they obtained.
- *This product came out wrong; what should change?* (not alloyed, not ordered, the shape or size lost): find what the
  records that obtained the asked product did differently.

Answer with the prediction, its **basis** — *retrieved* (the graph holds this same change or synthesis with its
outcome), *analog* (the same kind of change or synthesis with other metals, supports or routes) or *hypothesis* (no
record bears on it) — a confidence that is lower for an analog and lower still for a hypothesis, two sentences of why,
and the records relied on. Never use the records of the paper the question comes from when that paper is the answer
being tested.

## Rules

- Every rule of query-graph holds: rows as reported, typed absences reported, numbers computed by code, quotations
  checked, recommendations labelled and never changing a step.
- A variant pair is a controlled experiment of one source, in its chemistry: say the metals, route and support it was
  run in when you transfer it, and never treat a pair from another family of metals as direct evidence.
- check_chemistry.py's rules are rules of thumb a chemist applies, not a validation: passing them does not make a
  procedure safe or right. A person reviews every proposal before it is run, with a hazard assessment.
