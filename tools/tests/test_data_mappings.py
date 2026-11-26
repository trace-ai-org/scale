"""Built-in mappings: a laboratory's data file into a scale-catal document without writing a mapping.

The reader of workbooks and CSV dialects (scale.datainput), mapping_version 2 (an input block, melt, header units,
lookups, references, several records; scale.dataingest), the library of templates and the matcher ``scale data match``
(scale.datamatch), on the synthetic examples in domain/examples/scale-catal/data-mapping-examples/."""

import copy
import json
import shutil
import subprocess
import sys

import pytest
import yaml

from conftest import EXAMPLES, REPO, RUN_SCALE, spec_of
from scale import dataingest, datainput, datamatch, datatable, layout, pgraph
from scale.api import ScaleToolkit
from scale.egress import EgressGate
from scale.util import ScaleError, load_doc, load_yaml

X = EXAMPLES / "scale-catal/data-mapping-examples"
FILES = {"co2rr-fe-wide.xlsx": "co2rr-faradaic-efficiency", "thermal-tests.csv": "thermal-catalytic-test",
         "kinetics.csv": "kinetics-rates", "dft-energies.csv": "dft-adsorption-energy",
         "characterization.csv": "characterization-summary"}


@pytest.fixture(scope="module")
def spec():
    return spec_of(load_doc(X / "seed.json"))


@pytest.fixture(scope="module")
def templates():
    return datamatch.load_templates(REPO)


@pytest.fixture()
def seed():
    return load_doc(X / "seed.json")


def match(data, name, templates, spec, **kw):
    kw.setdefault("origin", "synthetic")
    kw.setdefault("namespace", "ex-dm")
    return datamatch.match(data, name, templates, spec, **kw)


def plan(data, mapping, doc, spec, name="data.csv"):
    return dataingest.plan(data, mapping.encode("utf-8") if isinstance(mapping, str) else mapping, doc, spec, name,
                           uri=name)


def node(doc, nid):
    return next(n for n in doc["nodes"] if n["id"] == nid)


def results(doc, nid):
    return {x["id"]: x for x in node(doc, nid)["props"]["result_values"]}


# ------------------------------------------------------------------------------------------ the library
def test_the_library_loads_and_each_template_names_its_example(templates):
    assert sorted(t["id"] for t in templates) == sorted(FILES.values())
    for t in templates:
        assert (REPO / layout.DOMAIN / t["example"]).is_file(), t["id"]
        assert t["maturity"] == "provisional"
        assert any(r.get("required") for r in t["roles"].values())
    with pytest.raises(ScaleError, match="template_version 1 is required"):
        datamatch.check_template({**templates[0], "template_version": 2}, "t.yaml")
    with pytest.raises(ScaleError, match="has no \\(\\?P<target>"):
        datamatch.check_template({**templates[0], "require_one_of": [],
                                  "roles": {"x": {"kind": "melt", "id": "x", "patterns": ["^FE"]}}}, "t.yaml")


@pytest.mark.parametrize("name", sorted(FILES))
def test_each_template_matches_its_example_and_compiles_to_a_verified_document(name, templates, spec, seed, tmp_path):
    data = (X / name).read_bytes()
    res = match(data, name, templates, spec)
    best = res["candidates"][0]
    assert best["template"] == FILES[name] and best["complete"] and best["problems"] == []
    assert all(c["score"] < best["score"] for c in res["candidates"][1:])
    assert best["mapping_yaml"] == (X / f"{name.rsplit('.', 1)[0]}.map.yaml").read_text()   # what the example saved
    out = plan(data, best["mapping_yaml"], seed, spec, name)
    doc = out["document"]
    rep = pgraph.validate(doc, spec)
    assert rep.errors == [] and rep.warnings == [] and out["records"] >= 3 and out["results"] >= 3
    (tmp_path / name).write_bytes(data)
    v = datatable.verify(doc, base=tmp_path)
    assert v["status"] == "verified", v
    assert all(d["matched"] == d["checked"] == d["cells"] for d in v["datasets"])


def test_the_example_is_built_reproducibly_valid_and_verified(spec, seed):
    doc = seed
    for name in FILES:
        m = (X / f"{name.rsplit('.', 1)[0]}.map.yaml").read_text()
        doc = plan((X / name).read_bytes(), m, doc, spec, name)["document"]
    assert doc == load_doc(X / "data-mappings.json")
    rep = pgraph.validate(doc, spec)
    assert rep.errors == [] and rep.warnings == []
    assert datatable.verify(doc, base=X)["status"] == "verified"


def test_matching_is_deterministic(templates, spec):
    data = (X / "co2rr-fe-wide.xlsx").read_bytes()
    assert match(data, "a.xlsx", templates, spec) == match(data, "a.xlsx", templates, spec)


