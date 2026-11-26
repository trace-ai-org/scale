---
name: add-source
version: 0.1.0
description: Turn any source a person has - a publication, patent, thesis or report (a PDF, a DOI, its text), a data file (CSV, Excel), or a graph file - into records of their own scale-catal document, in their own data repository, by one entry point (scale add-source) - asking back only what is uncertain, never guessing, and saying what was added, what the checklist still misses and where the document is saved. Use when someone says "turn this file into a graph", "add this paper / spreadsheet / data to my graph", "put my results into SCALE", or gives a file or a DOI and wants it in their knowledge graph.
scale_release: ">v0.4.0"
inputs:
  - The source - a file path (PDF, .csv, .xlsx, .json graph, .txt) or a DOI
  - Where their data live - an existing data repository or document (asked once; never this framework repository)
  - For a new data repository - the project id and who contributes (asked; never invented)
allowed-tools:
  - Bash(scale add-source:*)
  - Bash(scale data new:*)
  - Bash(scale data init:*)
  - Bash(scale data verify:*)
  - Bash(scale data match:*)
  - Bash(scale graph validate:*)
  - Bash(scale graph describe:*)
  - Bash(scale source check:*)
  - Bash(scale review values:*)
  - Bash(scale call skills:*)
  - Bash(scale call describe:*)
  # not pre-approved, so a person sees each call: git commit and git push in the data repository
outputs:
  - The person's document with the source's records added (a table imported and verified; a publication curated and folded in; a graph merged)
  - For a publication, the curation folder in the data repository's work/sources/ (kept on the computer)
  - A short report - what was added, the questions asked and answered, what the checklist still misses, where the document is saved
validation:
  - "`scale graph validate DOC` reports no error"
  - "`scale data verify DOC` reports VERIFIED for every table added"
  - For a publication, `scale source check FOLDER` reports no error before `--finish`
human-review:
  - Every question scale add-source asks is answered by the person (a column, a unit, a recording time, a sheet, the rights of a table), never by the agent
  - The person checks the review sheet of a curated publication's values before it is folded in
---

# Add a source to your graph

The person has something - a paper, a patent, a thesis, a report, a spreadsheet of their measurements, a graph a
colleague sent - and wants it in their knowledge graph. One command routes it:

```bash
scale add-source FILE_OR_DOI --into DOC
```

(the agent tool is `scale_add_source` with `source` and `into`). Keep the person informed in one line per step;
ask only what the tool asks, and only them.

## 1. Find, or make, their data repository and document

Their documents live in **their own data repository**, never in the SCALE framework repository (whose readers are the
public; the tools refuse to write project data there, and so do you). In order:

1. They name a document (`graphs/lab.json`) or a folder: use it.
2. The current folder is inside a data repository (a `scale-data.yaml` at its root or above): use its
   `graphs/lab.json` (or the document they choose under `graphs/`).
