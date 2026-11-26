"""Build the data-ingest example of the catalysis domain (all values invented).

    python build_examples.py

  seed.json
    What two tables refer to, before they are imported: the species, the reactions (CO2 to CO; CO and H adsorption),
    a Ag catalyst sample and its electrolyte, the two assays (GC of the outlet gas, NMR of the catholyte), the Variables
    the result columns hold (a Faradaic efficiency, a reaction energy) and the input and environment datasets of the
    calculations.
  data-ingest.json
    seed.json with co2rr-fe.csv and dft-adsorption.csv imported by their mappings (`scale data add`): a Dataset per
    file with its sha256 and table, three CatalyticTests and three Computations, each value citing its cell
    (evidence.dataset_cell), a formate efficiency below the detection limit ("<0.5"), one not measured (a blank) and
    a failed calculation whose energies are not measured, never zero.

`scale data verify data-ingest.json` checks the files against the Datasets and each cited cell against its value.
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

from scale import dataingest, pgraph  # noqa: E402
from scale.util import load_yaml, write_canonical  # noqa: E402

P = "ex:di/"
SYN = "synthetic"


def node(local: str, label: str, name: str, **props) -> dict:
    return {"id": P + local, "label": label, "props": {"name": name, "origin": SYN, **props}}


def link(n: int, type_: str, source: str, target: str, **props) -> dict:
    return {"id": f"{P}link/{n}", "type": type_, "source": P + source, "target": P + target,
            "props": {"origin": SYN, **props}}


def species(local: str, formula: str, comp: dict, charge: int = 0, **props) -> dict:
    return node(f"species/{local}", "ChemicalSpecies", formula, formula=formula, elemental_composition=comp,
                charge=charge, **props)


def seed() -> dict:
    version = load_yaml(REPO / "domain/graph/scale-catal.yaml")["version"]
    nodes = [
        species("co2", "CO2", {"C": 1, "O": 2}),
        species("co", "CO", {"C": 1, "O": 1}),
        species("h2", "H2", {"H": 2}),
        species("h", "H", {"H": 1}),
        species("h2o", "H2O", {"H": 2, "O": 1}),
        species("hydroxide", "OH-", {"H": 1, "O": 1}, -1, species_kind="ion"),
        species("formate", "HCOO-", {"C": 1, "H": 1, "O": 2}, -1, species_kind="ion"),
        species("electron", "e-", {}, -1, species_kind="other"),
        node("reaction/co2-to-co", "Reaction", "CO2 + H2O + 2 e- -> CO + 2 OH-", equation="CO2 + H2O + 2 e- -> CO + 2 OH-",
             direction="forward", balance_status="balanced"),
        node("reaction/co-adsorption", "Reaction", "CO(g) + * -> CO*", equation="CO(g) + * -> CO*", direction="forward",
             balance_status="balanced"),
        node("reaction/h-adsorption", "Reaction", "1/2 H2(g) + * -> H*", equation="1/2 H2(g) + * -> H*",
             direction="forward", balance_status="balanced"),
        node("catalyst/ag", "Catalyst", "Ag nanoparticles", catalyst_family="metal", nominal_formula="Ag"),
        node("sample/ag", "CatalystSample", "Ag nanoparticles, batch 1", sample_kind="powder"),
        node("electrolyte/khco3", "Electrolyte", "0.1 M KHCO3, CO2-saturated",
             pH={"status": "present", "unit": "1", "value": 6.8}),
        node("assay/gc", "Characterization", "Online GC of the outlet gas", technique="gas chromatography"),
        node("assay/nmr", "Characterization", "1H NMR of the catholyte after electrolysis", technique="1H NMR"),
        node("variable/faradaic-efficiency", "Variable", "Faradaic efficiency", scale="quantitative",
             quantity_kind="faradaic_efficiency", unit="%"),
        node("variable/adsorption-energy", "Variable", "adsorption energy (electronic)", scale="quantitative",
             quantity_kind="reaction_energy", unit="eV"),
        node("dataset/dft-inputs", "Dataset", "DFT input files (invented)", data_kind="configuration",
             availability="available", uri="https://example.org/invented/dft-inputs", version="1",
             sha256="5f0d0c9e7a35d5a4c3d0f5f8e0b7a1b2c3d4e5f60718293a4b5c6d7e8f901234"),
        node("dataset/dft-environment", "Dataset", "DFT container image (invented)", data_kind="environment",
             availability="available", uri="https://example.org/invented/dft-environment", version="1",
             sha256="0a1b2c3d4e5f60718293a4b5c6d7e8f9f0e1d2c3b4a5968778695a4b3c2d1e0f"),
    ]
    part = lambda n, rx, sp, coef, role, **kw: link(n, "HAS_PARTICIPANT", f"reaction/{rx}", f"species/{sp}",  # noqa: E731
                                                     coefficient=coef, role=role, **kw)
    links = [
        part(1, "co2-to-co", "co2", 1, "reactant"), part(2, "co2-to-co", "h2o", 1, "reactant"),
        part(3, "co2-to-co", "electron", 2, "reactant"), part(4, "co2-to-co", "co", 1, "product"),
        part(5, "co2-to-co", "hydroxide", 2, "product"),
        part(6, "co-adsorption", "co", 1, "reactant", phase="gas"), part(7, "co-adsorption", "co", 1, "product",
                                                                          phase="adsorbed"),
        part(8, "h-adsorption", "h2", 0.5, "reactant", phase="gas"), part(9, "h-adsorption", "h", 1, "product",
                                                                           phase="adsorbed"),
        link(10, "HAS_FORMULATION", "sample/ag", "catalyst/ag"),
    ]
    return {"directed": True, "multigraph": True,
            "graph": {"schema": "scale-catal", "schema_version": version, "graph_id": P + "graph",
                      "title": "Faradaic efficiencies of CO2 electroreduction and DFT adsorption energies imported from "
                               "two tables (synthetic)",
                      "data_status": "synthetic_example", "follows_domains": ["catalysis"],
                      "stewardship": {"access": "public"}},
            "nodes": nodes, "links": links}


def imported(doc: dict) -> dict:
    spec = pgraph.load_spec(REPO / "domain/graph/scale-catal.yaml")
    for csv, mapping in (("co2rr-fe.csv", "co2rr-fe.map.yaml"), ("dft-adsorption.csv", "dft-adsorption.map.yaml")):
        doc = dataingest.plan((HERE / csv).read_bytes(), (HERE / mapping).read_bytes(), doc, spec, csv)["document"]
    return doc


def main() -> None:
    s = seed()
    write_canonical(HERE / "seed.json", s)
    write_canonical(HERE / "data-ingest.json", imported(s))


if __name__ == "__main__":
    main()