# ------------------------------------------------------------------------------------------ wide tables
def test_a_wide_table_is_melted_into_one_result_per_column(spec, seed):
    doc = load_doc(X / "data-mappings.json")
    t = [n for n in doc["nodes"] if n["label"] == "CatalyticTest" and "/test/R2" in n["id"]][0]
    got = {x["id"]: x for x in t["props"]["result_values"]}
    assert sorted(got) == ["fe_co", "fe_h2", "fe_hcoo", "j_co"]          # FE total (%) is a sum: not a product
    assert got["fe_co"]["value"] == 48 and got["fe_co"]["unit"] == "%" and got["fe_co"]["target"] == "ex-dm:species/CO"
    assert got["fe_hcoo"]["status"] == "below_detection_limit" and got["fe_hcoo"]["as_reported"] == "<0.5"
    assert got["j_co"]["variable"] == "ex-dm:variable/partial-current-density" and got["j_co"]["unit"] == "mA/cm2"
    ev = {e["id"]: e for e in t["props"]["evidence"]}
    cell = ev[got["fe_co"]["evidence_ids"][0]]["dataset_cell"]
    assert cell == {"column": "FE CO (%)", "row_key": [{"column": "Run", "value": "R2"}]}
    r3 = [n for n in doc["nodes"] if n["id"].endswith("/test/R3")][0]
    hcoo = next(x for x in r3["props"]["result_values"] if x["id"] == "fe_hcoo")
    assert hcoo["status"] == "not_measured" and "value" not in hcoo                    # a blank is never zero


def test_melt_mapping_rules(spec, seed):
    data = b"run,FE CO (%),FE H2 (%)\nR1,50,40\n"
    base = {"mapping_version": 2, "input": {"format": "csv"}, "dataset": {"id": "ex:m/d", "version": "1",
                                                                          "primary_key": ["run"]},
            "references": {"fe": {"id": "ex:m/fe", "label": "Variable", "props": {
                "name": "FE", "scale": "quantitative", "quantity_kind": "faradaic_efficiency", "unit": "%"}}},
            "lookups": {"sp": {"label": "ChemicalSpecies", "match": ["formula"],
                               "create": {"id_prefix": "ex:m/sp/", "as": "formula"}}},
            "records": [{"as": "t", "label": "CatalyticTest", "key": ["run"], "origin": "synthetic",
                         "properties": {"test_kind": {"literal": "activity"}},
                         "results": [{"variable": "@fe", "unit_from_header": True, "target_lookup": "sp",
                                      "melt": [{"column": "FE CO (%)", "id": "fe_co", "target": "CO"},
                                               {"column": "FE H2 (%)", "id": "fe_h2", "target": "H2"}]}]}]}
    doc = plan(data, yaml.safe_dump(base), seed, spec)["document"]
    assert sorted(results(doc, "ex:m/d/t/R1")) == ["fe_co", "fe_h2"]
    bad = copy.deepcopy(base)
    bad["records"][0]["results"][0]["column"] = "FE CO (%)"
    with pytest.raises(ScaleError, match="reads one column \\(column\\) or several \\(melt\\), exactly one"):
        plan(data, yaml.safe_dump(bad), seed, spec)
    bad = copy.deepcopy(base)
    bad["records"][0]["results"][0]["melt"][1]["id"] = "fe_co"
    with pytest.raises(ScaleError, match="repeats an id or a column of the melt"):
        plan(data, yaml.safe_dump(bad), seed, spec)
    # two rows of one record give one id twice: refused, never merged
    with pytest.raises(ScaleError, match="has two results 'fe_co': its rows need an id_column"):
        plan(data.replace(b"R1,50,40\n", b"R1,50,40\nR1,51,41\n"),
             yaml.safe_dump({**base, "dataset": {**base["dataset"], "row_selector": "index", "primary_key": []}}),
             seed, spec)


# ------------------------------------------------------------------------------------------ header units
def test_header_units_are_read_and_checked_by_the_quantity_contracts(spec, seed):
    assert dataingest.header_unit("FE CO (%)") == ("FE CO", "%")
    assert dataingest.header_unit("T / °C") == ("T", "°C")
    assert dataingest.header_unit("T [K]") == ("T", "K")
    assert dataingest.header_unit("Ni/Al") == ("Ni/Al", None)
    doc = load_doc(X / "data-mappings.json")
    t1 = [n for n in doc["nodes"] if n["id"].endswith("/test/T1")][0]["props"]
    assert t1["temperature"]["unit"] == "°C" and t1["temperature"]["value"] == 250
    assert t1["space_velocity"]["unit"] == "h-1" and t1["pressure"]["unit"] == "bar"
    m = (X / "thermal-tests.map.yaml").read_text()
    data = (X / "thermal-tests.csv").read_bytes()
    with pytest.raises(ScaleError, match="the header 'T \\(mV\\)'.*not a dimension of temperature"):
        plan(data.replace(b"T (\xb0C)", b"T (mV)"), m.replace("T (°C)", "T (mV)"), seed, spec, "t.csv")
    with pytest.raises(ScaleError, match="cannot be read as a temperature"):
        plan(data.replace(b"T (\xb0C)", b"T (furlongs)"), m.replace("T (°C)", "T (furlongs)"), seed, spec, "t.csv")
    with pytest.raises(ScaleError, match="writes no unit"):
        plan(data.replace(b"T (\xb0C)", b"Temp"), m.replace("T (°C)", "Temp"), seed, spec, "t.csv")


