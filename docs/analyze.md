# Plotting a graph's numbers

A chart is a query whose rows are drawn. `tools/plotdata.py` turns a plot spec into the points of a chart, typed as
reported, with the record each point comes from and its evidence; the graph explorer's **Analyze** view, the command
line and the agent tool `scale_plot_data` all read it, so a person, an agent and a script see the same points.

```bash
scale graph plot-data graph.json spec.yaml --csv points.csv     # the chart's data (JSON) and its points (CSV)
scale graph plot-data graph.json --suggest                      # charts worth starting from, each a ready spec
scale graph plot-data --graph a=one.json --graph b=two.json spec.yaml   # several graphs, never merged
scale data plot-table data.csv spec.yaml --graph-file graph.json --dataset ex:x/dataset/1   # a linked data file
scale call plot_data '{"path": "graph.json", "spec": {...}, "view": "model"}'                # an agent, in pages
```

## The plot spec

```yaml
plot: 1
title: Faradaic efficiency per catalyst
label: CatalyticTest            # the records a point is read from (the variable rec)
where: [{prop: driving_forces, op: contains, value: electro}]
x: catalyst                     # what the point is placed at
y: {entries: faradaic_efficiency}
series: product                 # colour
facet: test_kind                # small multiples within a panel
mark: point                     # point, line, bar or box
population: observations        # or all (an inventory: proposals and predictions counted too)
```

An axis (`x`, `y`) is one of:

- a **property path** of the record: `temperature`, `electrode_potential`, `results.methanol_yield`, `test_kind` (text
  is a category axis);
- a **linked record**: `catalyst`, `sample` or `source` (a category: its name), or one of its properties through that
  link: `sample.bet_area`, `catalyst.catalyst_family`, `source.year` (the source's publication year, not the year of
  the experiment);
- a **path of edges**: `{via: [{edge: USES_CATALYST, to: CatalystSample}, {edge: CHARACTERIZES, to: Characterization,
  direction: in}], path: results.surface_area}`: the value of a record one to six links away (`path` empty: the record
  itself, as a category);
- a **result entry**: `{entries: result_values, variable: ..., target: ..., kind: ...}` or `{entries:
  faradaic_efficiency}`, one entry per point, with its product, assay and evidence its own;
- on y, **count**: the records per x value (a bar per category or year, a histogram of a quantity).

