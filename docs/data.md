# Tabular data: one link from a value to its cell

A value reaches the data that state it by one route, whether it was read from a paper's table, measured in a
laboratory or computed (scale-catal 0.8.4, provisional):

```
value.evidence_ids -> an evidence item with dataset_cell -> evidence.source, a Dataset with a table
```

The data stay outside the graph. A Dataset names them by `version` or `uri` and by the `sha256` of their exact bytes;
its `table` says how the bytes are read and what each column holds; an evidence item names one cell. New bytes are a
new Dataset record (a new id and version, linked to the old one by `UPDATES`), never a new checksum on an old record.

## Your own data, from any source

One entry point turns what you have into records of your own graph: a publication, patent, thesis or report (a PDF,
a DOI or its text), a table (CSV, Excel) or a graph file. An agent does the same by asking, with the add-source skill
(`skills/add-source/SKILL.md`) and the tool `scale_add_source`.

**1. Install, and make a data repository of your own** (once; outside any other git repository, above all outside the
SCALE framework repository, whose readers are the public):

```bash
pip install "scale-catal[pdf]"              # [pdf]: reading PDFs (PyMuPDF; OCR needs tesseract)
scale data init ~/data/co2 --project lab:project/co2 --contributor lab:org/ni-group
```

It writes a private git repository with a policy (who reads it: here the project's members), the guard's hooks and
a starter document, `graphs/lab.json`, that states the policy's stewardship ([private-data.md](private-data.md)).
`scale data new graphs/<name>.json` makes another.

**2. Add sources:**

```bash
cd ~/data/co2
scale add-source ~/Downloads/fe-runs.xlsx --into graphs/lab.json            # a table
scale add-source ~/Downloads/fe-runs.xlsx --into graphs/lab.json --recorded-at 2026-03-02T10:00:00Z
scale add-source ~/papers/smith2025.pdf --into graphs/lab.json              # a publication (or a DOI, or its text)
scale add-source --finish work/sources/smith2025 --into graphs/lab.json     # once its graph is curated and checked
scale add-source colleague.json --into graphs/lab.json --merge              # a graph file
```

| the source | what `scale add-source` does |
|---|---|
| a table (`.csv` of any common dialect, `.xlsx`, text laid out in columns) | matches the built-in mappings ([below](#built-in-mappings-scale-data-match)); when nothing is uncertain it imports the table (`scale data add`), validates the document and checks every cited cell against the file (`scale data verify`); a file from outside the repository is copied into `raw/`, its mapping saved in `work/mappings/`, and the rights entry of its Datasets (whose cells the document quotes) written to `rights.yaml`: `--data-rights own` by default, the project's own data |
| uncertain | writes nothing and asks (exit 1): each **question** names what is unsure and the option that settles it, `--bind ROLE=COLUMN` (two columns fit, or one by a similar name), `--confirm unit:COLUMN` (a unit the header does not write), `--recorded-at` or `--origin reported` (when the data were recorded), `--sheet` (several sheets fit), `--template` (two kinds of table fit as well), `--confirm assumptions:TEMPLATE` (what the mapping records for every row: a control mode, a normalization, what a blank means), `--data-rights` (a publication's table, or any table given `--from-source`); cells no rule reads are fixed in the file. The notes (a header row found, decimal commas) are reported, never asked. Nothing is written, copied or saved until every question is answered and the cells are verified against the file |
| a publication (PDF, DOI, `.txt`) | starts a curation folder in the repository's `work/sources/<slug>/` (kept on the computer) with the document's stewardship (`scale source start`); an agent curates it by the curate-corpus skill and `scale source check` holds it to the schema, the page texts and the copyright cap; `--finish FOLDER` folds the graph into the document (records keep their ids, the Source's rights entry goes to `rights.yaml`, the result must validate). A DOI with no open-access copy under an open licence asks for the PDF (`--doi DOI` with the file) |
| a graph (`.json`) | validates it; `--merge` adds its records, refusing a record the document holds with other content, a graph of another schema version and records the repository's readers may not read; each record keeps its audience |
| a table of a publication | `--from-source SOURCE_ID --locator "Table 2"`: the Dataset cites the publication's Source and is REPORTED_IN it |

Each run says what it added, what the catalysis checklist still misses on the added records (a test's reaction, its
feed or electrolyte: for you to add when you know them, never typical values) and where the document is. **The
document in the repository is the durable copy**: commit it (`git add`, `git commit`; the hooks check what is
committed). A copy opened in the explorer, a browser tab or a chat is a working copy.

**3. Explore, ask, plot.**

- *Explore*: open [the graph explorer](https://trace-ai-org.github.io/scale/), **Open a graph or bundle**, and choose
  `graphs/lab.json`: the page reads the file in your browser (or add the sources there, below). A question to a model from there goes to that provider
  only when you approve it for the document's access ([private-data.md](private-data.md#egress-what-a-model-may-receive)).
- *Ask*: `scale graph query graphs/lab.json PLAN` (the query-graph skill writes the plan), and across graphs without
  merging them: `scale graph query --graph lab=graphs/lab.json --graph pub=catal-commons.json PLAN`
  ([querying.md](querying.md#querying-across-graphs)).
- *Plot*: `scale data matrix --graph lab=graphs/lab.json --plan PLAN --contract CONTRACT --out matrix/` writes a
  typed table (`matrix.csv`, with its manifest) to plot with any tool; nothing is converted, so plot one unit and
  basis at a time.

### In the graph explorer, with nothing to install: Add a source

[The graph explorer](https://trace-ai-org.github.io/scale/) has the same entry point as a page: **Add a source**
(the toolbar) takes up to 10 sources at once, of any mix (files dropped or chosen, DOIs), and adds them all to one
**private graph** in your browser, so that a topic is read together, by the code of `scale add-source` (`tools/pagelab.py`,
run in Pyodide by `site/worker.js`). What each file is, the Python router says (`addsource.detect`), so the page and
the command line agree. The batch is a list, each source with its kind (paper, table, graph) and its state (waiting,
needs your confirmation, curating, added, failed, skipped, needs your key), each removable before it runs; past 10, the
first 10 are kept and the others named. When it runs, the private graph (a new one or one you choose) is made once;
graphs and bundles merge first, with no question; each table is then shown for you to confirm, one at a time (or
skipped); then each paper is curated with your key, one after another ("paper 2 of 4"). One file's failure is that
file's; without a key the papers wait for one and the rest goes in. The batch ends with one summary: what each source
added (records, results, cited cells matched), what was skipped or failed and why, Explore and Analyze. A reload in the
middle of a batch keeps what was added and names the files that were not.

| the source | what the page does |
|---|---|
| a table (CSV, `.xlsx`) | matches the built-in mappings and shows how it reads the file for you to confirm: the kind of table (the template) and its score, each role with its column (a dropdown to re-bind it) and unit, the wide columns melted (one result per column), the sheet and header row, the questions (when the data were recorded, with a date-time input, or *from a publication*; each assumption of the mapping, ticked one by one; a unit the header does not write; the rights of a publication's table), and what is not read (columns, other sheets, rows after a blank row). **Add to the graph** stays grey until a dry run says *Ready* with the records and results it will add and the cells it checked against the file; then it adds them (`scale add-source`), validates, verifies every cited cell and shows the records, results, cells verified and checklist gaps, with **Explore**, **Analyze** and **Download** |
| a publication (PDF, text) | hands it to the in-browser curation (your own API key, the lawful-access statement); the curated graph can then be added to a private graph (*Add to a private graph*), its PDF never kept, so that the tables you add link to it (*Link it to a publication*: `--from-source`) |
| a DOI | says that a page cannot fetch a paper by its DOI and gives the command that does (`scale add-source DOI --into graph.json`) |
| a graph (`.json`), a bundle (`.zip`) | opens it, as **Open a graph or bundle** does; a graph can instead be merged into a private graph (`--merge`: refused on a conflict or another schema version) |

A new private graph is made by `scale data new` with the project, your lab and who may read it (access `institution`,
your lab only, unless you choose: never public by default). Each graph in the explorer's list carries one level, the
same badge, colour and definition everywhere (the list, its details, the Into picker):

| level | what it is |
|---|---|
| Catal-Commons | the shared catal-commons graph (`graphs/catal-commons`) |
| Example | a public example graph the site ships (the other `graphs/`, `examples/`, the domain's synthetic fixtures, which say so) |
| Personal | a public-access graph in this browser only: a paper you curated, a public graph you opened; you can contribute it |
| Team | not public, shared beyond your lab: access `project` or `consortium` |
| Private | not public, your lab only: access `institution` or `embargoed` |

A private graph's or an opened file's level is read from its document (the strictest access of its header and records,
as the egress gate reads it), never from where it came from; **Unsaved** is a state shown beside the level, not a level. **Private**: everything runs in the browser and nothing is uploaded; a
private graph is never contributed (it has no Contribute button); a model reads something only with your own key and
after the egress gate's approval ([private-data.md](private-data.md#egress-what-a-model-may-receive)). The optional
model help for a table's columns not read is off until you click it, and sends only those columns' headers and three
sample values each; its suggestions are only suggestions, which you choose in the table.

**Do not lose the work.** A private graph lives in the tab and in a working copy in the browser's storage
(IndexedDB), which a reload restores; a browser can clear that storage, so the page shows **Unsaved** on the graph
and above the list until you download it, asks before the tab closes, and prompts for **Download** after every
addition. The download is one zip, the durable copy:

```text
<id>/graph.json            the graph
<id>/manifest.json         the tool and schema versions (the spec's sha256), each source's sha256 and how it was
                           read (template, sheet, bindings, answers), and the commands that rebuild graph.json
<id>/rights.yaml           the rights of the sources it quotes
<id>/sources/              the files added (a publication: its curated graph and rights, never its PDF)
<id>/work/mappings/        the mapping each table was read with
<id>/README.txt
```

On the command line the same batch is `scale add-source a.pdf b.csv c.xlsx lab.json --into graph.json` (up to 10, each
on its own; the options apply to every source).

In an empty folder outside the SCALE repository, with `sources/` copied into it, the manifest's
`reproduce.commands` (`scale data new graph.json …`, then one `scale add-source …` per source, then
`scale graph validate` and `scale data verify`) write the same `graph.json`, byte for byte, with the same tool and
schema versions.

## From a laboratory's file to a graph

Nobody writes a mapping by hand to import a table. The repository carries reviewed mappings for the tables
laboratories keep ([built-in mappings](#built-in-mappings-scale-data-match)); `scale data match` finds the one that
fits a file, binds its columns and says what it is unsure of; a person (or an agent) settles only that:

```bash
scale data match results.xlsx                             # the best mapping, its bindings, what is uncertain
scale data match results.xlsx --sheet FE --header-row 4 \
    --bind potential="E_we (V vs RHE)" --recorded-at 2026-03-02T10:00:00Z --save results.map.yaml
scale data add results.xlsx --map results.map.yaml --into lab.json --dry-run   # what it would write
scale data add results.xlsx --map results.map.yaml --into lab.json
scale data verify lab.json                                # the bytes and every cited cell
```

1. **Match.** The file is read (a CSV file of any common dialect, or each sheet of an .xlsx workbook), its header row
   found and every built-in mapping scored against its header and a sample of its values. The best is printed with
   the column each role binds (`run <- 'Run'`), the wide columns it melts (`fe <- 'FE CO (%)' (CO), 'FE H2 (%)'
   (H2)`), each unit (read from the header, or assumed and said so), the columns it leaves out, what is
   **uncertain** and what the mapping **assumes** (`CONFIRM`).
2. **Settle what is uncertain.** A role two columns fit equally is left unbound (`?CHOOSE` in the mapping, which
   `scale data add` refuses) until `--bind ROLE=COLUMN` names one; a binding by a similar name, a unit the header does
   not write, a header row found rather than given, a sheet chosen among several, tokens in a numeric column
   (`n.d.`) that no rule names and a recording time are all listed. Nothing is guessed silently: a mapping is
   `complete` only when nothing blocks it.
3. **Save and import.** `--save` writes the mapping (`mapping_version: 2`, YAML, with the uncertain items and the
   assumptions as comments at its top). The same file serves every file of the same layout: the Dataset's id is made
   from the file's name and sha256 and the records' ids from the Dataset's, so the next week's file is imported by the
   same command.

An agent does the same with the tools `scale_data_match` (its `save_as` writes the mapping into the working
directory), `scale_data_add` and `scale_data_verify`; the browser page will call the same code (`scale.datainput`,
`scale.datamatch` and `scale.dataingest` are pure Python and the standard library, so they run in Pyodide).

## The schema

| addition | shape |
|---|---|
| `evidence.dataset_cell` | `column`, and the row by exactly one of `row_key` (a list of `{column, value}`, every primary-key column once, the value as the file writes it) and `row_index` (zero-based among the data rows, the header excluded) |
| `Dataset.table` | `profile` (`scale_csv_v1`), `primary_key` (column names), `columns` (each `{name, datatype, variable?, unit?, basis?}`, datatype one of string, integer, number, boolean, datetime), `rows?`, `ingest?` (what compiled it: mapping_sha256, tool, schema_version, schema_sha256) |

`scale_csv_v1` is UTF-8 without a byte-order mark, comma delimited, double-quote escaped, with one header row and no
skipped or comment rows; a header that names a column twice, a row of another width and an empty row are refused.
Another dialect is another profile.

The validator's rules (errors): a cell goes on evidence of kind `dataset_row` whose source is a Dataset with a table,
a version or uri and a 64-character hexadecimal sha256; a row key names every primary-key column once and no other; a
row index lies within the table's rows; the column is the table's. A numeric value that cites a cell of a number or
integer column is in the column's unit, of the column's variable and basis where both state one (nothing is
converted); a number column that a result entry cites, or that holds values of a variable, names a quantitative
Variable with a quantity_kind and a unit of one of its kind's dimensions. A document without cells is valid as
before.

## Importing a table: `scale data add`

```bash
scale data add co2rr-fe.csv --map co2rr-fe.map.yaml --into lab.json --dry-run   # what it would write
scale data add co2rr-fe.csv --map co2rr-fe.map.yaml --into lab.json             # write it
scale data verify lab.json                                                       # the bytes and every cited cell
```

The destination is an existing document that holds what the rows name: the Variables of the result columns, the
products, the assays, the reactions, the input datasets. The import is a deterministic compiler, not a curation: it
writes

- a Dataset for the file (`data_kind: table`, its sha256, version or uri, table and `ingest` digests);
- one record per record key, with a stable id (`id_prefix` and the key's values, percent-encoded), its origin, the
  properties the mapping names and one result entry per row;
- for every cell read, an evidence item (`kind: dataset_row`, the cell as `dataset_cell`, its text as `quote`), cited
  by the value it states: a result cites its number's cell and the cells of its product, assay, phase and stream;
- each record's `REPORTED_IN` link to the Dataset and the links the mapping names.

Before writing, it validates the result with the destination (quantity contracts and checklist included) and refuses
any error the destination did not already have. It reports structural validity, the checklist items the imported
records miss and the new warnings (an efficiency above 100 % is kept as recorded and warned about) apart. It writes
the document whole or not at all; a retry with the same file, mapping, tool and schema changes nothing; a record that
exists with other content is a conflict, never overwritten.

### The mapping (`mapping_version: 1`)

A YAML file with the keys below and no others (an unknown key anywhere is refused). It holds no code, no templates
and no URLs to fetch.

```yaml
mapping_version: 1
dataset:
  id: lab:co2rr/v1          # the Dataset record
  version: "1"              # version or uri (or both)
  uri: co2rr-fe.csv         # a path relative to the document lets `scale data verify` find the file
  profile: scale_csv_v1
  primary_key: [run_id, result_id]   # or row_selector: index (cells then name rows by position)
record:
  label: CatalyticTest      # or Computation, or another Experiment
  id_prefix: lab:co2rr/test/
  key: [run_id]             # one record per distinct key
  origin: recorded          # recorded needs recorded_at_column (or recorded_at); computed for calculations
  recorded_at_column: recorded_at
  properties:
    driving_forces: {literal: [electro]}
    electrode_potential: {column: potential_V, unit: V vs RHE}
    level_of_theory: {column: functional, representation: text_value}
    configuration: {ref_column: input_dataset_id}
    status_reason: {column: status_reason, missing: {"": omit}}
result:                     # one result entry per row
  id_column: result_id      # snake_case, unique in its record
  column: fe_percent
  variable: lab:fe          # a Variable whose quantity_kind gives the contract
  unit: "%"
  target_column: product_id # or target, or unresolved_target
  produced_by_column: assay_id   # or produced_by: <id>, or produced_by: self
  phase_column: phase       # or phase
  stream_column: stream     # or stream
  censor: [{prefix: "<", as: below_detection_limit}]   # or upper_bound, lower_bound, below_quantification_limit
  missing: {"": {status: not_measured}}                # not_reported (with missing_reason), not_measured, not_applicable
links:
  - {type: USES_CATALYST, target: lab:sample/ag-1, props: {state: as_prepared}}
  - {type: IN_ELECTROLYTE, target_column: electrolyte_id}
```

A property is read from a `literal`, a `column` or a `ref_column` (a reference). A value property read from a column
needs its `unit` (a number) or its `representation` (`text_value`, `boolean_value`); a property that is no value
object (an enumerated value, a text, a reference) is the cell's text. A property that differs between the rows of one
record is refused (a run-level conflict), and so are duplicate keys, a reference the destination does not hold, a cell
that is not a number as the profile writes one (`12,5`, `1 000`, `NaN`), a blank or token the mapping does not name,
a declared primary key that names two rows, and a cell that is not of its column's datatype (`dataset.columns`
declares one; a column read as a number is `number`). Evidence ids are made from the column and the whole key, each
value percent-encoded, so two rows never share one.
`missing` maps a token, the blank `""` included, to a missing value and `censor` a prefix to a bound or a limit; a
token never becomes a number, and a blank never zero. The cell's own text stays in `as_reported`. A property that is
no value object may map a token to `omit` (left out); a result never is.

Literature stays `reported`: importing a table from a paper does not make it first-hand. Such a table is a derived
Dataset whose provenance cites the paper; its values keep the paper's Source evidence as well.

### Version 2 (`mapping_version: 2`): workbooks, wide tables, header units, lookups, several records

Version 2 reads everything version 1 reads, and a version 1 file is read exactly as before (its records, ids and
digests are unchanged; a version 2 key in it is refused). `scale data match` writes version 2. What it adds:

```yaml
mapping_version: 2
input: {format: xlsx, sheet: FE, header_row: 4}   # or {format: csv, delimiter: ";", encoding: cp1252, decimal: ","}
dataset:
  id_prefix: "lab:data/"     # or id; with id_prefix the id is made from the file's name and sha256 (see below)
  primary_key: [Run]         # version and uri may go: the uri is then the file's path relative to the document
references:                  # records the rows use, created when the document has none ("@fe" names one)
  fe:
    id: lab:variable/faradaic-efficiency
    label: Variable
    props: {name: Faradaic efficiency, scale: quantitative, quantity_kind: faradaic_efficiency, unit: "%"}
    reuse: [quantity_kind]   # a document holding one Variable of this kind uses it; two are refused, never guessed
lookups:                     # a name in a cell (or a header) -> a record id
  species:
    label: ChemicalSpecies
    aliases: {formate: HCOO-, carbon monoxide: CO}
    values: {}               # text -> id, first
    match: [formula, name]   # then a record of the label whose formula or name is the text (one; two are refused)
    create: {id_prefix: "lab:species/", as: formula}   # else a new species from its formula (charge, composition)
  sample:
    label: CatalystSample
    match: [name]
    create: {id_prefix: "lab:sample/", as: name, props: {sample_kind: other}}
records:                     # one or more; each names its handle (as)
  - as: test
    label: CatalyticTest
    key: [Run]
    origin: recorded
    recorded_at: "2026-03-02T10:00:00Z"
    properties:
      electrode_potential: {column: "E (V vs RHE)", unit_from_header: true}
    results:
      - variable: "@fe"
        unit_from_header: true          # "FE CO (%)" -> %, checked by the quantity contract of the Variable's kind
        target_lookup: species
        melt:                           # a wide table: one entry per row and column
          - {column: "FE CO (%)", id: fe_co, target: CO}
          - {column: "FE H2 (%)", id: fe_h2, target: H2}
        censor: [{prefix: "<", as: below_detection_limit}]
        missing: {"": {status: not_measured}}
    links:
      - {type: USES_CATALYST, target_column: Electrode, target_lookup: sample}
  - as: xrd                             # another record of the same rows, here a Characterization of each sample
    label: Characterization
    key: [Sample]
    properties: {technique: {literal: X-ray diffraction}}
    results: [{id: crystallite_size, column: "d_XRD (nm)", variable: "@crystallite", unit_from_header: true}]
    links: [{type: EXAMINES, target_record: sample}]   # the record another record spec makes of the same row
```

- **Input.** `input` names how the file is read ([below](#reading-workbooks-and-csv-dialects)); without it the file is
  `scale_csv_v1`, as in version 1.
- **Melt.** A result reads one `column` or several (`melt`): each melted column gives every row one entry, with its
  own `id` (prefixed by the row's `id_column` cell when a record has several rows), `target` (through
  `target_lookup` when given), and optionally its `unit`, `definition`, `phase` and `stream`. A result reading one
  column names its entry's id by `id`, `id_column` or both (`id_slug: true` turns the id column's cells into
  snake_case).
- **Header units.** `unit_from_header: true` reads a unit written at the end of a header (`FE CO (%)`, `T [K]`,
  `T / °C`); a header without one, a unit the contracts cannot read for the quantity, or one of another dimension is
  refused.
- **Lookups.** `target_lookup` (a result's target column, or a melt's targets), `lookup` (a property's `ref_column`)
  and a link's `target_lookup` turn a name into an id: aliases first, then `values`, then the document's records by
  the `match` properties, then `create` when the lookup allows it (`as: formula`: a ChemicalSpecies with its formula,
  elemental composition and charge, refused when the formula reads two ways, as `NO3-`; `as: name`: a record of that
  name with the `props` given). The table records each name it resolved (`table.ingest`, `lookup_<n>`), so that
  verification reads the cell `formate` as the species it named.
- **More.** `basis_column` reads a result's basis from a cell (a TOF's site basis); `unresolved_target_column` keeps
  what a cell names that no record identifies (an adsorbate named in words, whose reaction the table does not state).
- **Several records.** `records` lists record specs read from the same rows, each with its own key, properties,
  results and links; `target_record` links one to the record another spec makes of the same row (a Characterization
  EXAMINES the CatalystSample). A record that states nothing from the table gives a `name_column`, whose cell backs
  its REPORTED_IN link.

## Built-in mappings: `scale data match`

The library is [`domain/data-mappings/`](../domain/data-mappings/): one reviewed template per kind of table, each with
its synthetic example in
[`domain/examples/scale-catal/data-mapping-examples/`](../domain/examples/scale-catal/data-mapping-examples/).

| template | a row is | roles (required in bold) | writes |
|---|---|---|---|
| `co2rr-faradaic-efficiency` | an electrolysis run | **run**, sample, recorded_at, **potential**, current_density, **fe** (wide: FE CO (%), FE_H2, ...), partial_j (wide: j CO (mA/cm2)) | a CatalyticTest (electro, potentiostatic) with one Faradaic efficiency and partial current density per product |
| `thermal-catalytic-test` | a test condition | **run**, sample, recorded_at, **temperature**, pressure, space_velocity (GHSV or WHSV by its unit), time_on_stream; one of conversion, selectivity, yield (wide: X CO2 (%), S CH4 (%), Y CH4 (%)) | a CatalyticTest (thermo) with its conditions and one result per species and quantity |
| `kinetics-rates` | a kinetic measurement | **run**, sample, recorded_at, **temperature**, pressure, species; one of rate, tof; site_basis | a CatalyticTest (kinetics) with a rate and a TOF whose basis is the site-basis cell |
| `dft-adsorption-energy` | a calculation | **calc**, surface, adsorbate, functional, **energy**, status, status_reason | a Computation (electronic_structure, the functional as level of theory) with its energy; a failed calculation's energy not measured |
| `characterization-summary` | a catalyst sample | **sample**, recorded_at; one of bet, pore_volume, crystallite, xps_ratio (wide: Ni/Al (XPS)) | a CatalystSample and, in one pass, an N2 physisorption, an X-ray diffraction and an XPS Characterization that EXAMINES it |

A template holds no code: its `roles` (each a `kind` — text, number, datetime or melt —, `synonyms` of its header, the
`patterns` of a wide role with a `target` group, the `quantity` a number holds and its `default_unit`), the groups
of which one must be found (`require_one_of`), what it assumes (`confirm`) and a version 2 mapping whose `{{...}}`
placeholders the bound columns fill (`when:` keeps a block only when its roles are bound). `scale data match --list`
shows the library.

**How a file is scored** (deterministic): a header's name, its unit set aside, against a role's synonyms — 1.0 for
the same words, 0.95 in another order, 0.75 with a word or two more, 0.6 x the string similarity when that is at least
0.85; times 0.2 when the values are not of the role's kind, times 0.7 when the header's unit cannot be read as the
role's quantity, 0 when it is of another dimension, +0.05 when it is of the right one. A wide role takes every column
whose header matches one of its patterns, its unit of the right dimension and its values numbers (a sum, `FE total`,
is left out, and so is a column a single-column role names as well: a `Sample` is never a selectivity). Roles are
bound best score first; a role whose best column has a rival within 0.05 is not bound. A template scores 0.75 x the mean score of its required roles and groups (0 for one not found) + 0.25 x the share of
the file's columns it binds. A workbook's sheets are tried each; the best mappings come first (`--top`).

**What is uncertain** (`uncertain`, each with its column or role and, for a binding, its candidates): a role bound by
a similar name (below 0.9), a role two columns fit (not bound), a required role with no column, a unit assumed, a
header unit not read, a header row found rather than given, a sheet chosen among several, rows after a blank row not
read, decimal commas read, and, blocking the mapping (`complete: false`), a column a wide role's pattern names whose
values or unit it cannot read, tokens no rule names in a numeric column, cells outside a role's values (a status),
times without a time zone, a required group with no column, and a recording time an origin `recorded` needs
(`--recorded-at`, or `--origin reported` for a table from a publication, `computed` for calculations). The ids it
makes start with `--namespace` (default `lab`).

## Reading workbooks and CSV dialects

`input` (version 2) reads an .xlsx workbook's sheet (`sheet`, default the first; `range`, an A1 range) or a CSV file
of any common dialect (`delimiter`, default detected among `,` `;` tab `|`; `encoding`, default by its byte-order mark,
else UTF-8, else Windows-1252; `decimal: ","` reads `12,5` as 12.5), from its `header_row` (from 1; the rows above are
a preamble). The data rows end at the first empty row; rows after it are counted and reported, never read; a cell
beyond the header's last named column, a header column without a name and a header that names a column twice are
refused. A workbook's numbers are written as the shortest decimal that reads back as the stored number (lossless), its
dates as ISO 8601 (by the cell's number format); formulas give their cached values, and a formula never calculated is
refused (never read as a blank). A cell formatted as a percentage stores 0.6 and shows 60%: it is read as the text
`60%`, which no rule reads as a number, or, with `percent: displayed` (which `scale data match` sets and reports when
it finds such cells), as the number 60, so that a value is never taken at a hundredth of its size. A sheet whose used
area exceeds 20 million cells, and a workbook that unzips to more than 256 MiB, are refused. The
reader (`scale.datainput`) is pure Python and the standard library: no spreadsheet library is needed, and none is an
extra.

The table read is written as `scale_csv_v1`, and the cells evidence names are cells of those canonical bytes. **No
schema change:** `Dataset.table.profile` stays `scale_csv_v1`, and the reading is recorded with fields that exist.
When the canonical bytes are the file's own (a `scale_csv_v1` file), there is one Dataset, as in version 1. Otherwise
there are two: the file's own Dataset (its sha256, its media type, its uri; with `id_prefix`, the prefix, the file's
name and its sha256's first 12 characters, else `<id>/file` or `file_id`) and the table's (with `id_prefix`, the
file's id and `/table-` and the canonical bytes' sha256's first 12, so that two sheets of one workbook are two tables
of one file; else `id`): `origin: derived`, the canonical bytes' sha256, its `table`, whose `ingest` records the
recipe — `input_format`, `input_sheet`, `input_header_row`, `input_range`, `input_delimiter`, `input_encoding`,
`input_decimal`, `input_percent`, `input_rows` — and `extracted_from` (the file's id); they are linked by `INCLUDES`
(the file includes the table). `scale data verify` checks the file's sha256, extracts the table again by the recipe
and checks the table's sha256 and every cited cell; a copy of the canonical table given with `--file <table
id>=table.csv` is verified as well, and the file given as its own Dataset's (`--file <file id>=book.xlsx`) is
checked by its bytes.

An id the import would make or reuse never takes another meaning: a reference whose id the document holds with other
properties (another quantity kind, scale or unit), and a record a lookup would create under an id the document holds
for another formula or name, are refused.

## Verifying: `scale data verify`

Given local copies of the files (`--file <dataset id>=<path>`, or a Dataset's `uri` when that is a path relative to
the document), it checks that each file's sha256 is the one recorded, that the table's rows and header are the
file's, and that each cited cell holds what the record says: the number of a numeric cell, compared exactly as
decimals; a token (`<0.5`) only for a censored value (a limit or a bound) that records it as reported, never for an
exact one; a missing value's blank or token; a qualifier (product, assay, phase, stream) or a text; a cell no value
cites against its record's own properties; and every quote against its cell. Which bound a censoring prefix means is
the mapping's, which verification does not read. A Dataset whose file is not at hand, or with a cited value it cannot
compare with its cell (a range), is `unverified`, never verified, and nothing is fetched. `--require-all` fails
unless every Dataset is verified. A table read from a workbook or another dialect is extracted again from its
original file ([above](#reading-workbooks-and-csv-dialects)); a cell a lookup resolved holds the id the table's
`ingest` records for its text. The agent tools are `scale_data_match` (a file in a data repository carries the
repository's default access, and the egress gate decides whether its header and sample values may go to the
endpoint), `scale_data_add` (into a document in the working directory) and `scale_data_verify`.

## Who sees it

A Dataset is a record with its own stewardship. A view for an audience that may not see it (`scale graph filter`, an
export) removes its evidence items, and each value they alone state is withheld in its place, as for any source. A
checksum or a cap on quotes is no confidentiality: a private table's cells quoted in a document go wherever the
document goes, so keep a private document private ([curation.md](curation.md), "Rights"); quotes from a Dataset count
toward its rights like any other source's. A kg-bundle carries neither `dataset_cell` nor `table`, and a record whose
only evidence is dataset cells names no publication and is not exported to one (it is counted); read such data from
the scale-catal document.

## Worked example

[`domain/examples/scale-catal/data-ingest-examples/`](../domain/examples/scale-catal/data-ingest-examples/) holds two
synthetic tables and their mappings: Faradaic efficiencies of CO2 electroreduction on Ag at three potentials
(`co2rr-fe.csv`: one formate efficiency below the detection limit, `<0.5`, and one not measured, a blank) into three
CatalyticTests, and CO and H adsorption energies on three surfaces (`dft-adsorption.csv`: the Au calculation failed,
its status and reason kept and its energies not measured) into three Computations. `seed.json` is the destination
before the import and `data-ingest.json` after both; `build_examples.py` rebuilds them. Its records are synthetic
(`origin: synthetic`, a synthetic example); a laboratory's mapping says `recorded` (or `computed`) and writes into its
own research document.

```bash
cd domain/examples/scale-catal/data-ingest-examples
cp seed.json /tmp/lab.json
scale data add co2rr-fe.csv --map co2rr-fe.map.yaml --into /tmp/lab.json
scale data add dft-adsorption.csv --map dft-adsorption.map.yaml --into /tmp/lab.json
scale data verify data-ingest.json
```

A query binds a result and returns the cells it cites (`evidence: true`): each item names its Dataset, its
`dataset_cell` and the cell's text ([querying.md](querying.md), "Results").

[`domain/examples/scale-catal/data-mapping-examples/`](../domain/examples/scale-catal/data-mapping-examples/) holds one
synthetic laboratory file per built-in mapping, the mapping `scale data match` saved for it (`*.map.yaml`), the
destination before the imports (`seed.json`, which already holds CO2 and a Faradaic-efficiency Variable, found by a
lookup and a reference) and after them (`data-mappings.json`): a CO2RR workbook (three sheets, a preamble, a wide table
with a sum column left out), a thermal test in a semicolon, decimal-comma, Windows-1252 file, kinetics with a TOF's
site basis, DFT energies with a failed calculation and a characterization summary written as a sample and three
Characterizations in one pass. All five match their template with no binding uncertain; the import still leaves the
checklist's gaps the tables cannot fill (a CatalyticTest's reaction, an electrolyte), reported by `scale data add`.

```bash
cd domain/examples/scale-catal/data-mapping-examples
scale data match co2rr-fe-wide.xlsx --origin synthetic --namespace ex-dm      # FE, its bindings and notes
scale data verify data-mappings.json                                         # the workbook's sheet extracted again
```

## Not yet

A mapping reads one table of one file (a second sheet is a second mapping) and the records a row names are those its
specs describe: a reaction, an electrolyte or a reactor that no column names is not added, and the checklist reports
it. A template binds a role to one column, and a wide role's columns to its pattern; a table whose header spans two
rows, or merged cells, is read from one header row (give `range` and `header_row`). The library covers five kinds of
table; a new kind is a new template in a reviewed pull request. Product and assay names are looked up, sample and
species records created; Variables are reused only when exactly one of their kind exists. Values are never
converted (a column in K and one in °C are two units, as recorded).