def test_the_matcher_takes_units_from_headers_and_reports_assumed_ones(templates, spec):
    data = b"run,potential,FE_CO,FE_H2\nR1,-0.8,60,30\nR2,-1.0,50,40\n"
    best = match(data, "x.csv", templates, spec)["candidates"][0]
    assert best["template"] == "co2rr-faradaic-efficiency"
    assert best["bindings"]["potential"]["unit"] == "V vs RHE" and not best["bindings"]["potential"]["unit_from_header"]
    why = [u["why"] for u in best["uncertain"]]
    assert any("assumed 'V vs RHE'" in w for w in why) and any("assumed '%'" in w for w in why)
    # a header unit of another dimension is never bound to the role
    data = b"run,E (eV),FE CO (%)\nR1,-0.8,60\n"
    best = match(data, "x.csv", templates, spec, template="co2rr-faradaic-efficiency")["candidates"][0]
    assert "potential" not in best["bindings"] and not best["complete"]


# ------------------------------------------------------------------------------------------ lookups and references
def test_lookups_find_existing_records_resolve_aliases_and_create_by_formula(spec, seed):
    doc = load_doc(X / "data-mappings.json")
    t1 = results(doc, [n["id"] for n in doc["nodes"] if n["id"].endswith("/test/T1")][0])
    assert t1["x_co2"]["target"] == "ex-dm:species/co2"            # the seed's CO2, found by its formula
    fe = results(doc, [n["id"] for n in doc["nodes"] if n["id"].endswith("/test/R1")][0])["fe_co"]
    assert fe["variable"] == "ex-dm:variable/fe"                   # the seed's Faradaic-efficiency Variable, reused
    hcoo = node(doc, "ex-dm:species/HCOO-")["props"]
    assert hcoo["formula"] == "HCOO-" and hcoo["charge"] == -1 and hcoo["elemental_composition"] == {"C": 1, "H": 1, "O": 2}
    data = b"run,product,FE (%)\nR1,formate,4\nR2,carbon monoxide,60\n"
    m = {"mapping_version": 2, "input": {"format": "csv"},
         "dataset": {"id": "ex:m/d", "version": "1", "primary_key": ["run"]},
         "lookups": {"sp": {"label": "ChemicalSpecies", "match": ["formula", "name"],
                            "aliases": {"formate": "HCOO-", "carbon monoxide": "CO"},
                            "create": {"id_prefix": "ex:m/sp/", "as": "formula"}}},
         "records": [{"as": "t", "label": "CatalyticTest", "key": ["run"], "origin": "synthetic",
                      "properties": {"test_kind": {"literal": "activity"}},
                      "results": [{"id": "fe", "column": "FE (%)", "unit_from_header": True, "variable": "ex-dm:variable/fe",
                                   "target_column": "product", "target_lookup": "sp"}]}]}
    out = plan(data, yaml.safe_dump(m), seed, spec)
    doc = out["document"]
    assert results(doc, "ex:m/d/t/R1")["fe"]["target"] == "ex:m/sp/HCOO-"
    assert out["created"] == ["ex:m/sp/CO", "ex:m/sp/HCOO-"]
    ingest = node(doc, "ex:m/d")["props"]["table"]["ingest"]
    assert json.loads(ingest["lookup_0000"]) == ["product", "carbon monoxide", "ex:m/sp/CO"]
    v = datatable.verify(doc, files={"ex:m/d": _write(data)})
    assert v["status"] == "verified", v                           # the cell 'formate' names ex:m/sp/HCOO-
    # without create, a name the document does not hold is refused
    m2 = copy.deepcopy(m)
    del m2["lookups"]["sp"]["create"]
    with pytest.raises(ScaleError, match="'formate' names no ChemicalSpecies of the document \\(lookup sp\\)"):
        plan(data, yaml.safe_dump(m2), seed, spec)
    # a formula whose charge reads two ways is never created
    with pytest.raises(ScaleError, match="'NO3-' ends in a charge that can be read two ways"):
        plan(data.replace(b"formate", b"NO3-"), yaml.safe_dump(m), seed, spec)
    # a reference whose reuse finds two records is refused, never guessed
    two = copy.deepcopy(seed)
    two["nodes"].append({**copy.deepcopy(node(seed, "ex-dm:variable/fe")), "id": "ex-dm:variable/fe2"})
    m3 = copy.deepcopy(m)
    m3["references"] = {"fe": {"id": "ex:m/fe", "label": "Variable", "reuse": ["quantity_kind"], "props": {
        "name": "FE", "scale": "quantitative", "quantity_kind": "faradaic_efficiency", "unit": "%"}}}
    m3["records"][0]["results"][0]["variable"] = "@fe"
    with pytest.raises(ScaleError, match="holds 2 Variable records with quantity_kind 'faradaic_efficiency'"):
        plan(data, yaml.safe_dump(m3), two, spec)


