"""Value-to-evidence checks (tools/valuecheck.py; `scale review values`): a model that misreads a table writes a
record with a genuine quote and a wrong number. The verbatim check passes it; this check must not."""

import json
import subprocess
import sys

from conftest import REPO
from scale import valuecheck as vc
from scale.util import load_doc

ROW = "350 150 5000 17.9"          # a table row: temperature, pressure, space velocity, conversion


def _doc(props: dict) -> dict:
    ev = [{"source": "ex:t/source/s", "locator": "p. 2, Table I", "kind": "table", "quote": ROW}]
    return {"graph": {"title": "t"}, "nodes": [{"id": "ex:t/test/1", "label": "CatalyticTest",
                                                  "props": {"name": "run 1", "evidence": ev, **props}}], "links": []}


def _one(props: dict) -> dict:
    res = vc.check_document(_doc(props))
    assert res["values"] == 1
    return res["rows"][0]


def test_a_correct_table_value_is_backed():
    assert _one({"conversion": {"status": "present", "value": 17.9, "unit": "%", "as_reported": "17.9"}})["outcome"] == "ok"


def test_a_shifted_column_is_flagged_although_its_quote_is_verbatim():
    # a small model put the space velocity's neighbour (the pressure) in the conversion and typed it as printed
    row = _one({"conversion": {"status": "present", "value": 15.0, "unit": "%", "as_reported": "15.0"}})
    assert row["outcome"] == "flag" and row["checks_failed"] == ["value_vs_evidence"]


def test_a_mistyped_value_is_flagged_against_its_own_printed_text():
    row = _one({"conversion": {"status": "present", "value": 1.79, "unit": "%", "as_reported": "17.9"}})
    assert row["outcome"] == "flag" and "value_vs_as_reported" in row["checks_failed"]


def test_printed_forms_are_read_as_print_has_them():
    for value, printed in [(3560, "3, 560"), (-28085, "−28,085"), (390, "39O°C"), (250, "Two hundred and fifty cc"),
                           (125000000, "125,000,000 cubic feet/day"), (2.07e-5, "2.07 × 10⁻⁵")]:
        assert any(vc._close(value, r) for r in vc.readings(printed)), printed


def test_a_sign_is_kept_and_a_range_or_a_name_is_no_sign():
    assert -0.5 in vc.readings("at -0.5 V vs RHE") and 0.5 not in vc.readings("at -0.5 V vs RHE")
    assert -23.5 in vc.readings("boiled at —23.5° C.")                   # OCR's em dash for a minus
    assert {10.0, 20.0} <= set(vc.readings("10-20 °C")) and 323.0 in vc.readings("Cu-HCl(323)")
    ev = [{"source": "s", "locator": "p. 3", "kind": "text", "quote": "the onset potential was 0.5 V vs RHE"}]
    lost = vc.check_value({"status": "present", "value": -0.5, "unit": "V"}, ev)
    assert lost["outcome"] == "flag" and lost["checks_failed"] == ["value_vs_evidence"]


def test_a_value_is_backed_only_by_the_evidence_its_evidence_ids_name():
    ev = [{"id": "e1", "source": "s", "locator": "p. 2", "kind": "text", "quote": "in 0.1 M KHCO3"},
          {"id": "e2", "source": "s", "locator": "Fig. 3b", "kind": "figure", "transcription": "CO 18.9"}]
    fe = vc.check_value({"status": "present", "value": 0.1, "unit": "%", "evidence_ids": ["e2"]}, ev)
    assert fe["outcome"] == "unverifiable" and "figure" in fe["notes"][0]      # not backed by the electrolyte's 0.1
    conc = vc.check_value({"status": "present", "value": 0.1, "unit": "M", "evidence_ids": ["e1"]}, ev)
    assert conc["outcome"] == "ok"