3. None: ask once where to keep it, and the project id and who contributes (`lab:project/<name>`,
   `lab:org/<their group>`; never invent them), then

   ```bash
   scale data init ~/data/<name> --project <project id> --contributor <institution id>
   ```

   It makes a private git repository (mode 0700, no remote) with a policy, the data guard's hooks and a starter
   document, `graphs/lab.json`, that states the policy's stewardship. Another document: `scale data new
   graphs/<name>.json` (it takes the same defaults).

If the place they name is inside a git repository that holds SCALE's schema (`core/graph/kernel.yaml` and
`domain/DOMAIN.yaml`), stop and say why: that is the framework, and their data do not go there.

## 2. Route the source

```bash
scale add-source <file or DOI> --into <DOC> --dry-run     # optional: what it would do
scale add-source <file or DOI> --into <DOC>
```

It detects the kind from the file's first bytes and name (`--kind table|document|graph` overrides):

| the source | what happens |
|---|---|
| a table: `.csv` (any common dialect), `.xlsx`, a text file laid out in columns | the built-in mapping that fits is chosen; when nothing is uncertain, the table is imported (one Dataset, records with stable ids, every value citing its cell), validated, and every cited cell checked against the file; a file from outside the data repository is copied into its `raw/` |
| a publication: a PDF, a DOI, a `.txt` of its text | a curation folder is started in the data repository's `work/sources/<slug>/` with the document's stewardship (step 4) |
| a graph: a scale-catal `.json` | validated; merged into DOC only with `--merge` (step 5) |

An `.xls`, `.docx` or `.ods` file is refused with what to save it as; say that to the person.

## 3. A table: answer only what it asks

When the status is `QUESTIONS`, nothing was written. Each question names what is uncertain and the option that
settles it:

| question | ask the person | option |
|---|---|---|
| `recorded_at` | when the data were recorded, or whether the table comes from a publication | `--recorded-at 2026-03-02T10:00:00Z`, or `--origin reported` |
| `bind:<role>` | which column holds it (the candidates are listed) | `--bind <role>=<column>` |
| `unit:<column>` | whether the unit assumed is right | `--confirm unit:<column>`, or they write the unit in the header |
| `sheet` | which sheet of the workbook | `--sheet <name>` |
| `rights` | the licence of a publication's table | `--data-rights in_copyright` (or open, public_domain) |
| `fix:<column>` | to correct cells no rule reads (`n.d.`, a text in a number column) in the file | none: the file changes |

Put the person's answers into the `retry` command it prints (its upper-case words, `COLUMN`, `YYYY-...`, are
placeholders) and run it. **Never answer a question yourself**, not even with the suggestion: a guessed column, unit or
date is a wrong record that looks right. The notes (a header row found, decimal commas) and the assumptions
(`assumed (check it)`) are reported: read them to the person.

When the table is a publication's (its Table 2, its SI), add the publication first (step 4) and then the table with
`--from-source <the Source id> --locator "Table 2"`: the Dataset is then REPORTED_IN the publication.

## 4. A publication: curate, check, fold in

The status is `STARTED` and the folder holds `BRIEF.md`, the page texts and the graph to fill. Then:

1. Curate by the curate-corpus skill (`scale call skills '{"name": "curate-corpus"}'`), reading the page texts and
   recording what the source reports, each record with its evidence; `scale graph describe <Label> --domain
   catalysis` for what to record.
2. `scale source check <folder> --derive-links` until it reports no error (quotes stay within the copyright cap).
3. `scale review values <graph> --out <folder>/review-values.md`, and the person checks it.
4. `scale add-source --finish <folder> --into <DOC>`: the graph's records are added with their ids, the Source's rights
   entry goes to the data repository's `rights.yaml`, and the result must validate.

A DOI without an open-access copy under an open licence comes back with one question: the PDF (`scale add-source
paper.pdf --doi <DOI> --into <DOC>`). Ask for it; never fetch a paywalled copy.

## 5. A graph file

`scale add-source colleague.json --into <DOC>` validates it and says how it would merge; `--merge` adds its records.
A record DOC holds with other content refuses the merge (never overwritten); a graph pinning another schema version is
migrated first; records keep their own audience. To compare without merging, query both: `scale graph query --graph
a=<DOC> --graph b=colleague.json PLAN`.

## 6. Report

Say, in a few lines:

- **what was added**: the records and results (or the publication's records), from which file or source;
- **what was asked and answered**, and the assumptions the person should check;
- **what the checklist still misses** (the `checklist` lines: a test without its reaction, its feed, its electrolyte);
  these are for the person to add when they know them, never to fill with typical values;
- **where it is saved**: the document's path, and that it is the durable copy - commit it in the data repository
  (`git add`, `git commit`; the guard's hooks check what is committed). A copy opened in the graph explorer, a browser
  tab or a chat is a working copy only.

Then they can look at it in the explorer (*Open a graph*, the file), ask it questions (the query-graph skill), and
compare it with other graphs (`scale graph query --graph a=... --graph b=...`).
