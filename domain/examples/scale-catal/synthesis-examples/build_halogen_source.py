"""Build the synthetic halogen-source example of the catalysis domain (all values invented; scale-catal 0.8.3).

    python build_halogen_source.py

  halogen-source.json
    Two copper catalysts for CO2 electroreduction, from one invented paper, that tell a halogen source from a halide
    that is only a counter-ion:
      Cl-modified Cu    Cu(NO3)2 reduced by NaBH4 in water with HCl added; the chloride stays in the catalyst (XPS), so
                        HCl is a halogen_source (and the step's ph_modifier, as the paper states both)
      Cu from CuCl2     CuCl2 reduced by NaBH4 and washed; its chloride is only the precursor's counter-ion and is
                        washed out, so CuCl2 is a metal_precursor and no halogen_source
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build_examples import Doc, V, write_canonical  # noqa: E402


def halogen_source() -> dict:
    d = Doc("ex:hal", "Copper catalysts with and without retained chloride: a halogen source and a counter-ion "
                      "(synthetic)")
    d.source("cl-cu", "Chloride retained in copper catalysts for CO2 electroreduction")
    cu = d.material("cu-fcc", "fcc copper", formula="Cu", structure_type="fcc", material_family="metal",
                    crystallinity="crystalline", evidence=[d.ev("Results, XRD")])
    cuno3 = d.species("cuno3", "Cu(NO3)2", {"Cu": 1, "N": 2, "O": 6}, "copper(II) nitrate")
    cucl2 = d.species("cucl2", "CuCl2", {"Cu": 1, "Cl": 2}, "copper(II) chloride")
    hcl = d.species("hcl", "HCl", {"H": 1, "Cl": 1}, "hydrochloric acid")
    nabh4 = d.species("nabh4", "NaBH4", {"Na": 1, "B": 1, "H": 4}, "sodium borohydride")
    water = d.species("water", "H2O", {"H": 2, "O": 1}, "water")

    def catalyst(key, name):
        cat = d.node(f"catalyst/{key}", "Catalyst", name, catalyst_family="metal", nominal_formula="Cu",
                     evidence=[d.ev("Experimental, Synthesis")])
        d.link("HAS_COMPONENT", cat, cu, role="active_phase", evidence=[d.ev("Experimental, Synthesis")])
        powder = d.sample(key, f"{name}, as prepared", label="CatalystSample", sample_kind="powder")
        d.link("HAS_FORMULATION", powder, cat)
        return powder

    # Cl-modified Cu: HCl supplies the chloride that stays in the catalyst
    cl_cu = catalyst("cl-cu", "Cl-modified Cu")
    prep = d.preparation("cl-cu", "Cl-modified Cu by borohydride reduction with HCl", ["other"],
                         "Cu(NO3)2 reduced by NaBH4 in water containing HCl, washed and dried",
                         design_aims=["composition"])
    d.link("HAS_OUTPUT", prep, cl_cu)
    d.step(prep, 1, "cl-cu-dissolve", "Dissolve Cu(NO3)2 in water and add HCl", "dissolve",
           inputs=[(cuno3, "reagent", V(1.0, "mmol"), ["metal_precursor"]),
                   (hcl, "reagent", V(0.5, "mmol"), ["halogen_source", "ph_modifier"]),
                   (water, "material", V(50, "mL"), ["solvent"])])
    d.step(prep, 2, "cl-cu-reduce", "Add NaBH4 dropwise", "add",
           inputs=[(nabh4, "reagent", V(4.0, "mmol"), ["reducing_agent"], {"addition_mode": "dropwise"})])
    d.step(prep, 3, "cl-cu-wash", "Wash with water and dry", "wash", outputs=[cl_cu],
           inputs=[(water, "material", V(100, "mL"), ["solvent"])])
    xps = d.characterization("cl-cu-xps", "XPS of the Cl-modified Cu", "X-ray photoelectron spectroscopy", cl_cu,
                             "as_prepared", "Figure 2", information_depth="surface", sampling="ensemble")
    d.state("cl-cu", "Cl-modified Cu as prepared, chloride at the surface", cl_cu, prep, [xps], "Figure 2",
            architecture=["nanoparticles", "unsupported"])
    d.claim("cl-retained", "The chloride from HCl remains in the copper catalyst after washing.", "finding",
            [(prep, "preparation"), (cl_cu, "sample"), (hcl, "agent")], "Results, XPS", supported_by=[xps])

    # Cu from CuCl2: the chloride is the precursor's counter-ion, washed out, no halogen source
    plain = catalyst("cu-from-cucl2", "Cu from CuCl2")
    prep2 = d.preparation("cu-from-cucl2", "Cu by borohydride reduction of CuCl2", ["other"],
                          "CuCl2 reduced by NaBH4 in water, washed free of chloride and dried")
    d.link("HAS_OUTPUT", prep2, plain)
    d.step(prep2, 1, "cucl2-dissolve", "Dissolve CuCl2 in water", "dissolve",
           inputs=[(cucl2, "reagent", V(1.0, "mmol"), ["metal_precursor"]),
                   (water, "material", V(50, "mL"), ["solvent"])])
    d.step(prep2, 2, "cucl2-reduce", "Add NaBH4 dropwise", "add",
           inputs=[(nabh4, "reagent", V(4.0, "mmol"), ["reducing_agent"], {"addition_mode": "dropwise"})])
    d.step(prep2, 3, "cucl2-wash", "Wash with water until free of chloride and dry", "wash", outputs=[plain],
           inputs=[(water, "material", V(200, "mL"), ["solvent"])])
    return d.finish()


if __name__ == "__main__":
    write_canonical(HERE / "halogen-source.json", halogen_source())
    print("wrote halogen-source.json")