def test_a_figure_reading_is_never_printed_text_and_as_reported_does_not_hide_it():
    ev = [{"source": "s", "locator": "Fig. 2a", "kind": "figure", "reading": "vision", "region": [0, 0, 1, 1],
           "transcription": "Cu-HCl(323): 18.9"}]
    read = vc.check_value({"status": "present", "value": 18.9, "unit": "%"}, ev)
    named = vc.check_value({"status": "present", "value": 18.9, "unit": "%", "as_reported": "Cu-HCl(323) 18.9"}, ev)
    assert read["outcome"] == named["outcome"] == "unverifiable"


def test_the_sheet_shows_the_evidence_the_value_cites():
    doc = _doc({"conversion": {"status": "present", "value": 42, "unit": "%", "evidence_ids": ["t2"]}})
    doc["nodes"][0]["props"]["evidence"].append(
        {"id": "t2", "source": "ex:t/source/s", "locator": "p. 5, Table II", "kind": "table", "quote": "42 per cent"})
    row = vc.check_document(doc)["rows"][0]
    assert row["outcome"] == "ok" and row["locator"] == "p. 5, Table II" and row["quote"] == "42 per cent"


def test_what_no_tool_can_check_goes_to_a_person():
    fig = _one({"yield": {"status": "present", "value": 6.1, "unit": "%", "as_reported": "plotted point, Figure 6"}})
    words = vc.check_value({"status": "present", "value": 1, "unit": "h", "as_reported": "about an hour"},
                           [{"source": "s", "locator": "p. 1", "kind": "text", "quote": "reduced for about an hour"}])
    assert fig["outcome"] == words["outcome"] == "unverifiable"
    withheld = vc.check_value({"status": "present", "value": 5, "unit": "h"},
                              [{"source": "s", "locator": "p. 1", "kind": "text", "note": "quote withheld: sha256:ab chars:40"}])
    assert withheld["outcome"] == "unverifiable"


def test_nested_values_and_bounds_are_checked():
    doc = _doc({"conditions": [{"temperature": {"status": "present", "value": 350, "unit": "°C"}}],
                "conversion": {"status": "present", "value": 2, "unit": "%", "as_reported": "less than 2 per cent",
                               "bound": "upper"},
                "pressure": {"status": "not_reported", "missing_reason": "not_stated_in_source"}})
    res = vc.check_document(doc)
    assert {r["path"] for r in res["rows"]} == {"conditions[0].temperature", "conversion"}   # an absence is not checked
    assert "≤ 2 %" in vc.review_sheet(res)


def test_the_public_methanol_example_has_few_transcription_flags():
    res = vc.check_document(load_doc(REPO / "examples/methanol-synthesis-public"))
    assert res["values"] > 3000
    assert res["checks_failed"]["value_vs_as_reported"] <= 10          # each a real case for a person (OCR, mapping)
    assert res["counts"]["ok"] / res["values"] > 0.8


def test_source_check_and_scale_check_report_the_values_without_failing():
    from scale.devcheck import value_warnings
    msgs = value_warnings(REPO)
    assert any(m.startswith("values: methanol-synthesis-pre1966:") and "flagged" in m for m in msgs)


def test_source_check_summarizes_the_values_of_a_curation_folder(tmp_path):
    from test_source_curation import DOI, TEXT, fake
    from scale import curate
    (tmp_path / "paper.txt").write_text(TEXT, encoding="utf-8")
    curate.start(tmp_path / "w", doi=DOI, text=tmp_path / "paper.txt", repo=REPO, get=fake())
    res = curate.check(tmp_path / "w", REPO)
    assert res["ok"] and res["values"]["checked"] == 0 and res["values"]["flag"] == 0       # advisory: never fails it
    assert res["values"]["review_sheet"].startswith("scale review values ")


def test_the_command_writes_a_sheet_and_strict_fails_on_a_flag(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_doc({"conversion": {"status": "present", "value": 15.0, "unit": "%"}})))
    run = subprocess.run([sys.executable, "-m", "scale.cli", "review", "values", str(bad), "--strict"],
                         capture_output=True, text=True)
    assert run.returncode == 1 and "**1 flagged**" in run.stdout
