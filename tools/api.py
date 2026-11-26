"""Agent-facing toolkit: one small, safe, vendor-neutral tool surface over scale-catal documents.

The same tools are reachable in every way an agentic system can call software, all from
one install and none tied to a model provider:

* command line — ``scale call <tool> '<json>'`` (JSON in, JSON out; see ``cli.py``);
* HTTP — ``scale serve`` with an OpenAPI description (see ``http_server.py``);
* MCP — ``scale mcp`` over stdio (see ``mcp_server.py``);
* Python — ``ScaleToolkit().call(name, arguments)``.

Tool definitions are plain JSON Schema (``schemas()``), the common denominator of
function-calling formats; ``openapi()`` describes the HTTP form.

Design rules
------------
* **Zero configuration.** Documents live in a working directory (``$SCALE_WORKSPACE`` or
  ``~/.scale/workspace``), created when a tool first writes there. There is no lock and nothing to
  upgrade: a scale-catal document pins its schema version itself (``graph.schema_version``) and is
  read under that version; ``scale_migrate`` moves it to a newer one when asked.
* **Agents do the work, people decide.** Approving and merging pull requests, building releases and
  filing GitHub issues are deliberately *not* agent tools; they stay in the CLI for humans
  (``status.human_only_actions`` lists the commands).
* **Writes stay inside the working directory.** Only ``scale_example``, ``scale_migrate`` (with ``save_as``) and
  ``scale_data_add`` (into a document there) write, and only a sanitized file name there; ``scale_add_source`` and
  ``scale_data_new`` write a document there or in a data repository of one's own (``scale data init``), and the
  curation folder, mapping and raw copy beside it, never in the SCALE framework repository.
* **Results are data.** Every tool returns JSON-serializable dicts; failures return
  ``{"error": ...}`` rather than raising, so an agent can read and recover.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from . import __version__, layout
from .util import ScaleError, find_repo_root, load_doc, load_yaml, write_canonical

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")


def default_workspace() -> Path:
    return Path(os.environ.get("SCALE_WORKSPACE") or Path.home() / ".scale" / "workspace").expanduser().resolve()


# ---------------------------------------------------------------- tool definitions (JSON Schema)

_DOC = {
    "document": {"type": "object", "description": "A scale-catal document (node-link JSON), inline."},
    "path": {"type": "string", "description": "Or a document file: a path relative to the working directory, or absolute."},
}
_DOMAIN = {"domain": {"type": "string", "description": "Whose schema to use: catalysis (the catalysis domain, "
                                                       "domain/), or core for the science kernel alone. "
                                                       "Default: catalysis."}}

_VIEW = {"view": {"type": "string", "enum": ["full", "model"], "default": "full",
                  "description": "model: the same result with its repetition written once, for a model to read "
                                 "(each evidence item once as E1..En with whose evidence it is, each source once, ids "
                                 "with the graph's prefix as ~; a query's rows in pages): lossless, versioned "
                                 "(view.version), its legend in view.legend; docs/querying.md, 'The model view'."}}

TOOLS: list[dict] = [
    {
        "name": "scale_status",
        "description": "Start here. Returns the tool version, the latest release, the schema version (one for the science "
                       "kernel and the catalysis domain together), the domain, the working directory and the documents in "
                       "it, the skills, which capabilities are functioning, mocked or planned, and the actions only a "
                       "person takes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "scale_domains",
        "description": "The catalysis domain (domain/): its status, the schema version (and any "
                       "frozen versions), checklist, maintainers, example query plans (its competency questions) "
                       "and public examples.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "scale_describe",
        "description": "Describe labels and edge types (properties, types, descriptions) and, with domain catalysis, "
                       "the catalysis checklist for them: what a record must and should report. With no names, an "
                       "overview of every label and edge type. Use before writing records or query plans.",
        "input_schema": {"type": "object", "properties": {
            "names": {"type": "array", "items": {"type": "string"}, "description": "Labels or edge types, e.g. CatalyticTest."},
            **_DOMAIN,
            "schema_version": {"type": "string", "description": "A frozen schema version to describe (default: current)."},
            "checklist": {"type": "boolean", "default": True,
                          "description": "With names and domain catalysis: include the catalysis checklist (what a "
                                         "record must and should report; for curating). false leaves it out (for "
                                         "writing query plans)."}}},
    },
    {
        "name": "scale_template",
        "description": "A minimal valid scale-catal document for the catalysis domain (or, with domain core, the "
                       "science kernel alone): the graph header with stewardship, a Source, one evidenced record and "
                       "its REPORTED_IN link, with notes on what to fill in.",
        "input_schema": {"type": "object", "properties": {**_DOMAIN}},
    },
    {
        "name": "scale_validate",
        "description": "Validate a scale-catal document under the schema version it pins. Returns errors (invalid), "
                       "warnings, and conformance to the catalysis checklist it follows (must items missing make a "
                       "record non-conformant, not invalid). Writes nothing.",
        "input_schema": {"type": "object", "properties": {**_DOC, **_DOMAIN,
            "source_texts": {"type": "object", "additionalProperties": {"type": "string"},
                             "description": "Map source id -> full source text, to check quotes verbatim."}}},
    },
    {
        "name": "scale_source_start",
        "description": "Start curating one publication into a scale-catal document (a subgraph to study one paper, "
                       "patent or report). Resolves a DOI's metadata (Crossref) and, with a contact email, downloads an "
                       "open-access PDF only when it carries an open licence; reads a PDF's page texts (or takes the text); "
                       "writes a working folder with the graph to fill (pinned schema, Source record, stewardship), the "
                       "source's rights, the page texts and BRIEF.md (what to curate and how). Then fill the graph "
                       "following the curate-corpus skill, check it with scale_source_check, and finish with "
                       "scale_source_finish.",
        "input_schema": {"type": "object", "properties": {
            "doi": {"type": "string", "description": "The source's DOI (a DOI, a doi: CURIE or a doi.org link)."},
            "pdf": {"type": "string", "description": "The source's PDF file (when there is no open-access copy, or no DOI)."},
            "text": {"type": "string", "description": "Or a text file of the source (pages headed [page N], or separated "
                                                      "by form feeds)."},
            "folder": {"type": "string", "description": "The working folder, relative to the working directory or absolute "
                                                        "(default: the source's slug)."},
            "version": {"type": "string", "description": "The schema version to pin (default: the head)."},
            "email": {"type": "string", "description": "A contact address for Unpaywall's open-access lookup; without one "
                                                       "no open-access copy is looked for."},
            **_DOMAIN}},
    },
    {
        "name": "scale_source_check",
        "description": "Check a curation folder's graph: valid under the schema it pins, every quote found verbatim in the "
                       "source's page texts, checklist conformance, gaps and the copyright cap. ok is false while any "
                       "error remains. Writes nothing, unless derive_links.",
        "input_schema": {"type": "object", "required": ["folder"], "properties": {
            "folder": {"type": "string", "description": "The curation folder (as scale_source_start returned it)."},
            "derive_links": {"type": "boolean", "description": "First write the REPORTED_IN links the graph's evidence "
                                                               "implies (one per record and cited source)."}}},
    },
    {
        "name": "scale_source_finish",
        "description": "Finish a curation folder whose graph checks without error: build its evidence index; with public, "
                       "write a copy to share with quoted text capped (only an in-copyright source's text is withheld, as "
                       "sha256 and length); with bundle, export it as a kg-bundle under bundle/ with a short title.",
        "input_schema": {"type": "object", "required": ["folder"], "properties": {
            "folder": {"type": "string", "description": "The curation folder."},
            "public": {"type": "boolean", "description": "Also write <slug>.public.graph.json, capped."},
            "bundle": {"type": "boolean", "description": "Also export a kg-bundle (of the public copy with public)."},
            "title": {"type": "string", "description": "The bundle's title, at most 40 characters (default: the graph's)."}}},
    },
    {
        "name": "scale_query",
        "description": "Run a scale-catal query plan (match, steps, where, return, evidence, order_by, limit) over a "
                       "document. Returns rows with the values as reported and their evidence (each item with its "
                       "source's kind, peer review and publication status, and whether it is second-hand), records left "
                       "out and why, a coverage block (candidates, each stage's rows in and out, everything left out by "
                       "reason, the values compared, whether the plan narrows nothing) and the kind of answer: answered, "
                       "no_matching_record, missing_context or schema_gap. A numeric filter (eq, ne and in with numbers too) "
                       "states its unit and compares only values in that unit (spelled the same, case included: mPa is "
                       "not MPa) and on the basis it states; nothing is converted. 'not stated' is no unit (the "
                       "placeholder for a number printed without one): never filter in it. With comparison: scientific, "
                       "values are also compared and ranked only within one context of their quantity contract (a "
                       "potential's reference scale, a current density's normalization, a pressure's convention, a flow's "
                       "reference conditions), stated with context to check one or grouped; a value whose context is not "
                       "stated is left out as missing_context. A step {from, entries, kind, as} binds one value entry "
                       "per row (result_values, a per-product list, a map or a scalar) and {from, field, to, as} follows "
                       "its target, variable or produced_by: every filter, returned value and evidence item on it is "
                       "that one result's. Absences are answers; never fill them with typical values. With graphs (several "
                     "documents, each with an alias), the plan runs on each graph on its own under the schema it pins, "
                     "nothing merged: every row says its graph and digest, a record is (alias, id), rows of different "
                     "graphs join only through an evidenced identity_map (the plan's join steps), one id with two "
                     "definitions is an identity_conflict, a graph that cannot be read makes the answer partial, ranking "
                     "and limit apply after federation, and the access is the intersection of the graphs' audiences.",
        "input_schema": {"type": "object", "required": ["plan"], "properties": {
            "plan": {"type": "object", "description": "Query plan; see the query-graph skill and example_plans in scale_domains."},
            **_DOC, **_DOMAIN,
            "graphs": {"type": "array", "description": "Query several documents without merging them (docs/querying.md, "
                                                      "Querying across graphs), instead of document or path.",
                       "items": {"type": "object", "required": ["alias"], "properties": {
                           "alias": {"type": "string", "description": "A name for the graph (letters, digits, _ or -)."},
                           "path": _DOC["path"], "document": _DOC["document"],
                           "spec_sha256": {"type": "string", "description": "The spec digest the caller binds this graph's "
                                                                            "schema to; another digest is spec_mismatch."}}}},
            "identity_map": {"description": "With graphs: the evidenced identity map of shared reference records (an "
                                            "object, or a file path).", "type": ["object", "string"]},
            "format": {"type": "string", "enum": ["json", "markdown"], "description": "markdown adds a readable rendering."},
            **_VIEW,
            "offset": {"type": "integer", "minimum": 0, "default": 0,
                       "description": "With view model: the first row of the page (page.next gives the next one's)."},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100,
                          "description": "With view model: at most this many rows in a page."},
            "page_chars": {"type": "integer", "minimum": 1000, "default": 40000,
                           "description": "With view model: past its first row, a page holds about this many "
                                          "characters of rows and their evidence at most; page.next continues."}}},
    },
    {
        "name": "scale_stats",
        "description": "What a document holds, to write query plans against: counts of labels, edge types and distinct "
                       "quotes, the properties in use per label, the units in use per label (or edge type) and property "
                       "path (a numeric filter states one of them), and the link patterns (source label, edge type, "
                       "target label). labels and properties narrow the properties, units and link patterns to "
                       "those named (the counts stay the whole document's). Writes nothing.",
        "input_schema": {"type": "object", "properties": {**_DOC,
            "labels": {"type": "array", "items": {"type": "string"},
                       "description": "Labels or edge types whose properties, units and link patterns to keep, e.g. "
                                      "CatalyticTest."},
            "properties": {"type": "array", "items": {"type": "string"},
                           "description": "Properties whose units to keep, e.g. temperature, pressure, conditions."}}},
    },
    {
        "name": "scale_trace_ai",
        "description": "Read the TRACE-AI publication checklist (agentic catalysis: data, models, workflow, executable "
                       "artifacts; Xin et al., Chem Catal. 2026) on a document's records, or on one source's: for each "
                       "item, whether the records support it, partly, not at all (a gap), whether it applies, or what "
                       "a person checks in the source, with the records and evidence found and what to look at (a "
                       "random split, a metric without its uncertainty); and how the records were curated (the "
                       "autonomy level of their curation, the checks people made, precision and recall). It decides "
                       "nothing about the source: its authors fill the checklist. Writes nothing.",
        "input_schema": {"type": "object", "properties": {**_DOC, **_DOMAIN,
            "source": {"type": "string", "description": "Only the records of this Source (its id)."},
            "format": {"type": "string", "enum": ["json", "markdown"], "description": "markdown adds a readable rendering."}}},
    },
    {
        "name": "scale_recipe",
        "description": "Read each preparation of a document (or one) as a recipe: its stages and steps in order, each "
                       "with its conditions (temperature, time, ramp, gas and whether it flowed, pH, stirring, fill "
                       "fraction, cooling) and its inputs (function, amount, concentration, share, ratio, flow), what "
                       "the checklist asks of a step and does not find, the preparations it is a variant of, and a "
                       "summary read from the steps: the highest temperature and the time stated, grouped by unit and "
                       "never converted, the steps that state no time, the inputs by function, the gases and the pH. "
                       "The summary is not a record: cite the steps' evidence.",
        "input_schema": {"type": "object", "properties": {**_DOC, **_DOMAIN,
            "preparation": {"type": "string", "description": "Only this Preparation (its id)."},
            "format": {"type": "string", "enum": ["json", "markdown"], "description": "markdown adds a readable rendering."}}},
    },
    {
        "name": "scale_search",
        "description": "Search a document's evidence by words and meaning (its evidence index: the one saved beside it "
                       "by `scale graph index`, or built in memory from the document when none is); hits lead to the records that cite each passage. "
                       "Each hit is one quote: the other records that cite the same quote are in its `also`. The "
                       "result's access says where the hits may go: the whole document's, as scale_query's. With graphs "
                       "(several documents, each with an alias), each graph's evidence is searched on its own; hits say "
                       "their graph, are interleaved by their rank in their graph (scores of separate indexes are never "
                       "compared), k applies after federation, and the access is the intersection of the graphs' audiences.",
        "input_schema": {"type": "object", "required": ["query"], "properties": {
            "path": _DOC["path"], "query": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "default": 10},
            "graphs": {"type": "array", "description": "Search several documents, instead of path.",
                       "items": {"type": "object", "required": ["alias", "path"], "properties": {
                           "alias": {"type": "string"}, "path": _DOC["path"]}}},
            "mode": {"type": "string", "enum": ["hybrid", "vector", "keyword", "rrf"], "default": "hybrid"},
            "published_before": {"type": "string", "description": "Only evidence from sources published before this year."},
            **_VIEW}},
    },
    {
        "name": "scale_migrate",
        "description": "Move a document along the schema's version steps to a newer schema version (default: the "
                       "current one); record ids are kept. Returns the migrated document, the steps and rules "
                       "applied, and its validation. With save_as, writes it into the working directory.",
        "input_schema": {"type": "object", "properties": {**_DOC, **_DOMAIN,
            "to_version": {"type": "string", "description": "Target schema version (default: the current one)."},
            "save_as": {"type": "string", "description": "File name in the working directory (letters, digits, . _ -)."}}},
    },
    {
        "name": "scale_data_add",
        "description": "Compile a data file into records of a document in the working directory by a declarative mapping "
                       "(YAML, mapping_version 1 or 2; scale_data_match writes one): a Dataset with the file's sha256 "
                       "and table, records with stable ids, each value citing its cell (evidence.dataset_cell). Refuses "
                       "duplicate keys, conflicting run-level properties, unresolved references, malformed numbers, a "
                       "conflict with what the document holds and anything that would not validate; a retry changes "
                       "nothing. dry_run returns what would be written.",
        "input_schema": {"type": "object", "required": ["csv", "mapping", "into"], "properties": {
            "csv": {"type": "string", "description": "The data file: a CSV file (scale_csv_v1), or, by a version 2 "
                                                     "mapping's input block, a CSV file of another dialect or a sheet "
                                                     "of an .xlsx workbook; relative to the working directory, or "
                                                     "absolute."},
            "mapping": {"type": "string", "description": "The mapping (YAML): relative to the working directory, or "
                                                         "absolute."},
            "into": {"type": "string", "description": "The destination document's file name in the working directory "
                                                      "(letters, digits, . _ -); it exists and holds what the rows name."},
            "dry_run": {"type": "boolean", "description": "Write nothing; return the records and links it would add."}}},
    },
    {
        "name": "scale_data_match",
        "description": "Find the built-in mapping that fits a data file (a CSV file of any common dialect, or a sheet "
                       "of an .xlsx workbook): the templates of domain/data-mappings scored against its header and a "
                       "sample of its values, best first, each with the column each role binds, the wide columns it "
                       "melts into one result per column, their units, the uncertain bindings with their candidates "
                       "(never guessed silently), what the template assumes (confirm) and a complete mapping (YAML) for "
                       "scale_data_add. Deterministic. A file inside a data repository is gated by its access.",
        "input_schema": {"type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "description": "The data file: relative to the working directory, or absolute."},
            "sheet": {"type": "string", "description": "The workbook's sheet (default: every sheet is tried)."},
            "header_row": {"type": "integer", "description": "The header's row number, from 1 (default: found)."},
            "template": {"type": "string", "description": "Match this built-in mapping only."},
            "bind": {"type": "object", "additionalProperties": {"type": "string"},
                     "description": "Role -> column: settles an uncertain or missing binding."},
            "namespace": {"type": "string", "description": "The prefix of the ids the mapping makes (default lab)."},
            "origin": {"type": "string", "enum": ["recorded", "computed", "reported", "synthetic"],
                       "description": "The records' origin (default: the template's; recorded needs a recording time)."},
            "recorded_at": {"type": "string", "description": "When the data were recorded (ISO 8601 with a time zone)."},
            "top": {"type": "integer", "description": "How many mappings to return (default 3)."},
            "save_as": {"type": "string", "description": "Write the best mapping (YAML) to this file name in the working "
                                                         "directory (letters, digits, . _ -; it ends in .yaml)."}}},
    },
    {
        "name": "scale_add_source",
        "description": "Turn any source into records of a document (the add-source skill): a table (CSV, .xlsx) is "
                       "matched to a built-in mapping and imported, verified cell by cell, when nothing about it is "
                       "uncertain, else nothing is written and the questions come back, each with the argument that "
                       "settles it (ask the person; never guess); a publication (a PDF, a DOI, a text file) starts a "
                       "curation folder beside the document (curate it by the curate-corpus skill, then call again "
                       "with finish); a graph (.json) is validated and merged with merge. Refuses to write into the "
                       "SCALE framework repository, records the repository's readers may not read, and an id "
                       "conflict. Gated by the destination's access, like scale_data_add.",
        "input_schema": {"type": "object", "required": ["into"], "properties": {
            "source": {"type": "string", "description": "A file (relative to the working directory, or absolute) or "
                                                        "a DOI."},
            "into": {"type": "string", "description": "The destination document: a .json file in the working "
                                                      "directory or in a data repository (scale data init); "
                                                      "scale_data_new makes one."},
            "finish": {"type": "string", "description": "Instead of a source: a checked curation folder to fold into "
                                                        "the document."},
            "dry_run": {"type": "boolean", "description": "Say what would be written; write nothing."},
            "kind": {"type": "string", "enum": ["table", "document", "graph"], "description": "How to read the file "
                                                                                             "(default: detected)."},
            "sheet": {"type": "string"}, "header_row": {"type": "integer"}, "range": {"type": "string"},
            "recorded_at": {"type": "string", "description": "A table: when the data were recorded (ISO 8601 with "
                                                             "a time zone)."},
            "origin": {"type": "string", "enum": ["recorded", "computed", "reported"]},
            "bind": {"type": "object", "additionalProperties": {"type": "string"},
                     "description": "A table: role -> column, as a question asks."},
            "confirm": {"type": "array", "items": {"type": "string"},
                        "description": "A table: assumptions accepted (unit:<column>), as a question asks."},
            "template": {"type": "string"}, "namespace": {"type": "string"},
            "data_rights": {"type": "string", "enum": ["own", "open", "public_domain", "restricted", "in_copyright"]},
            "from_source": {"type": "string", "description": "A table: the Source (of the document) the data belong "
                                                             "to; the Dataset is REPORTED_IN it."},
            "locator": {"type": "string", "description": "With from_source: where (Table 2, SI section S3)."},
            "doi": {"type": "string", "description": "A document: its DOI, when the file is its PDF or text."},
            "email": {"type": "string"}, "source_kind": {"type": "string"},
            "merge": {"type": "boolean", "description": "A graph: merge it (default: validate only)."}}},
    },
    {
        "name": "scale_data_new",
        "description": "Write an empty, valid research_data document (the header pinning the schema version, its "
                       "stewardship and the Project and Institution records it names) to add sources to; inside a "
                       "data repository its policy's defaults.stewardship applies. Refused in the SCALE framework "
                       "repository.",
        "input_schema": {"type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "description": "The new .json file: in the working directory, or absolute "
                                                      "inside a data repository."},
            "project": {"type": "string"}, "contributor": {"type": "string"},
            "access": {"type": "string", "enum": ["public", "consortium", "project", "institution"]},
            "title": {"type": "string"}}},
    },
    {
        "name": "scale_data_verify",
        "description": "Check a document's tabular Datasets against local copies of their files: the sha256 of the bytes "
                       "and that each cited cell holds the recorded value. A Dataset whose file is not at hand is "
                       "unverified, never verified; nothing is fetched.",
        "input_schema": {"type": "object", "properties": {**_DOC,
            "files": {"type": "object", "additionalProperties": {"type": "string"},
                      "description": "Dataset id -> its local file (relative to the working directory, or absolute); "
                                     "default: a Dataset's uri when that is a local path."}}},
    },
    {
        "name": "scale_example",
        "description": "List the public synthetic example documents, or copy one into the working directory so it "
                       "can be validated and queried at once.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "An example's name from the list (default: list them)."}}},
    },
    {
        "name": "scale_skills",
        "description": "List the SCALE skills (versioned procedures: index-sources, fetch-corpus, curate-corpus, "
                       "query-graph, map-coverage, scan-conflicts, export-graph, evolve-schema, triage-feedback, "
                       "first-paper, upgrade-curation, add-source), or "
                       "return one "
                       "skill's text, or one of the reference files its text points to (e.g. curate-corpus's "
                       "references/recipes.md). curate-source and curate-dataset name curate-corpus.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "A skill's name (default: list them)."},
            "reference": {"type": "string", "description": "A file of the skill's folder as the skill lists it in "
                                                           "`references` (e.g. references/recipes.md)."}}},
    },
]

#: Skills merged into another one: the old name still finds the skill it went into (scale_skills, MCP prompts/get).
SKILL_ALIASES = {"curate-source": "curate-corpus", "curate-dataset": "curate-corpus"}

HUMAN_ONLY = {
    "approve a pull request (one human reviewer approves every change; governance/GOVERNANCE.md)":
        "gh pr review NUMBER --approve",
    "merge an approved pull request": "gh pr merge NUMBER --squash --delete-branch",
    "add a reviewer (a real person who agreed; no fixed number)": "edit governance/review-policy.yaml, core/CORE.yaml, "
                                                                  "domain/DOMAIN.yaml and every line of .github/CODEOWNERS",
    "move a data repository's documents to the latest schema": "scale kg update --data DIR",
    "build a release": "scale release build vX.Y.Z",
    "publish a release to PyPI (once the trusted publisher is configured)": "gh release create vX.Y.Z --repo trace-ai-org/scale",
    "register the PyPI trusted publisher for scale-catal": "pypi.org, project scale-catal: add the GitHub publisher "
                                                            "trace-ai-org/scale, workflow publish.yml, environment pypi "
                                                            "(governance/repository-settings.md)",
    "apply the repository settings (ruleset, environments, secrets)": "governance/repository-settings.md (gh api commands)",
    "file a GitHub issue": "scale gap issue GAP-....json --submit",
}


def _check_view(view) -> None:
    if view not in ("full", "model"):
        raise ScaleError(f"view is full or model, not {view!r}")


def template(domain: str | None, version: str) -> dict:
    """A minimal valid scale-catal document: header and stewardship, the institution and project the
    stewardship names, a Source, one evidenced Claim (a kernel label, valid with or without the catalysis checklist) and its
    REPORTED_IN link."""
    ev = {"source": "ex:source/1", "locator": "Results, paragraph 2", "kind": "text",
          "quote": "a short verbatim excerpt that states the claim"}
    return {
        "directed": True, "multigraph": True,
        "graph": {"schema": "scale-catal", "schema_version": version, "graph_id": "ex:my-graph",
                  "title": "State what was curated and what was left out (e.g. main text only; SI not examined).",
                  "data_status": "research_data", **({"follows_domains": [domain]} if domain else {}),
                  "stewardship": {"contributed_by": ["ex:org/my-lab"], "project": "ex:project/my-project",
                                  "access": "public"}},
        "nodes": [
            {"id": "ex:org/my-lab", "label": "Institution", "props": {"name": "My laboratory", "origin": "curated"}},
            {"id": "ex:project/my-project", "label": "Project", "props": {"name": "My project", "origin": "curated"}},
            {"id": "ex:source/1", "label": "Source",
             "props": {"name": "Author et al. 2025", "kind": "article", "title": "The article's title", "year": 2025,
                       "doi": "10.xxxx/xxxx", "origin": "curated"}},
            {"id": "ex:claim/1", "label": "Claim",
             "props": {"name": "The authors' main finding", "statement": "What the source states, in one sentence.",
                       "claim_kind": "finding", "claim_status": "proposed", "origin": "reported", "evidence": [ev]}},
        ],
        "links": [{"id": "ex:claim/1/reported-in/1", "type": "REPORTED_IN", "source": "ex:claim/1", "target": "ex:source/1",
                   "props": {"origin": "derived", "locators": ["Results, paragraph 2"]}}],
    }


def _file_key(st: os.stat_result) -> tuple:
    """What tells a file's versions apart for the toolkit's caches: its identity (device, inode), its change time
    (which copying a file's timestamps does not set) and its modification time and size. A replacement of the same
    size that keeps the modification time is still a new version."""
    return (st.st_dev, st.st_ino, st.st_ctime_ns, st.st_mtime_ns, st.st_size)


class ScaleToolkit:
    """SCALE tools for an agent. ``call(name, arguments)`` never raises; errors come back as data."""

    def __init__(self, workspace: str | os.PathLike | None = None, repo: str | os.PathLike | None = None,
                 egress=None):
        self.repo = Path(repo).resolve() if repo else find_repo_root()
        self.workspace = Path(workspace).expanduser().resolve() if workspace else default_workspace()
        # an egress gate (scale.egress.EgressGate) when the results go to a model: a tool refuses a document whose
        # records are not public unless the policy approves the gate's endpoint for their levels (None: no gate, the
        # caller keeps the results; the MCP and HTTP servers and `scale call` pass one)
        self.egress = egress
        self._specs: dict[str, tuple] = {}       # schema path -> (its source files' stat key, the schema)
        self._loaded: dict[str, dict] = {}      # documents by path, kept while their file is unchanged (_load)
        self._handlers: dict[str, Callable[..., dict]] = {t["name"]: getattr(self, "_" + t["name"]) for t in TOOLS}

    # ------------------------------------------------------------ tool schemas
    @staticmethod
    def schemas() -> list[dict]:
        """Neutral tool definitions: ``{name, description, parameters}`` with JSON Schema parameters."""
        return [{"name": t["name"], "description": t["description"], "parameters": t["input_schema"]} for t in TOOLS]

    @staticmethod
    def openapi(server_url: str = "http://127.0.0.1:8765") -> dict:
        """OpenAPI 3.1 description of the HTTP form (``scale serve``): ``POST /tools/{name}``."""
        paths = {"/tools": {"get": {"operationId": "list_tools", "summary": "List tool definitions",
                                    "responses": {"200": {"description": "Tool definitions (JSON Schema)."}}}}}
        for t in TOOLS:
            paths[f"/tools/{t['name']}"] = {"post": {
                "operationId": t["name"], "summary": t["description"].split(". ")[0],
                "description": t["description"],
                "requestBody": {"required": False, "content": {"application/json": {"schema": t["input_schema"]}}},
                "responses": {"200": {"description": "Tool result (JSON). Errors are returned as {\"error\": ...}.",
                                      "content": {"application/json": {"schema": {"type": "object"}}}}}}}
        return {"openapi": "3.1.0",
                "info": {"title": "SCALE tools", "version": __version__,
                         "description": "SCALE — Shared Concepts, Attributes, and Links with Evidence — agent tools."},
                "servers": [{"url": server_url}], "paths": paths}

    def call(self, name: str, arguments: dict | None = None) -> dict:
        name = name if name.startswith("scale_") else f"scale_{name}"
        handler = self._handlers.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}", "available": list(self._handlers)}
        try:
            if self.egress is None:
                return handler(**(arguments or {}))
            checked = self._egress_check(name, arguments or {})
            # every graph a call across graphs reads is checked as a single document is (docs/querying.md)
            graphs = (arguments or {}).get("graphs")
            many = [self._egress_check(name, {"document": g["document"]} if isinstance(g.get("document"), dict)
                                       else {"path": g.get("path")})
                    for g in graphs if isinstance(g, dict)] if isinstance(graphs, list) else []
            if isinstance(graphs, list) and any(not isinstance(g, dict) or not (isinstance(g.get("document"), dict)
                                                                               or g.get("path")) for g in graphs):
                from .egress import EgressDenied
                raise EgressDenied("egress denied: a graph of the call names no document or path to check; nothing "
                                   "was sent")
            out = handler(**(arguments or {}))
            # the result must come from the snapshot that was checked: a file replaced meanwhile is refused
            for c in [checked, *many]:
                if c is not None and self._version(c) != c[1]:
                    from .egress import EgressDenied
                    raise EgressDenied("egress denied: the document changed while the tool ran; nothing was returned")
            return out
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except ScaleError as exc:
            return {"error": str(exc)}
        except FileNotFoundError as exc:
            return {"error": f"file not found: {exc.filename}"}

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _version(checked: tuple) -> tuple:
        """The version key of a checked file now (every file of a graph kept as parts), to compare with the one
        checked."""
        p = checked[0]
        files = sorted(p.rglob("*.json")) if p.is_dir() else [p]
        return tuple((str(f), _file_key(f.stat())) for f in files if f.exists())

    def _egress_check(self, name: str, arguments: dict) -> tuple | None:
        """Refuse (scale.egress.EgressDenied, returned as an error) a tool whose result would carry records the gate's
        endpoint may not receive: the document given inline or by path, or the graph of a curation folder (a file or
        a graph kept as parts). A document that cannot be read is refused without its diagnostics, which may name
        its records. Returns the file checked and its version key (None for an inline document or none at all)."""
        from .egress import EgressDenied
        if not isinstance(arguments, dict):
            return None
        target = None
        if isinstance(arguments.get("document"), dict):
            self.egress.check(arguments["document"], "the document (inline)", self.workspace)
            return None
        if isinstance(arguments.get("path"), str) and arguments["path"]:
            target = self._path(arguments["path"])
        elif name == "scale_add_source":
            self._egress_add_source(arguments)
            return None                             # it writes the destination: what it returns is checked here
        elif name == "scale_data_add" and isinstance(arguments.get("into"), str) and arguments["into"]:
            target = self._path(arguments["into"])  # the records written there, and returned, take its access
        elif name == "scale_data_match" and isinstance(arguments.get("file"), str) and arguments["file"]:
            # a data file is no document: inside a data repository it carries the repository's default access (none
            # stated: institution), so its header and sample values go only to an endpoint approved for that
            from .audience import find_policy, parse_policy
            if not self._path(arguments["file"]).exists():
                raise FileNotFoundError(2, "no such file", str(self._path(arguments["file"])))
            try:
                f = self._path(arguments["file"]).resolve(strict=True)
                data = f.read_bytes()               # the bytes checked are the bytes matched (no swap in between)
                pf = find_policy(f)
                st = None
                if pf:
                    st = (parse_policy(load_yaml(pf), str(pf)).get("defaults") or {}).get("stewardship") or {}
            except (ScaleError, OSError, ValueError) as exc:
                raise EgressDenied("egress denied: the data repository's policy cannot be read to check the file's "
                                   "access; nothing was sent") from exc
            if st is not None:
                self.egress.check({"graph": {"stewardship": st}, "nodes": [], "links": []},
                                  f"the data file {arguments['file']} (data repository {pf.parent})", f)
            self._pinned = (arguments["file"], f, data)
            return None
        elif isinstance(arguments.get("folder"), str) and name in ("scale_source_check", "scale_source_finish"):
            from .curate import _folder
            try:
                _, _, target = _folder(self._path(arguments["folder"]))
            except (ScaleError, OSError, ValueError, KeyError) as exc:
                raise EgressDenied("egress denied: the curation folder cannot be read to check its access; nothing "
                                   "was sent") from exc
            if not target.exists():
                return None
        if target is None:
            return None
        if not target.exists():
            raise FileNotFoundError(2, "no such file", str(target))
        before = self._version((target,))
        try:
            doc = self._load(arguments["path"])["doc"] if "path" in arguments and arguments.get("path") else load_doc(target)
        except Exception as exc:                     # noqa: BLE001 - its text may name private records
            raise EgressDenied("egress denied: the document cannot be read to check its access; nothing was sent "
                               "(read it without a gate to see why)") from exc
        self.egress.check(doc, f"the document {arguments.get('path') or arguments.get('folder')}", target)
        return (target, before)

    def _egress_add_source(self, arguments: dict) -> None:
        """scale_add_source returns what it adds and reads: every document it draws from is checked (the destination,
        a graph to merge, a curation folder's graph), and a data file inside a data repository carries that
        repository's default access, as for scale_data_match. A document that cannot be read is refused."""
        from .audience import find_policy, parse_policy
        from .egress import EgressDenied
        docs: list[tuple[str, Path]] = []
        if isinstance(arguments.get("into"), str) and arguments["into"]:
            docs.append(("the destination", self._path(arguments["into"])))
        if isinstance(arguments.get("finish"), str) and arguments["finish"]:
            from .curate import _folder
            try:
                docs.append(("the curation folder's graph", _folder(self._path(arguments["finish"]))[2]))
            except (ScaleError, OSError, ValueError, KeyError) as exc:
                raise EgressDenied("egress denied: the curation folder cannot be read to check its access; nothing "
                                   "was sent") from exc
        src = arguments.get("source")
        sp = self._path(src) if isinstance(src, str) and src else None
        if sp is not None and sp.is_file():
            if sp.suffix.lower() == ".json":
                docs.append(("the graph to add", sp))
            try:
                pf = find_policy(sp)
                st = (parse_policy(load_yaml(pf), str(pf)).get("defaults") or {}).get("stewardship") or {} if pf else None
            except (ScaleError, OSError, ValueError) as exc:
                raise EgressDenied("egress denied: the data repository's policy cannot be read to check the file's "
                                   "access; nothing was sent") from exc
            if st is not None:
                self.egress.check({"graph": {"stewardship": st}, "nodes": [], "links": []},
                                  f"the file {src} (data repository {pf.parent})", sp)
        for what, p in docs:
            if not p.exists():
                continue                            # the tool says what is missing; nothing is read
            try:
                doc = load_doc(p)
            except Exception as exc:                 # noqa: BLE001 - its text may name private records
                raise EgressDenied(f"egress denied: {what} cannot be read to check its access; nothing was sent") \
                    from exc
            self.egress.check(doc if isinstance(doc, dict) else {}, f"{what} {p.name}", p)

    def _path(self, path: str) -> Path:
        """A document path: absolute as given, else relative to the working directory."""
        p = Path(path).expanduser()
        return p if p.is_absolute() else self.workspace / p

    def _doc(self, document: dict | None, path: str | None) -> dict:
        if document is not None:
            if not isinstance(document, dict):
                raise ScaleError("document must be a JSON object (a scale-catal document)")
            return document
        if path:
            return load_doc(self._path(path))
        raise ScaleError("give a document (inline) or a path")

    def _load(self, path: str) -> dict:
        """The document at ``path`` with what its queries and recipes reuse: ``{doc, graphs (a query Graph per schema),
        recipes (a recipe context per schema), index}``,
        kept while the file is unchanged (:func:`_file_key`: its identity, change and modification times and size; a
        long-running server loads a large graph and indexes it once). The tools never change a loaded document; a few recent documents are kept."""
        p = self._path(path).resolve()
        st = p.stat()
        key = _file_key(st)
        hit = self._loaded.get(str(p))
        if hit is None or hit["key"] != key:
            hit = {"key": key, "doc": load_doc(p), "graphs": {}, "recipes": {}, "index": None, "index_key": None}
            self._loaded.pop(str(p), None)
            self._loaded[str(p)] = hit
            while len(self._loaded) > 4:
                self._loaded.pop(next(iter(self._loaded)))
        return hit

    def _manifest(self, domain: str | None, doc: dict | None = None) -> Path:
        """The schema manifest: the catalysis domain's (by its name, or by default), or the kernel alone for ``core``."""
        from . import pgraph
        if domain == "core":
            return self.repo / layout.KERNEL_ONLY_MANIFEST
        if domain:
            mans = layout.graph_manifests(self.repo, experimental=True)
            if domain not in mans:
                raise ScaleError(f"no scale-catal schema for {domain!r}; use {', '.join(mans)} (or core)")
            return mans[domain]
        return pgraph.spec_path(self.repo, None, doc)

    def _spec(self, manifest: Path, version: str | None = None, doc: dict | None = None) -> dict:
        from . import pgraph
        from .pgraph_schema import spec_sources
        p = pgraph.pinned(manifest, version, doc)
        # kept while none of the files the schema is read from changes (a long-running server sees a new head)
        key = tuple((str(f), _file_key(f.stat()) if f.exists() else None) for f in spec_sources(p))
        hit = self._specs.get(str(p))
        if hit is None or hit[0] != key:
            hit = self._specs[str(p)] = (key, pgraph.load_spec(p))
        return hit[1]

    def _doc_spec(self, doc: dict, domain: str | None) -> dict:
        """The schema a document pins (a version this repository does not hold is an error that says so)."""
        return self._spec(self._manifest(domain, doc), (doc.get("graph") or {}).get("schema_version"), doc)

    def _examples(self) -> dict[str, Path]:
        """The public synthetic examples by name: the kernel's and the catalysis domain's documents whose
        ``data_status`` is ``synthetic_example`` (curated research data is not served here)."""
        out: dict[str, Path] = {}
        for root in layout.example_roots(self.repo):
            for p in sorted(root.rglob("*.json")):
                if layout.is_excluded(p.relative_to(self.repo)):
                    continue
                try:
                    g = load_doc(p).get("graph") or {}
                except Exception:
                    continue
                if g.get("schema") == "scale-catal" and g.get("data_status") == "synthetic_example" \
                        and (g.get("stewardship") or {}).get("access") == "public":    # missing: not public
                    out.setdefault(p.stem, p)
        return out

    def _skills(self) -> dict[str, dict]:
        from .releases import skill_files, skill_folder
        out = {}
        for p in skill_files(self.repo):
            text = p.read_text(encoding="utf-8")
            fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
            import yaml
            meta = (yaml.safe_load(fm.group(1)) if fm else {}) or {}
            refs = {f.relative_to(p.parent).as_posix(): f for f in skill_folder(p)[1:]}
            out[meta.get("name", p.parent.name)] = {"version": str(meta.get("version")), "description": meta.get("description"),
                                                    "path": p.relative_to(self.repo).as_posix(), "text": text,
                                                    "references": refs}
        return out

    # ------------------------------------------------------------ tools
    def _scale_status(self) -> dict:
        from .releases import list_releases
        from .updates import tool_status
        inventory: dict[str, list] = {}
        inv_path = self.repo / layout.IMPLEMENTATION_STATUS
        if inv_path.exists():
            for c in load_yaml(inv_path)["capabilities"]:
                inventory.setdefault(c["status"], []).append(c["name"])
        rels = list_releases(self.repo)
        docs = sorted(p.name for p in self.workspace.glob("*.json")) if self.workspace.is_dir() else []
        mans = layout.graph_manifests(self.repo)
        version = sorted({str(load_yaml(m).get("version")) for m in mans.values()})
        return {
            "scale_version": __version__,
            "latest_release": rels[-1] if rels else None,
            "schema_version": version[0] if len(version) == 1 else version,
            "kernel": {"file": layout.KERNEL, "schema_version": version[0] if len(version) == 1 else version},
            "domains": self._scale_domains()["domains"],
            "workspace": str(self.workspace), "documents": docs,
            "skills": {k: v["version"] for k, v in self._skills().items()},
            "capabilities": inventory,
            "human_only_actions": HUMAN_ONLY,
            "updates": tool_status(self.workspace / ".cache" if self.workspace.is_dir() else None),
            "next_steps": ["scale_skills curate-corpus (curating) or query-graph (asking)",
                           "scale_describe <Label> --domain before writing records or plans",
                           "scale_template for a valid starting document; scale_validate after every edit",
                           "scale_example to try scale_query on a public example"],
        }

    def _scale_domains(self) -> dict:
        from .domains import list_domains
        mans = layout.graph_manifests(self.repo, experimental=True)
        rows = []
        for r in list_domains(self.repo):
            man = mans.get(r["domain"])
            g = load_yaml(man) if man and man.is_file() else {}
            frozen = sorted(p.stem.removeprefix("scale-catal-") for p in layout.frozen_graph_dir(man).glob("scale-catal-*.yaml")) \
                if man else []
            plans = {}
            for q in sorted((self.repo / r["path"] / layout.DOMAIN_COMPETENCY).glob("*.yaml")):
                cq = load_yaml(q) or {}
                if isinstance(cq, dict) and cq.get("plan"):
                    plans[str(cq.get("id"))] = {"question": cq.get("question"), "documents": cq.get("documents"),
                                                "plan": cq["plan"]}
            rows.append({**{k: r[k] for k in ("domain", "title", "status", "version", "team", "stewards")},
                         "schema": {"manifest": man.relative_to(self.repo).as_posix(), "version": g.get("version"),
                                    "frozen_versions": frozen} if man else None,
                         "example_plans": plans})
        return {"domains": rows, "examples": sorted(self._examples())}

    def _mapping_index(self) -> dict | None:
        """SCALE's terms mapped to ontology terms, by subject (docs/mappings.md); none without the registry."""
        from . import mappings
        try:
            return mappings.index(self.repo) if (self.repo / layout.ONTOLOGY_REGISTRY).is_file() else None
        except ScaleError:
            return None

    def _scale_describe(self, names: list[str] | None = None, domain: str | None = None,
                        schema_version: str | None = None, checklist: bool = True) -> dict:
        from . import pgraph
        if not isinstance(checklist, bool):
            raise ScaleError(f"checklist is true or false, not {checklist!r}")
        hint = {"graph": {"follows_domains": [domain]}} if domain and domain != "core" else None
        spec = self._spec(self._manifest(domain, hint), schema_version)
        profile = domain if domain and domain in (spec.get("profiles") or {}) else None
        if names:
            out = pgraph.describe(spec, [names] if isinstance(names, str) else list(names), profile, self._mapping_index())
            if not checklist and "checklist" in out:
                del out["checklist"]
                out["checklist_left_out"] = "checklist: true adds what a record must and should report (for curating)"
            return out
        first = lambda t: (t or "").strip().split("\n")[0]   # noqa: E731
        return copy.deepcopy({"schema_version": spec["version"], "domain": domain,
                "labels": {n: {"is_a": d.get("ancestors") or [], "description": first(d.get("description"))}
                           for n, d in sorted(spec["nodes"].items())},
                "edges": {n: {"from": d.get("from"), "to": d.get("to"), "description": first(d.get("description"))}
                          for n, d in sorted(spec["edges"].items())},
                "checklists": sorted(spec.get("profiles") or {}),
                "value_fields": sorted(spec["value"]["fields"]), "evidence_fields": sorted(spec["evidence"]["fields"]),
                "next": "scale_describe with names (and domain) for properties and the checklist items"})

    def _scale_template(self, domain: str | None = None) -> dict:
        from . import pgraph
        man = self._manifest(domain)
        spec = self._spec(man)
        name = None if domain == "core" else (domain or next((n for n, m in layout.graph_manifests(self.repo).items()
                                                               if m == man), None))
        doc = template(name if name in (spec.get("profiles") or {}) else None, spec["version"])
        # inside a data repository (the working directory's, else the current directory's) the template states the
        # stewardship its policy gives new documents, and names the project and institutions that stewardship does
        from .datarepo import repository_defaults
        pf, dflt = repository_defaults(self.workspace)
        if pf is None:
            pf, dflt = repository_defaults(Path.cwd())
        stewarded = None
        if pf is not None and dflt:
            st = {**doc["graph"]["stewardship"], **{k: v for k, v in dflt.items() if v not in (None, "", [])}}
            doc["graph"]["stewardship"] = st
            names = {"ex:org/my-lab": st["contributed_by"][0], "ex:project/my-project": st["project"]}
            doc["nodes"] = [{**n, "id": names.get(n["id"], n["id"])} for n in doc["nodes"]]
            doc["nodes"][1:1] = [{"id": i, "label": "Institution", "props": {"name": i.rsplit("/", 1)[-1],
                                                                               "origin": "curated"}}
                                 for i in st["contributed_by"][1:]]
            stewarded = str(pf)
        rep = pgraph.validate(doc, spec).as_dict()
        return {"document": doc, "valid": rep["valid"], "schema_version": spec["version"],
                **({"stewardship_from": stewarded} if stewarded else {}),
                "notes": ["Replace every ex: id with your own prefix (a CURIE: prefix:path); ids are unique across nodes and links.",
                          "Each record that states something from a source carries evidence: source, locator, kind and a short "
                          "verbatim quote; derive REPORTED_IN links from the evidence.",
                          "Record values as {status, value, unit} as reported; declare unknown values with a status "
                          "(not_reported, with missing_reason not_curated if you did not look) instead of filling them in.",
                          "Context the schema cannot express goes in the record's unmodeled list (a gap), never in an "
                          "invented property.",
                          "A research_data graph names its stewardship (contributed_by, project, access).",
                          "access public: a document meant for the repository is public (scale check requires it); project "
                          "data not yet cleared for publication takes access project and stays outside the repository.",
                          "scale_describe <Label> with domain lists the properties and the checklist items to report."]}

    def _scale_validate(self, document: dict | None = None, path: str | None = None, domain: str | None = None,
                        source_texts: dict | None = None) -> dict:
        from . import pgraph
        doc = self._doc(document, path)
        spec = self._doc_spec(doc, domain)
        rep = pgraph.validate(doc, spec, source_texts).as_dict()
        return {"schema_version": spec["version"], **rep}

    def _scale_source_start(self, doi: str | None = None, pdf: str | None = None, text: str | None = None,
                            folder: str | None = None, version: str | None = None, email: str | None = None,
                            domain: str | None = "catalysis") -> dict:
        from . import curate
        slug = curate.slug_of(curate.normalize_doi(doi) if doi else None, Path(pdf or text) if (pdf or text) else None)
        return curate.start(self._path(folder or slug), doi=doi, pdf=self._path(pdf) if pdf else None,
                            text=self._path(text) if text else None, domain=None if domain == "core" else domain,
                            version=version, email=email, repo=self.repo)

    def _scale_source_check(self, folder: str, derive_links: bool = False) -> dict:
        from . import curate
        return curate.check(self._path(folder), self.repo, derive_links=derive_links)

    def _scale_source_finish(self, folder: str, public: bool = False, bundle: bool = False, title: str | None = None) -> dict:
        from . import curate
        return curate.finish(self._path(folder), public=public, bundle=bundle, title=title, repo=self.repo)

    def _members(self, graphs: Any, domain: str | None) -> list[dict]:
        """The graphs of a federated call (:mod:`federation`), each read as the single-document tools read it (a path
        cached while its file is unchanged), under the schema it pins; a graph that cannot be read, or pins a version
        the tools do not hold, is that graph's status, never the call's error."""
        from . import federation, pgraph_query
        if not isinstance(graphs, list) or not graphs:
            raise ScaleError("graphs is a list of {alias, path} or {alias, document}")
        recorded = federation.recorded_digests(self.repo)
        out = []
        for g in graphs:
            if not isinstance(g, dict) or set(g) - {"alias", "path", "document", "spec_sha256"} \
                    or ("path" in g) == ("document" in g):
                raise ScaleError("each graph is {alias, path} or {alias, document} (and optionally spec_sha256)")
            alias, path, hit = g.get("alias"), g.get("path"), None
            try:
                if path:
                    hit = self._load(path)
                    doc = hit["doc"]
                else:
                    doc = g["document"]
                if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), list) or not isinstance(doc.get("links"), list):
                    raise ScaleError("not a scale-catal document (node-link JSON with nodes and links)")
            except (ScaleError, OSError, ValueError) as exc:
                out.append(federation.member(alias, status="unavailable", error=str(exc), path=path))
                continue
            try:
                spec = self._doc_spec(doc, domain)
            except ScaleError as exc:
                out.append(federation.member(alias, doc=doc, status="unsupported_version", error=str(exc), path=path))
                continue
            graph = None
            if hit is not None:
                graph = hit["graphs"].get(id(spec))
                if graph is None:
                    graph = hit["graphs"][id(spec)] = pgraph_query.Graph(doc, spec)
            out.append(federation.member(alias, doc=doc, spec=spec, path=path, expect_spec=g.get("spec_sha256"),
                                         recorded=recorded, graph=graph,
                                         index=lambda path=path, doc=doc, hit=hit: self._index(path, doc, hit)))
        return out

    def _identity_map(self, identity_map: Any) -> dict | None:
        from . import federation
        if isinstance(identity_map, str):
            return federation.load_identity_map(self._path(identity_map))
        return federation.load_identity_map(identity_map)

    def _scale_query(self, plan: dict, document: dict | None = None, path: str | None = None, domain: str | None = None,
                     format: str = "json", view: str = "full", offset: int = 0, page_size: int | None = None,
                     page_chars: int | None = None, graphs: list | None = None, identity_map: Any = None) -> dict:
        from . import pgraph_query
        _check_view(view)
        if isinstance(plan, str):                     # a plan sent as JSON text
            try:
                plan = json.loads(plan)
            except ValueError:
                raise ScaleError("plan is a JSON object, not text that does not parse as one. A plan looks like "
                                 + pgraph_query.PLAN_EXAMPLE) from None
        pgraph_query.check_plan_shape(plan)          # a plan of the wrong shape: an error that says how to repair it
        if graphs is not None:                        # several graphs, never merged (docs/querying.md)
            from . import federation
            if document is not None or path:
                raise ScaleError("give graphs, or a document or path, not both")
            if view == "model":
                raise ScaleError("view model is not available for a query across graphs yet: omit view (the full "
                                 "result is returned)")
            res = federation.run(self._members(graphs, domain), plan, self._identity_map(identity_map))
            if format == "markdown":
                res["markdown"] = federation.render_markdown(res)
            return pgraph_query.detached(res)
        if identity_map is not None:
            raise ScaleError("identity_map identifies records across graphs: give graphs")
        res, doc = self._run_query(plan, document, path, domain)
        if view == "model":
            from .modelview import expand, pack_query
            out = pack_query(res, offset, page_size, doc, page_chars)
            if format == "markdown" and "error" not in out:     # the page's rows, as the page holds them
                out["markdown"] = pgraph_query.render_markdown(expand(out))
            return out
        if format == "markdown":
            res["markdown"] = pgraph_query.render_markdown(res)
        return res

    def _run_query(self, plan: dict, document: dict | None, path: str | None, domain: str | None) -> tuple[dict, dict]:
        from . import pgraph_query
        if document is None and path:
            hit = self._load(path)
            doc = hit["doc"]
            spec = self._doc_spec(doc, domain)
            g = hit["graphs"].get(id(spec))
            if g is None:
                g = hit["graphs"][id(spec)] = pgraph_query.Graph(doc, spec)
            res = pgraph_query.run(doc, spec, plan, self._index(path, doc, hit) if plan.get("text") else None, g)
        else:
            doc = self._doc(document, path)
            res = pgraph_query.run(doc, self._doc_spec(doc, domain), plan, self._index(path, doc) if plan.get("text") else None)
        return res, doc

    def _scale_stats(self, document: dict | None = None, path: str | None = None, labels: list[str] | None = None,
                     properties: list[str] | None = None) -> dict:
        from . import pgraph
        for k, v in (("labels", labels), ("properties", properties)):
            if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise ScaleError(f"{k} is a list of names, not {v!r}")
        doc = self._load(path)["doc"] if document is None and path else self._doc(document, path)
        return pgraph.stats(doc, labels, properties)

    def _scale_trace_ai(self, document: dict | None = None, path: str | None = None, domain: str | None = None,
                        source: str | None = None, format: str = "json") -> dict:
        from .traceai import assess, load_crosswalk, render_markdown
        doc = self._doc(document, path)
        rep = assess(doc, self._doc_spec(doc, domain), load_crosswalk(self.repo), source)
        if format == "markdown":
            rep["markdown"] = render_markdown(rep)
        return rep

    def _scale_recipe(self, document: dict | None = None, path: str | None = None, domain: str | None = None,
                      preparation: str | None = None, format: str = "json") -> dict:
        from .pgraph_query import detached
        from .recipes import recipe, recipe_context, render_markdown
        if document is None and path:
            # the loaded document, with what its recipes read made once per schema: a served graph is not read
            # again, nor its checklists run again, for each preparation; the answer shares nothing with it
            hit = self._load(path)
            doc = hit["doc"]
            spec = self._doc_spec(doc, domain)
            ctx = hit["recipes"].get(id(spec))
            if ctx is None:
                ctx = hit["recipes"][id(spec)] = recipe_context(doc, spec)
            res = detached(recipe(doc, spec, preparation, ctx))
        else:
            doc = self._doc(document, path)
            res = recipe(doc, self._doc_spec(doc, domain), preparation)
        if format == "markdown":
            res["markdown"] = render_markdown(res)
        return res

    def _index(self, path: str | None, doc: dict, hit: dict | None = None):
        """The evidence index beside ``path``, refused unless it is ``doc``'s, or built in memory from ``doc`` when none is
        saved there or the document came inline (:func:`passages.index_for`); kept with a loaded document (``hit``)
        while the saved index files are unchanged."""
        from .passages import Index, evidence_items, graph_sha256, index_for
        if not path:
            return Index.build(evidence_items(doc), graph_sha256=graph_sha256(doc))
        p = self._path(path)
        stem = p.with_name(p.name.removesuffix(".json").removesuffix(".graph") + ".evidence")
        if hit is None:
            return index_for(stem, doc)
        key = tuple(_file_key(f.stat()) if f.exists() else None
                    for f in (Path(f"{stem}{x}") for x in (".jsonl", ".i8", ".terms")))
        if hit["index"] is None or hit["index_key"] != key:
            hit["index"], hit["index_key"] = index_for(stem, doc), key
        return hit["index"]

    def _scale_search(self, query: str, path: str | None = None, k: int = 10, mode: str = "hybrid",
                      published_before: str | None = None, graphs: list | None = None, view: str = "full") -> dict:
        _check_view(view)
        from . import pgraph
        from .pgraph_query import document_access
        if graphs is not None:
            from . import federation
            if path:
                raise ScaleError("give graphs or a path, not both")
            if any(not isinstance(g, dict) or "document" in g for g in graphs):
                raise ScaleError("each graph to search is {alias, path}: its evidence index is the one beside it")
            if view == "model":
                raise ScaleError("view model is not available for a search across graphs yet: omit view")
            return federation.search(self._members(graphs, None), query, k=k, mode=mode, published_before=published_before)
        if not path:
            raise ScaleError("give a path (or graphs)")
        hit = self._load(path)
        doc = hit["doc"]
        ix = self._index(path, doc, hit)
        nodes = {n["id"]: n for n in doc["nodes"]}
        links = {e["id"]: e for e in doc["links"]}
        years = {n["id"]: n["props"].get("year") for n in doc["nodes"] if n["label"] == "Source"}
        before = int(str(published_before)[:4]) if published_before else None
        where = (lambda it: years.get(it.get("source")) is not None and years[it["source"]] < before) if before else None
        # one hit per quote of a source, every other citation of the same text in its `also` (passages.search_quotes)
        def name_of(rid):
            rec = nodes.get(rid) or links.get(rid)
            return (rec or {}).get("props", {}).get("name") or (rec or {}).get("type")
        from .passages import search_quotes
        hits = search_quotes(ix, query, k=k, mode=mode, where=where, name_of=name_of)
        for h in hits:
            h["source_year"] = years.get(h.get("source"))
        # the hits are the document's verbatim quotes: they go only where the whole document may go (scale_query's access)
        res = {"query": query, "mode": mode, "embedder": ix.embedder_id, "hits": hits,
               "access": document_access(doc, pgraph.embargo_today())}
        if view == "model":
            from .modelview import pack_search
            return pack_search(res, doc)
        return res

    def _scale_migrate(self, document: dict | None = None, path: str | None = None, domain: str | None = None,
                       to_version: str | None = None, save_as: str | None = None) -> dict:
        from . import pgraph
        from .pgraph_migrate import STEPS, raise_version
        doc = self._doc(document, path)
        man = self._manifest(domain, doc)
        target = to_version or load_yaml(man).get("version")
        spec = self._spec(man, target)
        have = (doc.get("graph") or {}).get("schema_version")
        if have == target:
            return {"error": f"the document is already at scale-catal {have}"}
        if have not in STEPS:
            return {"error": f"the document pins scale-catal {have}; migrations run along the steps {sorted(STEPS) or '(none yet)'}"}
        new, rep = raise_version(doc, target, spec)
        rep["validation"] = pgraph.validate(new, spec).as_dict()["summary"]
        written = None
        if save_as:
            if not _NAME.match(save_as):
                return {"error": "save_as must be 1-80 characters: letters, digits, '.', '_' or '-'"}
            target_path = self.workspace / (save_as if save_as.endswith(".json") else f"{save_as}.json")
            write_canonical(target_path, new)
            written = str(target_path)
        return {"from_version": have, "to_version": target, **rep, "written": written, "document": new}

    def _scale_data_add(self, csv: str, mapping: str, into: str, dry_run: bool = False) -> dict:
        from . import dataingest
        if not _NAME.match(into):
            return {"error": "into is a file name in the working directory: 1-80 characters, letters, digits, '.', '_' "
                             "or '-'"}
        target = self.workspace / (into if into.endswith(".json") else f"{into}.json")
        return dataingest.add(self._path(csv), self._path(mapping), target, repo=self.repo, dry_run=dry_run)

    def _scale_data_match(self, file: str, sheet: str | None = None, header_row: int | None = None,
                          template: str | None = None, bind: dict | None = None, namespace: str = "lab",
                          origin: str | None = None, recorded_at: str | None = None, top: int = 3,
                          save_as: str | None = None) -> dict:
        from . import datamatch, pgraph
        if bind is not None and (not isinstance(bind, dict) or not all(isinstance(v, str) for v in bind.values())):
            return {"error": "bind maps a role to a column name"}
        if save_as is not None and (not _NAME.match(save_as) or not save_as.endswith((".yaml", ".yml"))):
            return {"error": "save_as is a file name in the working directory (letters, digits, '.', '_' or '-') ending "
                             "in .yaml"}
        p = self._path(file)
        pinned, self._pinned = getattr(self, "_pinned", None), None
        if self.egress is not None:                 # read nothing the gate did not check: the bytes it read
            if not pinned or pinned[0] != file:
                return {"error": "egress denied: the file was not checked; nothing was sent"}
            data = pinned[2]
        else:
            data = p.read_bytes()
        spec = pgraph.load_spec(self.repo / layout.DOMAIN / layout.DOMAIN_GRAPH_MANIFEST)
        res = datamatch.match(data, p.name, datamatch.load_templates(self.repo), spec, sheet=sheet,
                              header_row=header_row, template=template, bind=bind or {}, namespace=namespace,
                              origin=origin, recorded_at=recorded_at, top=top)
        res["written"] = None
        if save_as and res["candidates"]:
            dest = self.workspace / save_as
            datamatch.save(dest, res["candidates"][0]["mapping_yaml"])
            res["written"] = str(dest)
        for c in res["candidates"]:
            c.pop("mapping", None)                  # the YAML says it, once
        return res

    def _writable(self, path: str, what: str) -> Path:
        """A document a tool writes: in the working directory, or in a data repository (``scale data init``); never in
        the SCALE framework repository."""
        from .audience import find_policy
        from .datarepo import refuse_framework
        p = self._path(path).resolve()
        refuse_framework(p, what)
        if not (p.is_relative_to(self.workspace) or find_policy(p) is not None):
            raise ScaleError(f"{path}: a tool writes documents only in the working directory ({self.workspace}) or in a "
                             "data repository (scale data init)")
        return p

    def _scale_add_source(self, into: str, source: str | None = None, finish: str | None = None, **opts) -> dict:
        from . import addsource
        known = {"dry_run", "kind", "sheet", "header_row", "range", "recorded_at", "origin", "bind", "confirm", "template",
                 "namespace", "data_rights", "from_source", "locator", "doi", "email", "source_kind", "merge"}
        if set(opts) - known:
            return {"error": f"unknown arguments {sorted(set(opts) - known)}"}
        if opts.get("bind") is not None and (not isinstance(opts["bind"], dict)
                                             or not all(isinstance(v, str) for v in opts["bind"].values())):
            return {"error": "bind maps a role to a column name"}
        target = self._writable(into, "a source added to a graph of project data")
        if source and self._path(source).exists():
            source = str(self._path(source))
        return addsource.add(source, target, repo=self.repo,
                             finish_folder=str(self._path(finish)) if finish else None, **opts)

    def _scale_data_new(self, file: str, project: str | None = None, contributor: str | None = None,
                        access: str | None = None, title: str | None = None) -> dict:
        from .datarepo import new_document
        return new_document(self._writable(file, "a document of project data"), project=project,
                            contributor=contributor, access=access, title=title, repo=self.repo)

    def _scale_data_verify(self, document: dict | None = None, path: str | None = None,
                           files: dict | None = None) -> dict:
        from . import datatable
        doc = self._doc(document, path)
        base = self._path(path).resolve().parent if path else self.workspace
        return datatable.verify(doc, {k: self._path(v) for k, v in (files or {}).items()}, base)

    def _scale_example(self, name: str | None = None) -> dict:
        ex = self._examples()
        if not name:
            return {"examples": [{"name": n, "path": p.relative_to(self.repo).as_posix(),
                                  "title": (load_doc(p).get("graph") or {}).get("title")} for n, p in ex.items()]}
        if name not in ex:
            return {"error": f"no public example {name!r}", "available": sorted(ex)}
        self.workspace.mkdir(parents=True, exist_ok=True)
        target = self.workspace / f"{name}.json"
        shutil.copy2(ex[name], target)
        return {"written": str(target), "path": target.name,
                "try": [{"tool": "scale_validate", "arguments": {"path": target.name}},
                        {"tool": "scale_query", "arguments": {"path": target.name,
                                                               "plan": {"match": {"label": "Source", "as": "source"},
                                                                        "return": ["source.name", "source.year"]}}}]}

    def _scale_skills(self, name: str | None = None, reference: str | None = None) -> dict:
        skills = self._skills()
        if not name:
            return {"skills": [{"name": n, "version": s["version"], "description": s["description"],
                                "references": sorted(s["references"])} for n, s in skills.items()]}
        renamed = {"renamed_from": name} if name not in skills and SKILL_ALIASES.get(name) in skills else {}
        name = SKILL_ALIASES[name] if renamed else name
        if name not in skills:
            return {"error": f"no skill {name!r}", "available": sorted(skills)}
        s = skills[name]
        if reference is not None:              # only a file the skill's folder holds, named as listed
            if reference not in s["references"]:
                return {"error": f"skill {name!r} has no reference {reference!r}", "available": sorted(s["references"])}
            return {"name": name, **renamed, "version": s["version"], "reference": reference,
                    "text": s["references"][reference].read_text(encoding="utf-8")}
        return {"name": name, **renamed, "version": s["version"], "text": s["text"], "references": sorted(s["references"])}


def tools(**kwargs) -> ScaleToolkit:
    """Shorthand: ``scale.api.tools()`` returns a ready toolkit."""
    return ScaleToolkit(**kwargs)