`series` and `facet` take `product` (the y entry's target), `catalyst`, `sample`, `source`, `graph` or a property path.
Instead of `label` and `where`, `plan` gives a whole query plan (docs/querying.md); the axes then name its returned
paths (`t.temperature`, `fe`).

A spec that would lose what a value states is refused: a path to a field of a value object (`temperature.value`, which
drops its unit, bound and context) and a filter that converts (`convert: true`). A plan with a numeric filter runs as a
scientific comparison (`comparison: scientific`), so that a filter at −1.0 V selects potentials on a stated reference
scale only.

## What a chart keeps apart

- **Units.** A value is placed in the unit it states. Two units of one axis, `%` and `mol%` too, are two panels; nothing
  is converted. A value object with no unit is left out (`unit_unstated`).
- **Comparison contexts and contracts.** Each value is read under its own graph's quantity contracts as `comparison:
  scientific` reads it (`pgraph_query.Comparison.compared_as`): its kind, and the contexts its kind compares by (a
  potential's reference scale, a current density's normalization, a share's species, a pressure's convention). Two
  contexts, or two graphs whose schemas define the kind differently, are two panels. A context that is not stated, or
  stated two ways, and a unit the contracts cannot read as the kind's leave the value out (`missing_context`,
  `incompatible_context`, `incompatible_unit`). A value no contract reads is grouped by its unit and stated basis
  only, and its panel says its comparability is unknown.
- **Bounds and ranges.** A one-sided value (`bound`, a detection limit, `lower` or `upper` alone) is an open triangle
  pointing the way the value lies; a range a line from one end to the other (a box when both axes are ranges). Neither
  is a number on the axis, binned in a histogram or put in a box. A reported uncertainty or confidence interval is an
  error bar; the qualifier (`approximate`) is kept.
- **Pairs.** Two result entries of one record share a point only when one assay (`produced_by`) gave both; otherwise
  the pair is left out (`unpaired_entries`), never matched by position.
- **Observations.** By default a proposal, a prediction and a record a newer version REVISES are left out with their
  reason; `population: all` counts every record (an inventory).
- **Categories of records** keep the record's identity: two samples named alike are two categories (the name takes the
  end of the record's id).
- **Counts.** A count counts records (of the spec's label), once per x value within one axis typing (two products'
  efficiencies of one test are counted in their own panels): a test with two samples counts under each
  (`counted.in_more_than_one_x`). Counts, bins and boxes are per series and facet; a faceted count or box is drawn as
  one chart per facet. The buckets say the distinct sources behind each bar; histogram bins (`[lo, hi)`, a
  width of 1, 2 or 5 times a power of ten) and box statistics (quartiles by linear interpolation, whiskers from the
  minimum to the maximum value; bounds and ranges counted apart) are computed in Python, so the chart, its CSV and an
  agent read the same numbers.
- **What is left out** is counted by axis and reason (`left_out.by_reason`: `y:absent` for records that state no such
  value, `x:not_reported:not_stated_in_source`, `y:missing_context:species`, ...), after what each graph's query left out
  (`graphs[].query_left_out`).

The result names every graph (alias, digest, schema version, spec digest, status, `data_status`), the plan run, the
identity map's digest and the access: the intersection of the graphs' audiences. Synthetic examples are said to be.

## The Analyze view

The explorer's **Analyze** button opens a panel over the canvas (the inspector stays beside it):

- **Suggested charts**, from `--suggest`: per label, each outcome (conversion, selectivity, yield, Faradaic efficiency,
  rates) against each condition recorded with it (temperature, pressure, potential, space velocity, time) and the
  catalyst, ranked by the records whose values the chart would plot; and counts (records per source year, per a text
  property of a few values).
- **A builder**: records (any label), y (a count, a result entry or a numeric property), x (the record's own values or
  categories, or a value through its links, one or two links away either way), series, facet, mark, every record or
  observations only, and other graphs of the site to plot with it (each read on its own).
- **Charts**, one per panel (Vega-Lite, loaded when first needed, pinned and checked against its digest, with no
  external data). A click on a point selects its record in the graph and shows its evidence; a click on a bar lists its
  records. Each chart downloads as SVG or PNG; the data as CSV (record ids, units, kinds, contexts, sources, locators)
  or JSON (the whole result).

A reader's own model (Your API key) has a `plot` tool with the same spec: its chart appears under the answer, each point
linked to its record, and the model reads the chart's panels and points in pages. The egress gate applies to it as to
every other tool.

## A linked data file

A Dataset (or TimeSeries) record names its file (`uri`) and may state its `sha256`. In the explorer's inspector,
**Attach local file** reads a copy from the reader's computer, in the browser only: its sha256 is computed
(crypto.subtle) and compared with the record's (verified, mismatch, or no digest recorded), and its columns can be
plotted in the Analyze view. The file is kept in the tab's memory and sent nowhere (not to a model, nor the service).

Columns are read from the header: a unit in parentheses or brackets (`T (°C)`, `FE CO [%]`); a cell is a number, a bound
(`<0.5`, `>90`) or text; text in a column of numbers is reported with its rows (`left_out.not_a_number_rows`), never
plotted as a category; rows keep their number in the file (data rows from 1, after the header). The rows the graph's
records cite are ringed, and only when the file is the one the record names: an evidence item's `dataset_cell`
(`{row, column}`), when the schema has it, is firm (a solid ring); a locator naming a row of the Dataset ("rows 3-4") is
tentative (a dashed ring). On the command line `--dataset` checks the file against the Dataset's record and `--sha256`
against a digest you hold, never both.

## Limits

A path of edges reaches a linked record's properties, not its result entries or an edge's own properties (write a
query plan for those). A data file's units come from its header alone; its reference scales, normalizations and
uncertainty columns are not read. A line joins a series' values in x order to guide the eye; it says nothing of whether
the source ran them as one sweep. Nothing is averaged, fitted or converted.