def _write(data: bytes):
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "data.csv"
    p.write_bytes(data)
    return p


def test_several_records_in_one_pass(spec):
    doc = load_doc(X / "data-mappings.json")
    did = next(n["id"] for n in doc["nodes"] if n["id"].startswith("ex-dm:data/characterization-") and
               n["props"].get("table"))
    sample = node(doc, f"{did}/sample/Ni-Al2O3-1")
    assert sample["label"] == "CatalystSample" and sample["props"]["name"] == "Ni-Al2O3-1"
    techs = {n["props"]["technique"]: n for n in doc["nodes"] if n["label"] == "Characterization"
             and n["id"].endswith("/Ni-Al2O3-1")}
    assert sorted(techs) == ["N2 physisorption (BET)", "X-ray diffraction", "X-ray photoelectron spectroscopy"]
    examines = {(ln["source"], ln["target"]) for ln in doc["links"] if ln["type"] == "EXAMINES"}
    assert all((n["id"], sample["id"]) in examines for n in techs.values())
    xps = results(doc, techs["X-ray photoelectron spectroscopy"]["id"])
    assert xps["xps_ni_al"]["definition"] == "Ni/Al surface atomic ratio (XPS)" and xps["xps_ni_al"]["value"] == 0.21
    assert results(doc, f"{did}/xrd/Ni-Al2O3-3")["crystallite_size"]["status"] == "not_measured"


# ------------------------------------------------------------------------------------------ workbooks and dialects
def test_a_workbook_with_several_sheets_and_a_preamble(templates, spec, seed, tmp_path):
    data = (X / "co2rr-fe-wide.xlsx").read_bytes()
    assert datainput.sheets(data) == ["Notes", "FE", "GC calibration"]
    res = match(data, "fe.xlsx", templates, spec)
    reading = {r["sheet"]: r for r in res["reading"]}
    assert "no header row found" in reading["Notes"]["error"]
    assert reading["FE"]["recipe"] == {"format": "xlsx", "sheet": "FE", "header_row": 4} and reading["FE"]["rows"] == 3
    best = res["candidates"][0]
    why = " | ".join(u["why"] for u in best["uncertain"])
    assert "sheet 'FE' of 'Notes', 'FE', 'GC calibration'" in why and "header row was found, not given: row 4" in why
    assert "1 non-empty rows after a blank row are not read" in why
    # the sheet and header row given: those notes go
    best = match(data, "fe.xlsx", templates, spec, sheet="FE", header_row=4)["candidates"][0]
    assert not any("sheet" in u["why"] or "header row" in u["why"] for u in best["uncertain"])
    # two Datasets: the workbook's bytes, and its sheet as scale_csv_v1 with the recipe
    doc = plan(data, best["mapping_yaml"], seed, spec, "fe.xlsx")["document"]
    tab = next(n for n in doc["nodes"] if n["label"] == "Dataset" and n["props"].get("table"))
    fid = tab["props"]["table"]["ingest"]["extracted_from"]
    assert tab["id"].startswith(fid + "/table-")
    wb = node(doc, fid)["props"]
    assert wb["sha256"] == datatable.sha256(data) and wb["media_type"] == datainput.XLSX_MEDIA and "table" not in wb
    ing = tab["props"]["table"]["ingest"]
    assert ing["input_sheet"] == "FE" and ing["input_header_row"] == "4"
    assert any(ln["type"] == "INCLUDES" and ln["source"] == fid and ln["target"] == tab["id"] for ln in doc["links"])
    canonical = datainput.extract(data, {"sheet": "FE", "header_row": 4})["canonical"]
    assert tab["props"]["sha256"] == datatable.sha256(canonical)
    # verification extracts the table again from the workbook, or reads the canonical table given as the file
    (tmp_path / "fe.xlsx").write_bytes(data)
    assert datatable.verify(doc, base=tmp_path)["status"] == "verified"
    (tmp_path / "t.csv").write_bytes(canonical)
    assert datatable.verify(doc, files={tab["id"]: tmp_path / "t.csv"}, base=tmp_path / "none")["status"] == "verified"
    # the workbook given as its own Dataset's file: checked by its bytes, its table extracted from it
    got = datatable.verify(doc, files={fid: tmp_path / "fe.xlsx"}, base=tmp_path / "none")
    assert got["status"] == "verified", got
    # another sheet of the same workbook is another table, so another Dataset under the same file
    m2 = {"mapping_version": 2, "input": {"format": "xlsx", "sheet": "GC calibration"},
          "dataset": {"id_prefix": "ex-dm:data/", "primary_key": ["Gas"]},
          "records": [{"as": "c", "label": "Characterization", "key": ["Gas"], "origin": "synthetic",
                       "name_column": "Gas", "properties": {"technique": {"literal": "GC"}}}]}
    doc2 = plan(data, yaml.safe_dump(m2), doc, spec, "fe.xlsx")["document"]
    tabs = sorted(n["id"] for n in doc2["nodes"] if n["label"] == "Dataset" and n["id"].startswith(fid + "/table-"))
    assert len(tabs) == 2
    other = datainput.write_xlsx([("FE", [["Run", "E (V vs RHE)"], ["R1", -0.9]])])
    (tmp_path / "fe.xlsx").write_bytes(other)
    got = datatable.verify(doc, base=tmp_path)
    assert got["status"] == "failed" and "checksum mismatch" in got["datasets"][0]["problems"][0]
    (tmp_path / "fe.xlsx").unlink()
    assert datatable.verify(doc, base=tmp_path)["status"] == "unverified"


