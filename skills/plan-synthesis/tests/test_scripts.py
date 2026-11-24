"""check_chemistry.py on small drafts, and the variant-pairs plan on the public synthesis examples.

Run from a checkout of trace-ai-org/scale with the package installed:
    python -m pytest skills/plan-synthesis/tests
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
REPO = HERE.parents[2]
EXAMPLES = REPO / "domain/examples/scale-catal/synthesis-examples"
sys.path.insert(0, str(SCRIPTS))

import check_chemistry as cc  # noqa: E402


def rules(found):
    return {(f["level"], f["rule"]) for f in found}


def pt_on_carbon(calcine_atm="air", calcine_t=400):
    return {"inputs": [{"name": "H2PtCl6·6H2O", "formula": "H2PtCl6·6H2O", "function": "metal_precursor",
                        "amount": {"value": 0.1, "unit": "mmol"}},
                       {"name": "Vulcan XC-72", "function": "support", "amount": {"value": 100, "unit": "mg"}},
                       {"name": "ethylene glycol", "function": "solvent", "amount": {"value": 50, "unit": "mL"}}],
            "steps": [{"action": "impregnate", "temperature_C": 25, "atmosphere": "air"},
                      {"action": "calcine", "temperature_C": calcine_t, "atmosphere": calcine_atm},
                      {"action": "reduce", "temperature_C": 300, "atmosphere": "5% H2/Ar"}]}


def test_a_carbon_support_heated_in_air_is_an_error_and_in_argon_or_air_free_it_is_not():
    assert ("error", "carbon_support_burns") in rules(cc.check(pt_on_carbon(), {"Pt"}))
    assert ("error", "carbon_support_burns") not in rules(cc.check(pt_on_carbon("Ar"), {"Pt"}))
    assert ("error", "carbon_support_burns") not in rules(cc.check(pt_on_carbon("H2/Ar (air-free)"), {"Pt"}))
    assert ("error", "carbon_support_burns") not in rules(cc.check(pt_on_carbon("air", 300), {"Pt"}))


def test_a_base_metal_heated_in_air_is_an_error_unless_a_later_step_reduces_it():
    draft = {"inputs": [{"name": "Ni(NO3)2·6H2O", "formula": "Ni(NO3)2·6H2O", "function": "metal_precursor",
                         "amount": {"value": 1, "unit": "mmol"}}],
             "steps": [{"action": "calcine", "temperature_C": 450, "atmosphere": "air"}]}
    assert ("error", "base_metal_oxidized") in rules(cc.check(draft, {"Ni"}))
    draft["steps"].append({"action": "reduce", "temperature_C": 500, "atmosphere": "H2"})
    assert ("error", "base_metal_oxidized") not in rules(cc.check(draft, {"Ni"}))


def test_the_warnings_a_missing_metal_an_ordering_step_and_electrons():
    draft = {"inputs": [{"name": "HAuCl4·3H2O", "formula": "HAuCl4·3H2O", "function": "metal_precursor",
                         "amount": {"value": 1, "unit": "mmol"}},
                        {"name": "NaBH4", "function": "reducing_agent", "amount": {"value": 0.1, "unit": "mmol"}},
                        {"name": "water", "function": "solvent"}],
             "steps": [{"action": "reduce", "temperature_C": 25, "atmosphere": "air"}]}
    found = rules(cc.check(draft, {"Au", "Cu"}, "ordered_intermetallic"))
    assert ("warning", "precursor_per_metal") in found          # no Cu precursor
    assert ("warning", "no_ordering_step") in found
    assert ("warning", "too_few_electrons") in found            # 0.8 mmol e- for 3 mmol e-
    draft["inputs"][1]["amount"] = {"value": 1, "unit": "mmol"}  # 8 mmol e-
    assert ("warning", "too_few_electrons") not in rules(cc.check(draft, {"Au"}))


def test_a_wet_step_above_every_solvents_boiling_point_is_a_warning_unless_sealed():
    draft = {"inputs": [{"name": "AgNO3", "formula": "AgNO3", "function": "metal_precursor",
                         "amount": {"value": 0.5, "unit": "mmol"}},
                        {"name": "ethanol", "function": "solvent"}],
             "steps": [{"action": "heat", "temperature_C": 160, "atmosphere": "N2"}]}
    assert ("warning", "above_boiling_point") in rules(cc.check(draft, {"Ag"}))
    assert ("warning", "above_boiling_point") not in rules(cc.check(draft, {"Ag"}, text="in a Teflon-lined autoclave"))


def test_the_target_is_read_from_the_question():
    q = "Target catalyst:\n- Composition (atomic_percent): Pt=50, Zn=50\n- Material class: ordered_intermetallic\n" \
        "- Support: hollow nitrogen-doped carbon nanotubes\n"
    assert cc.target_from_question(q) == ({"Pt", "Zn"}, "ordered_intermetallic", "hollow nitrogen-doped carbon nanotubes")


def test_the_variant_pairs_plan_returns_each_change_with_its_base():
    from scale.api import ScaleToolkit
    plan = json.loads((HERE.parent / "references" / "variant-pairs-plan.json").read_text())
    res = ScaleToolkit(repo=REPO).call("query", {"plan": plan, "path": str(EXAMPLES / "synthesis-conditions.json")})
    pairs = {(r["values"]["variant.id"], r["values"]["base.id"]): r["values"]["change.changed"] for r in res["rows"]}
    assert pairs[("ex:cnd/preparation/pt-sea-9", "ex:cnd/preparation/pt-sea-11")] == "the solution set to pH 9 instead of 11"
    assert len(pairs) == 2
