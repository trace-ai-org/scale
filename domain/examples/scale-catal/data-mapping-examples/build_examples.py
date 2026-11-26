"""Build the built-in-mapping example of the catalysis domain (all values invented).

    python build_examples.py

One synthetic data file per built-in mapping (domain/data-mappings/), written as a laboratory would keep it:

  co2rr-fe-wide.xlsx      a workbook: a notes sheet, the Faradaic efficiencies of CO2 electroreduction on Ag in a
                          wide table under a two-line preamble (FE CO (%), FE H2 (%), FE HCOO- (%), j CO (mA/cm2)), and
                          a GC calibration sheet
  thermal-tests.csv       CO2 methanation on Ni/Al2O3: semicolon-delimited, decimal commas, Windows-1252 (T (°C)),
                          a preamble; conversion of CO2 and selectivities to CH4 and CO
  kinetics.csv            rates and turnover frequencies of CO2 methanation with the basis the sites were counted on
  dft-energies.csv        CO, H and O adsorption energies on three surfaces, one calculation failed
  characterization.csv    BET area, pore volume, XRD crystallite size and XPS atomic ratios of three samples

and the documents:

  seed.json               the destination before the imports: a synthetic example that already holds CO2 and a
                          Faradaic-efficiency Variable (a lookup and a reference find them; the rest is created)
  <file>.map.yaml         the mapping `scale data match <file> --origin synthetic --namespace ex-dm` writes for it
  data-mappings.json      seed.json with the five files imported by their mappings (`scale data add`)

`scale data verify data-mappings.json` checks every cited cell; the workbook's and the semicolon file's tables are
extracted again from the original files by the recipe their Datasets record.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _repo() -> Path:
    for p in [HERE, *HERE.parents]:
        if (p / "scale-release.yaml").exists() and (p / "releases").is_dir():
            return p
    raise SystemExit("run inside a SCALE repository checkout")


REPO = _repo()
sys.path.insert(0, str(REPO / "tools"))

from scale import dataingest, datainput, datamatch, pgraph  # noqa: E402
from scale.util import load_yaml, write_canonical  # noqa: E402

NS = "ex-dm"
FILES = ("co2rr-fe-wide.xlsx", "thermal-tests.csv", "kinetics.csv", "dft-energies.csv", "characterization.csv")


def co2rr_workbook() -> bytes:
    fe = [
        ["CO2 electroreduction on Ag foil, 0.1 M KHCO3, H-cell (synthetic example, values invented)"],
        ["potentials iR-corrected, on the RHE scale"],
        [],
        ["Run", "Electrode", "E (V vs RHE)", "j total (mA/cm2)", "FE CO (%)", "FE H2 (%)", "FE HCOO- (%)",
         "j CO (mA/cm2)", "FE total (%)"],
        ["R1", "Ag-foil-1", -0.8, 2.1, 62.5, 30.1, 4.2, 1.31, 96.8],
        ["R2", "Ag-foil-1", -1.0, 5.3, 48.0, 44.3, "<0.5", 2.54, 92.3],
        ["R3", "Ag-foil-1", -1.2, 11.0, 21.7, 71.9, None, 2.39, 93.6],
        [],
        ["formate of R3 not analysed"],
    ]
    calib = [["Gas", "Retention time (min)", "Response factor"], ["H2", 1.2, 0.95], ["CO", 3.4, 1.02]]
    notes = [["Synthetic workbook for the SCALE data-mapping example; every value is invented."]]
    return datainput.write_xlsx([("Notes", notes), ("FE", fe), ("GC calibration", calib)])


def thermal_csv() -> bytes:
    text = ("# CO2 methanation, fixed bed, 100 mg catalyst (synthetic example, values invented)\n"
            "Operator;example\n"
            "\n"
            "Run;Catalyst;T (°C);P (bar);GHSV (h-1);X CO2 (%);S CH4 (%);S CO (%)\n"
            "T1;Ni-Al2O3-1;250;10;12000;18,4;97,1;2,9\n"
            "T2;Ni-Al2O3-1;300;10;12000;46,2;98,3;1,7\n"
            "T3;Ni-Al2O3-1;350;10;12000;71,5;98,9;1,1\n"
            "T4;Ni-Al2O3-1;400;10;12000;79,0;97,6;2,4\n")
    return text.replace("\n", "\r\n").encode("cp1252")


def kinetics_csv() -> bytes:
    return ("run,catalyst,species,T (K),P (bar),rate (mol g-1 s-1),TOF (s-1),site basis\n"
            "K1,Ni-Al2O3-1,CH4,493,1,1.2e-6,0.011,H2 chemisorption\n"
            "K2,Ni-Al2O3-1,CH4,503,1,2.0e-6,0.018,H2 chemisorption\n"
            "K3,Ni-Al2O3-1,CH4,513,1,3.3e-6,0.030,H2 chemisorption\n").encode("utf-8")


def dft_csv() -> bytes:
    return ("calc_id,surface,adsorbate,functional,status,E_ads (eV)\n"
            "C1,Ni(111),CO,RPBE,completed,-1.62\n"
            "C2,Ni(111),H,RPBE,completed,-0.47\n"
            "C3,Ni(211),CO,RPBE,completed,-1.88\n"
            "C4,Ni(211),O,RPBE,failed,\n").encode("utf-8")


def characterization_csv() -> bytes:
    return ("Sample,S_BET (m2/g),V_pore (cm3/g),d_XRD (nm),Ni/Al (XPS),O/Ni (XPS)\n"
            "Ni-Al2O3-1,182,0.41,6.2,0.21,1.8\n"
            "Ni-Al2O3-2,165,0.38,8.9,0.27,1.6\n"
            "Ni-Al2O3-3,141,0.35,,0.33,1.5\n").encode("utf-8")


DATA = {"co2rr-fe-wide.xlsx": co2rr_workbook, "thermal-tests.csv": thermal_csv, "kinetics.csv": kinetics_csv,
        "dft-energies.csv": dft_csv, "characterization.csv": characterization_csv}


def seed() -> dict:
    version = load_yaml(REPO / "domain/graph/scale-catal.yaml")["version"]
    syn = {"origin": "synthetic"}
    nodes = [
        {"id": f"{NS}:species/co2", "label": "ChemicalSpecies",
         "props": {"name": "CO2", "formula": "CO2", "charge": 0, "elemental_composition": {"C": 1, "O": 2}, **syn}},
        {"id": f"{NS}:variable/fe", "label": "Variable",
         "props": {"name": "Faradaic efficiency", "scale": "quantitative", "quantity_kind": "faradaic_efficiency",
                   "unit": "%", **syn}},
    ]
    return {"directed": True, "multigraph": True,
            "graph": {"schema": "scale-catal", "schema_version": version, "graph_id": f"{NS}:graph",
                      "title": "Five laboratory tables imported by the built-in mappings of scale data match (synthetic)",
                      "data_status": "synthetic_example", "follows_domains": ["catalysis"],
                      "stewardship": {"access": "public"}},
            "nodes": nodes, "links": []}


def mapping_for(name: str, data: bytes, spec: dict) -> str:
    res = datamatch.match(data, name, datamatch.load_templates(REPO), spec, origin="synthetic", namespace=NS)
    best = res["candidates"][0]
    if not best["complete"]:
        raise SystemExit(f"{name}: the best mapping ({best['template']}) is not complete: {best['problems']}")
    return best["mapping_yaml"]


def main() -> None:
    spec = pgraph.load_spec(REPO / "domain/graph/scale-catal.yaml")
    doc = seed()
    write_canonical(HERE / "seed.json", doc)
    for name in FILES:
        data = DATA[name]()
        (HERE / name).write_bytes(data)
        mapping = mapping_for(name, data, spec)
        (HERE / f"{Path(name).stem}.map.yaml").write_text(mapping, encoding="utf-8")
        doc = dataingest.plan(data, mapping.encode("utf-8"), doc, spec, name, uri=name)["document"]
    write_canonical(HERE / "data-mappings.json", doc)


if __name__ == "__main__":
    main()