def test_the_xlsx_reader(tmp_path):
    wb = datainput.write_xlsx([("S", [["a", "b", "when"], ["x", 0.1 + 0.2, 46083.5], ["y", 3, None], [True, "<1", 7]])],
                              {("S", 1, 2)})
    g = datainput.grid(wb)
    assert g["rows"] == [["a", "b", "when"], ["x", "0.30000000000000004", "2026-03-02T12:00:00"], ["y", "3", ""], ["true", "<1", "7"]]
    assert datainput.write_xlsx([("S", [["a"]])]) == datainput.write_xlsx([("S", [["a"]])])      # deterministic
    with pytest.raises(ScaleError, match="no sheet 'T'"):
        datainput.grid(wb, {"sheet": "T"})
    with pytest.raises(ScaleError, match="Excel 97-2003"):
        datainput.sniff(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
    with pytest.raises(ScaleError, match="holds data in column D, beyond the header"):
        datainput.extract(datainput.write_xlsx([("S", [["a", "b"], ["1", "2", None, "x"]])]))
    with pytest.raises(ScaleError, match="has no name in column B"):
        datainput.extract(datainput.write_xlsx([("S", [["a", None, "c"], ["1", "2", "3"]])]))
    e = datainput.extract(wb, {"range": "A1:B3"})
    assert e["header"] == ["a", "b"] and e["rows_used"] == "A1:B3" and e["recipe"]["range"] == "A1:B3"
    # cell and row references are optional in a workbook: each then follows the last
    import io
    import re
    import zipfile
    src = zipfile.ZipFile(io.BytesIO(datainput.write_xlsx([("S", [["a", "b"], [1, 2]])])))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            x = src.read(n)
            z.writestr(n, re.sub(rb' r="[A-Z]*[0-9]+"', b"", x) if n.startswith("xl/worksheets/") else x)
    assert datainput.grid(buf.getvalue())["rows"] == [["a", "b"], ["1", "2"]]


def test_csv_dialects_are_read_and_recorded(templates, spec, seed):
    data = (X / "thermal-tests.csv").read_bytes()
    g = datainput.grid(data)
    assert g["encoding"] == "cp1252" and g["delimiter"] == ";"
    best = match(data, "t.csv", templates, spec)["candidates"][0]
    m = yaml.safe_load(best["mapping_yaml"])
    assert m["input"] == {"format": "csv", "header_row": 4, "encoding": "cp1252", "delimiter": ";", "decimal": ","}
    doc = plan(data, best["mapping_yaml"], seed, spec, "t.csv")["document"]
    tab = next(n for n in doc["nodes"] if n["label"] == "Dataset" and n["props"].get("table"))
    assert tab["props"]["table"]["ingest"]["input_decimal"] == ","
    x = next(n for n in doc["nodes"] if n["id"].endswith("/test/T1"))
    assert next(r for r in x["props"]["result_values"] if r["id"] == "x_co2")["value"] == 18.4
    # a file already in scale_csv_v1 is one Dataset, its own bytes
    data = (X / "kinetics.csv").read_bytes()
    assert datainput.extract(data)["identity"]
    best = match(data, "k.csv", templates, spec)["candidates"][0]
    doc = plan(data, best["mapping_yaml"], seed, spec, "k.csv")["document"]
    ds = [n for n in doc["nodes"] if n["label"] == "Dataset" and n["id"].startswith("ex-dm:data/")]
    assert len(ds) == 1 and ds[0]["props"]["sha256"] == datatable.sha256(data)


# ------------------------------------------------------------------------------------------ uncertainty
def test_ambiguous_headers_are_reported_never_guessed(templates, spec, seed):
    data = "run,Temp 1 (°C),Temp 2 (°C),X CO2 (%)\nA,250,260,10\nB,300,310,20\n".encode()
    best = match(data, "a.csv", templates, spec, template="thermal-catalytic-test")["candidates"][0]
    assert "temperature" not in best["bindings"] and not best["complete"]
    amb = next(u for u in best["uncertain"] if u["role"] == "temperature")
    assert "two or more columns fit" in amb["why"]
    assert [c["column"] for c in amb["candidates"]] == ["Temp 1 (°C)", "Temp 2 (°C)"]
    assert "?CHOOSE: a column for temperature (Temp 1 (°C) | Temp 2 (°C))" in best["mapping_yaml"]
    with pytest.raises(ScaleError, match="names the column '\\?CHOOSE"):
        plan(data, best["mapping_yaml"], seed, spec, "a.csv")
    # a person settles it
    best = match(data, "a.csv", templates, spec, template="thermal-catalytic-test",
                 bind={"temperature": "Temp 2 (°C)"})["candidates"][0]
    assert best["complete"] and best["bindings"]["temperature"] == {"column": "Temp 2 (°C)", "score": 1.0, "by": "you",
                                                                     "unit": "°C", "unit_from_header": True}
    doc = plan(data, best["mapping_yaml"], seed, spec, "a.csv")["document"]
    t = next(n for n in doc["nodes"] if n["id"].endswith("/test/A"))
    assert t["props"]["temperature"]["value"] == 260


def test_a_similar_name_is_bound_and_reported(templates, spec):
    data = b"run id,Reactor temp (K),X CO2 (%)\nA,500,10\n"
    best = match(data, "a.csv", templates, spec, template="thermal-catalytic-test")["candidates"][0]
    assert best["bindings"]["temperature"]["column"] == "Reactor temp (K)"
    assert any(u["role"] == "temperature" and "similar name" in u["why"] for u in best["uncertain"])


def test_tokens_the_mapping_does_not_name_make_it_incomplete(templates, spec):
    data = b"run,potential (V vs RHE),FE CO (%),FE H2 (%)\nR1,-0.8,n.d.,30\nR2,-1.0,40,50\nR3,-1.2,30,60\n"
    best = match(data, "a.csv", templates, spec)["candidates"][0]
    assert not best["complete"]
    assert any(u["column"] == "FE CO (%)" and u.get("tokens") == ["n.d."] for u in best["uncertain"])
    # a column the pattern names whose values are no numbers is not read, and that blocks the mapping too
    data = b"run,potential (V vs RHE),FE CO (%),FE H2 (%)\nR1,-0.8,n.d.,30\n"
    best = match(data, "a.csv", templates, spec)["candidates"][0]
    assert not best["complete"] and "fe_co" not in best["mapping_yaml"]
    assert any(u["column"] == "FE CO (%)" and "not taken" in u["why"] and u["blocking"] for u in best["uncertain"])
    # a time without a time zone, and a status outside its values
    data = b"calc,E_ads (eV),status,date\nC1,-1.2,done,2026-01-01 10:00\n"
    best = match(data, "a.csv", templates, spec, template="dft-adsorption-energy",
                 bind={"status": "status"})["candidates"][0]
    assert not best["complete"] and any("not one of" in u["why"] for u in best["uncertain"])


def test_a_recorded_origin_needs_a_recording_time(templates, spec, seed):
    data = (X / "kinetics.csv").read_bytes()
    best = match(data, "k.csv", templates, spec, origin=None, namespace="lab")["candidates"][0]
    assert not best["complete"] and any(u["role"] == "recorded_at" for u in best["uncertain"])
    best = match(data, "k.csv", templates, spec, origin=None, recorded_at="2026-03-02T10:00:00Z")["candidates"][0]
    assert best["complete"]
    assert yaml.safe_load(best["mapping_yaml"])["records"][0]["recorded_at"] == "2026-03-02T10:00:00Z"
    best = match(data, "k.csv", templates, spec, origin="reported")["candidates"][0]
    assert best["complete"] and yaml.safe_load(best["mapping_yaml"])["records"][0]["origin"] == "reported"


# ------------------------------------------------------------------------------------------ mapping_version 1
def test_mapping_version_1_files_are_read_as_before(spec):
    v1 = EXAMPLES / "scale-catal/data-ingest-examples"
    seed = load_doc(v1 / "seed.json")
    doc = dataingest.plan((v1 / "co2rr-fe.csv").read_bytes(), (v1 / "co2rr-fe.map.yaml").read_bytes(), seed, spec,
                          "co2rr-fe.csv")["document"]
    doc = dataingest.plan((v1 / "dft-adsorption.csv").read_bytes(), (v1 / "dft-adsorption.map.yaml").read_bytes(), doc,
                          spec, "dft-adsorption.csv")["document"]
    assert doc == load_doc(v1 / "data-ingest.json")
    text = (v1 / "co2rr-fe.map.yaml").read_text()
    for extra in ("input:\n  format: csv\n", "lookups: {}\n", "references: {}\n"):     # version 2 keys stay version 2's
        with pytest.raises(ScaleError, match="the mapping has unknown keys"):
            dataingest.plan((v1 / "co2rr-fe.csv").read_bytes(), (text + extra).encode(), seed, spec)


# ------------------------------------------------------------------------------------------ the command and the tool
def test_the_cli_matches_saves_adds_and_verifies(tmp_path):
    for name in ("thermal-tests.csv", "seed.json"):
        shutil.copy(X / name, tmp_path / name)
    run = lambda *a: subprocess.run([sys.executable, "-c", RUN_SCALE, str(REPO / "tools"), *a],  # noqa: E731
                                    cwd=tmp_path, capture_output=True, text=True)
    r = run("data", "match", "--list")
    assert r.returncode == 0 and "co2rr-faradaic-efficiency" in r.stdout and "kinetics-rates" in r.stdout
    r = run("data", "match", "thermal-tests.csv", "--origin", "synthetic", "--namespace", "ex-dm", "--save", "t.map.yaml")
    assert r.returncode == 0, r.stderr
    assert "best: thermal-catalytic-test (score 1.0)" in r.stdout and "UNCERTAIN the header row was found" in r.stdout
    assert (tmp_path / "t.map.yaml").read_text() == (X / "thermal-tests.map.yaml").read_text()
    r = run("data", "match", "thermal-tests.csv", "--save", "t.map.yaml")
    assert r.returncode != 0 and "exists" in r.stderr
    r = run("data", "add", "thermal-tests.csv", "--map", "t.map.yaml", "--into", "seed.json")
    assert r.returncode == 0, r.stderr
    assert "read csv, header row 4, cells A4:H8; the original file is" in r.stdout
    r = run("data", "verify", "seed.json", "--require-all")
    assert r.returncode == 0 and "VERIFIED" in r.stdout, r.stdout + r.stderr
    j = json.loads(run("data", "match", "thermal-tests.csv", "--format", "json").stdout)
    assert j["best"] == "thermal-catalytic-test" and "mapping_yaml" in j["candidates"][0]


def test_the_toolkit_matches_inside_its_working_directory(tmp_path):
    shutil.copy(X / "dft-energies.csv", tmp_path / "dft.csv")
    tk = ScaleToolkit(workspace=tmp_path, repo=REPO)
    got = tk.call("scale_data_match", {"file": "dft.csv", "namespace": "ex-dm", "origin": "synthetic",
                                       "save_as": "dft.map.yaml"})
    assert got["best"] == "dft-adsorption-energy" and got["written"] == str(tmp_path / "dft.map.yaml")
    assert "mapping" not in got["candidates"][0] and got["candidates"][0]["complete"]
    assert tk.call("scale_data_match", {"file": "dft.csv", "save_as": "../x.yaml"})["error"].startswith("save_as")
    shutil.copy(X / "seed.json", tmp_path / "seed.json")
    out = tk.call("scale_data_add", {"csv": "dft.csv", "mapping": "dft.map.yaml", "into": "seed.json"})
    assert "error" not in out and out["records"] == 4


def test_matching_a_file_of_a_data_repository_is_gated(tmp_path):
    repo = tmp_path / "lab-data"
    (repo / "raw").mkdir(parents=True)
    (repo / "scale-data.yaml").write_text(json.dumps({
        "scale_data_policy": 1, "repository": {"readers": {"access": "project", "project": "lab:project/a"},
                                               "copyright_cap": False},
        "defaults": {"stewardship": {"access": "project", "project": "lab:project/a"}}}))
    shutil.copy(X / "kinetics.csv", repo / "raw" / "k.csv")
    tk = ScaleToolkit(workspace=repo, repo=REPO, egress=EgressGate("anthropic", []))
    out = tk.call("scale_data_match", {"file": "raw/k.csv"})
    assert "egress denied" in out.get("error", "") and "Ni-Al2O3" not in json.dumps(out)
    ok = ScaleToolkit(workspace=repo, repo=REPO, egress=EgressGate("local", [{"id": "local", "access": ["project"]}]))
    assert ok.call("scale_data_match", {"file": "raw/k.csv"})["best"] == "kinetics-rates"
    shutil.copy(X / "kinetics.csv", tmp_path / "k.csv")             # outside any data repository: not gated
    assert ScaleToolkit(workspace=tmp_path, repo=REPO, egress=EgressGate("anthropic", [])).call(
        "scale_data_match", {"file": "k.csv"})["best"] == "kinetics-rates"


def test_the_examples_are_reviewed_public_fixtures():
    fixtures = load_yaml(REPO / layout.PUBLIC_FIXTURES)["fixtures"]
    pats = [p for f in fixtures for p in f["paths"]]
    assert any(p.startswith("domain/examples/scale-catal/data-mapping-examples/") for p in pats)
    doc = load_doc(X / "data-mappings.json")
    assert doc["graph"]["data_status"] == "synthetic_example" and doc["graph"]["stewardship"]["access"] == "public"


def test_build_examples_rebuilds_the_same_files(tmp_path):
    shutil.copytree(X, tmp_path / "x")
    src = (tmp_path / "x" / "build_examples.py").read_text().replace(
        "HERE = Path(__file__).resolve().parent", f"HERE = Path({str(tmp_path / 'x')!r})")
    script = tmp_path / "x" / "build_examples.py"
    script.write_text(src.replace("for p in [HERE, *HERE.parents]:", f"for p in [Path({str(REPO)!r})]:"))
    subprocess.run([sys.executable, str(script)], check=True, capture_output=True)
    # the data files and the mappings match saves (the documents pin the head's version: the test above checks them)
    for f in sorted(p.name for p in X.iterdir() if p.suffix in (".csv", ".xlsx", ".yaml")):
        assert (tmp_path / "x" / f).read_bytes() == (X / f).read_bytes(), f


# ------------------------------------------------------------------------------------------ what review found
def test_a_workbook_never_gives_a_value_it_does_not_hold():
    import io
    import zipfile
    src = zipfile.ZipFile(io.BytesIO(datainput.write_xlsx([("S", [["run", "FE CO (%)"], ["R1", 60]])])))

    def variant(old: bytes, new: bytes, styles: bytes | None = None) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in src.namelist():
                x = src.read(n)
                if n == "xl/worksheets/sheet1.xml":
                    x = x.replace(old, new)
                if n == "xl/styles.xml" and styles is not None:
                    x = styles
                z.writestr(n, x)
        return buf.getvalue()
    # a formula never calculated is refused, never read as a blank (and so as not measured)
    with pytest.raises(ScaleError, match="B2: a formula without a computed value"):
        datainput.grid(variant(b'<c r="B2"><v>60</v></c>', b'<c r="B2"><f>A1*2</f></c>'))
    # a cell formatted as a percentage stores 0.6 and shows 60%: never read as 0.6
    styles = (b'<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/'
              b'spreadsheetml/2006/main"><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="9"/></cellXfs></styleSheet>')
    wb = variant(b'<c r="B2"><v>60</v></c>', b'<c r="B2" s="1"><v>0.6</v></c>', styles)
    assert datainput.grid(wb)["rows"][1] == ["R1", "60%"]
    assert datainput.grid(wb, {"percent": "displayed"})["rows"][1] == ["R1", "60"]
    templates = datamatch.load_templates(REPO)
    spec = spec_of(load_doc(X / "seed.json"))
    best = datamatch.match(wb, "p.xlsx", templates, spec, template="co2rr-faradaic-efficiency")["candidates"][0]
    assert yaml.safe_load(best["mapping_yaml"])["input"]["percent"] == "displayed"
    assert any("percentages are read as the numbers they show" in u["why"] for u in best["uncertain"])
    # a workbook whose used area is a sheet's whole grid is refused before it is allocated
    with pytest.raises(ScaleError, match="more than 20000000 cells"):
        datainput.grid(variant(b"</sheetData>", b'<row r="1048576"><c r="XFD1048576"><v>1</v></c></row></sheetData>'))


def test_an_existing_id_of_another_meaning_is_refused(spec, seed):
    data = b"run,product,FE (%)\nR1,CO,60\n"
    m = {"mapping_version": 2, "input": {"format": "csv"},
         "dataset": {"id": "ex:m/d", "version": "1", "primary_key": ["run"]},
         "references": {"fe": {"id": "ex-dm:variable/fe", "label": "Variable", "props": {
             "name": "S", "scale": "quantitative", "quantity_kind": "selectivity", "unit": "%"}}},
         "lookups": {"sp": {"label": "ChemicalSpecies", "match": ["formula"],
                            "create": {"id_prefix": "ex-dm:species/", "as": "formula"}}},
         "records": [{"as": "t", "label": "CatalyticTest", "key": ["run"], "origin": "synthetic",
                      "properties": {"test_kind": {"literal": "activity"}},
                      "results": [{"id": "fe", "column": "FE (%)", "unit_from_header": True, "variable": "@fe",
                                   "target_column": "product", "target_lookup": "sp"}]}]}
    with pytest.raises(ScaleError, match="ex-dm:variable/fe is in the document with other quantity_kind"):
        plan(data, yaml.safe_dump(m), seed, spec)
    m["references"]["fe"]["props"]["quantity_kind"] = "faradaic_efficiency"
    clash = copy.deepcopy(seed)                             # the id a creation would take holds another species
    clash["nodes"].append({"id": "ex-dm:species/CO", "label": "ChemicalSpecies",
                           "props": {"name": "methane", "formula": "CH4", "origin": "synthetic"}})
    del m["lookups"]["sp"]["match"]
    with pytest.raises(ScaleError, match="would create ex-dm:species/CO for 'CO'.*whose formula is 'CH4'"):
        plan(data, yaml.safe_dump(m), clash, spec)


def test_a_sample_column_is_never_melted_as_a_selectivity(templates, spec):
    data = b"Run,Sample,T (K),S CH4 (%)\nA,1,500,90\n"
    best = match(data, "s.csv", templates, spec, template="thermal-catalytic-test")["candidates"][0]
    assert best["bindings"]["sample"]["column"] == "Sample"
    assert [c["column"] for c in best["melt"]["selectivity"]] == ["S CH4 (%)"]
