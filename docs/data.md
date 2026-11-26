# Tabular data: one link from a value to its cell

A value reaches the data that state it by one route, whether it was read from a paper's table, measured in a
laboratory or computed (scale-catal 0.8.4, provisional):

```
value.evidence_ids -> an evidence item with dataset_cell -> evidence.source, a Dataset with a table
```

The data stay outside the graph. A Dataset names them by `version` or `uri` and by the `sha256` of their exact bytes;
its `table` says how the bytes are read and what each column holds; an evidence item names one cell. New bytes are a
new Dataset record (a new id and version, linked to the old one by `UPDATES`), never a new checksum on an old record.

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

## Verifying: `scale data verify`

Given local copies of the files (`--file <dataset id>=<path>`, or a Dataset's `uri` when that is a path relative to
the document), it checks that each file's sha256 is the one recorded, that the table's rows and header are the
file's, and that each cited cell holds what the record says: the number of a numeric cell, compared exactly as
decimals; a token (`<0.5`) only for a censored value (a limit or a bound) that records it as reported, never for an
exact one; a missing value's blank or token; a qualifier (product, assay, phase, stream) or a text; a cell no value
cites against its record's own properties; and every quote against its cell. Which bound a censoring prefix means is
the mapping's, which verification does not read. A Dataset whose file is not at hand, or with a cited value it cannot
compare with its cell (a range), is `unverified`, never verified, and nothing is fetched. `--require-all` fails unless every Dataset is verified. The agent tools are `scale_data_add`
(into a document in the working directory) and `scale_data_verify`.

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

## Not yet

One dialect (`scale_csv_v1`); one record label and one result column per mapping (a second column is a second
mapping); product, assay and dataset columns hold record ids (no lookup from a name to an id); a mapping never creates
the records the rows name. Queries across several documents and a design-ready table for experiment-design agents are
later work.
