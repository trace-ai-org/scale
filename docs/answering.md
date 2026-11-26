# Answering questions from the evidence graph

How an answer drawn from a scale-catal graph is found, written and checked: in the graph explorer (questions answered
with a reader's own key, `site/explorer.js`), and for any agent that follows the query-graph skill. The principles
come from a design review that weighed what a reader needs, scientific rigor and cost against one another, and
measured the tools' real output on the public methanol graph.

## Principles

1. **Support first.** Every factual clause has identifiable supporting evidence (a record id and a locator); prose
   never repairs missing support.
2. **Find before you answer.** Reach the relevant records by at least two routes, a structured query plan and an
   evidence search with synonyms, narrower terms and other spellings; before stating that the graph holds none, run
   another plan; say how the rows were found. (One plan on "inactive" in a test's name found 3 of at least 17 tests
   that report no methanol: a fully cited answer can still miss most of the evidence.)
3. **Result identity.** A value keeps its own product, assay, specimen, conditions and evidence (an `entries` step).
4. **Scientific compatibility.** Values as reported, in their units and bases; comparisons and rankings only within
   one context (`comparison: scientific`; a filter's `convert: true` for exact unit conversions only).
5. **Relevance before counting.** Candidates, relevant records, sources and independent works are different counts;
   identical wording in sibling patents is one claim repeated, not corroboration.
6. **Explicit epistemic status.** What was observed, what the authors read it as showing, and the answer's own
   inference are kept visibly apart.
7. **Typed uncertainty.** Source silence, curation gaps, withheld quotes, schema gaps and incompatible context are
   answers, named by kind; an absence in the graph is not an absence in the literature.
8. **Answer first.** A lead of one to three sentences; then a table when the records share columns (conditions shared
   by a source stated once, a column for what was varied) or short attributed prose; caveats that change the meaning
   beside their claims; a Limits line. The information is fixed, not the headings.
9. **Inspectability.** Short citations (source, year, kind, locator) that show the quote and open the record; plans
   and coverage one click away.
10. **Proportionate investigation.** Decompose only compound questions; look for counter-evidence where a claim is
    causal, a consensus, an earliest record or an absence; stop when every distinction asked for has support or an
    explicit unresolved status.
11. **Reversible compression.** Remove repetition before removing information; paginate rather than truncate.
12. **Mechanical checks, honestly labelled.** The page checks references, not meaning.

## The answer

- **Lead**: the answer in one to three sentences.
- **Evidence**: a table or short paragraphs, every factual sentence cited as `[record id, locator]`.
- **Authors' reading**: their interpretations, apart from the observations.
- **What is missing**: each absence by its kind.
- **Found by**: the routes used and what each returned.
- **Limits**: what was left out and why, from the coverage.

## What the explorer does with it

- The prompt is the query-graph skill without the proposal procedure (`data/query-graph.md`, built by `scale site
  build`); a question that asks how to make a material or run a test fetches the procedure through the
  `proposal_procedure` tool (`data/query-graph-propose.md`). The answering rules above follow the skill in the prompt.
- Each citation becomes a short link (source title, year, kind and locator) whose title shows the cited record's
  quote at that locator and whose click opens the record.
- **Reference checks**, run in the page on every answer: each cited id is a record of the graph; a tool returned it
  while the answer was written; the cited locator is one the record's evidence gives; a phrase quoted in the
  citation's sentence is in that record's evidence (line-break hyphens, page marks, spacing and ellipses aside). The line
  says "reference checks", never that the answer is verified: whether a record supports a sentence is a reader's
  judgment.

## The tools' results

The page asks the toolkit for the **model view** of query and search results (docs/querying.md, "The model view"):
each quote once (`E1`..`En`) with explicit ownership, each source once, ids shortened with `~` for an id prefix,
rows in pages fetched with `page.next` rather than truncated; `stats` and `describe` narrowed to what a plan needs.
On the methanol graph this sends the model 49-65% fewer characters per query, losing nothing an answer uses (a
round-trip test rebuilds the full result).

## How it was tested

Six questions (five on the methanol graph, one on catal-commons) were answered by the current prompt and by the new
one, each with the page's four tools, and judged blind by a second model that checked every claim against the graph
(faithfulness, recall, scientific care, reader effort, 1-5 each). After a first round the answering rules were revised
for what the judge found (missed record kinds, retrieval gaps written as source absences, lost context, unchecked
counts, too much narration). In the second round the new answers scored 90 against 83 in total, with preferences
split (2 new, 3 current, 1 tie); they read 3 to 15 times less tool output on five of the six questions and were
about 40% shorter. The judge's remaining weaknesses (misses that become absence claims, claim statuses, scope,
locators, numerical context) shaped the last rules and the locator check. This is a small test: six questions,
one answering model, one judge.
