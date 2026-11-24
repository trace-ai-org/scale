"""Rule checks on a draft synthesis procedure, before it is reviewed: what a chemist would catch at a glance, by rules
and no model. The draft is JSON in the plan-synthesis procedure form (references/procedure-schema.json): its inputs
(name, formula, function, amount) and its steps in order (action, temperature_C, duration_min, atmosphere).

Each finding is an **error** (a rule that published procedures break only exceptionally: the draft would likely fail)
or a **warning** (a rule that real procedures break for good reasons: read the step again and say why it holds).

errors
  carbon_support_burns     a ready-made carbon support (Vulcan, Ketjen, carbon black, CNT, graphene) is heated above
                           350 °C in air or O2
  base_metal_oxidized      a target with Fe, Co, Ni, Cu, Mn, Zn, Sn, Ga or In is heated above 200 °C in air or O2 and
                           never reduced afterwards (H2, or a reduce step)
warnings
  precursor_per_metal      a metal of the target has no metal precursor that contains it
  precursor_amount         a metal precursor has no amount
  nothing_reduces          nothing reduces the metal precursors (no reducing agent, reducing solvent, H2, carbonyl,
                           galvanic, deposition, pyrolysis or electro step)
  too_few_electrons        the reducing agents give fewer electrons than the metals need (only when every amount and
                           every electron count is known, and no H2 or reducing solvent takes part)
  above_boiling_point      a wet step is hotter than the boiling point of every solvent, with no sealed vessel named
  no_ordering_step         an ordered intermetallic target has no thermal step at 250 °C or more outside air

    python3 -I check_chemistry.py <draft.json> --question <question.md>
    python3 -I check_chemistry.py <draft.json> --metals Pt,Zn --class ordered_intermetallic --support "Vulcan XC-72"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata

METALS = set("""Li Be Na Mg Al K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Cs Ba
La Ce Pr Nd Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi""".split())
BASE_METALS = {"Fe", "Co", "Ni", "Cu", "Mn", "Zn", "Sn", "Ga", "In"}
ELEMENT_WORDS = {"gold": "Au", "silver": "Ag", "platinum": "Pt", "palladium": "Pd", "copper": "Cu", "nickel": "Ni",
                 "cobalt": "Co", "iron": "Fe", "rhodium": "Rh", "ruthenium": "Ru", "iridium": "Ir", "zinc": "Zn",
                 "tin": "Sn", "bismuth": "Bi", "indium": "In", "gallium": "Ga", "molybdenum": "Mo", "tungsten": "W",
                 "manganese": "Mn", "chromium": "Cr", "cerium": "Ce", "lead": "Pb"}
WEIGHT = {"H": 1.008, "B": 10.81, "C": 12.011, "N": 14.007, "O": 15.999, "F": 18.998, "Na": 22.99, "Mg": 24.305,
          "Al": 26.982, "Si": 28.085, "P": 30.974, "S": 32.06, "Cl": 35.45, "K": 39.098, "Ti": 47.867, "Cr": 51.996,
          "Mn": 54.938, "Fe": 55.845, "Co": 58.933, "Ni": 58.693, "Cu": 63.546, "Zn": 65.38, "Ga": 69.723,
          "Br": 79.904, "Mo": 95.95, "Ru": 101.07, "Rh": 102.91, "Pd": 106.42, "Ag": 107.87, "In": 114.82,
          "Sn": 118.71, "I": 126.9, "Ce": 140.12, "W": 183.84, "Re": 186.21, "Ir": 192.22, "Pt": 195.08,
          "Au": 196.97, "Pb": 207.2, "Bi": 208.98}
LIGANDS = {"acac": {"C": 5, "H": 7, "O": 2}, "OAc": {"C": 2, "H": 3, "O": 2}, "Ac": {"C": 2, "H": 3, "O": 2}}
SOLVENT_BP = {"water": 100, "ethanol": 78, "methanol": 65, "isopropanol": 82, "ethylene glycol": 197,
              "diethylene glycol": 245, "triethylene glycol": 285, "tetraethylene glycol": 314, "glycerol": 290,
              "benzyl alcohol": 205, "dmf": 153, "n,n-dimethylformamide": 153, "toluene": 111, "hexane": 69,
              "chloroform": 61, "acetone": 56, "1-octadecene": 315, "octadecene": 315, "oleylamine": 350,
              "dibenzyl ether": 298, "benzyl ether": 298, "diphenyl ether": 258, "1,2-propanediol": 188,
              "dimethyl sulfoxide": 189, "dmso": 189, "acetonitrile": 82, "thf": 66}
SOLVENT_ALIAS = {"deionized water": "water", "distilled water": "water", "di water": "water", "h2o": "water",
                 "ultrapure water": "water", "eg": "ethylene glycol", "deg": "diethylene glycol",
                 "teg": "tetraethylene glycol", "ode": "1-octadecene", "ola": "oleylamine", "oam": "oleylamine",
                 "etoh": "ethanol", "meoh": "methanol", "ipa": "isopropanol"}
REDUCING_SOLVENT = re.compile(r"oleylamine|ethylene glycol|diethylene glycol|triethylene glycol|tetraethylene|glycerol|"
                              r"polyol|\bdmf\b|dimethylformamide|benzyl alcohol|hexadecanediol|propanediol|butanediol|"
                              r"octadecylamine|hexadecylamine|dodecylamine|trioctylamine|formamide", re.I)
H2 = re.compile(r"\bh2\b|hydrogen|forming gas", re.I)
AIR = re.compile(r"\bair\b|\bo2\b|oxygen", re.I)
NOT_AIR = re.compile(r"air[- ]?free|no air|without air|absence of air|exclu\w* air|air[- ]?tight", re.I)
SEALED = re.compile(r"autoclave|sealed|solvothermal|hydrothermal|teflon|pressure|microwave|bomb", re.I)
CARBON = re.compile(r"vulcan|ketjen|xc-?72|carbon black|\bcnts?\b|carbon nanotube|graphene|\brgo\b|graphite|"
                    r"acetylene black|super p|black pearls|activated carbon", re.I)
E_RED = {"nabh4": 8, "sodium borohydride": 8, "kbh4": 8, "hydrazine": 4, "n2h4": 4, "hydrazine hydrate": 4,
         "ascorbic acid": 2, "l-ascorbic acid": 2, "sodium ascorbate": 2, "trisodium citrate": 2, "sodium citrate": 2,
         "formaldehyde": 2, "formic acid": 2, "glucose": 2, "hydroquinone": 2, "tert-butylamine borane": 6,
         "ammonia borane": 6, "dimethylamine borane": 6}
MW_RED = {"nabh4": 37.83, "sodium borohydride": 37.83, "kbh4": 53.94, "hydrazine": 32.05, "n2h4": 32.05,
          "hydrazine hydrate": 50.06, "ascorbic acid": 176.12, "l-ascorbic acid": 176.12, "sodium ascorbate": 198.11,
          "trisodium citrate": 258.07, "sodium citrate": 258.07, "glucose": 180.16, "formaldehyde": 30.03,
          "formic acid": 46.03, "hydroquinone": 110.11, "tert-butylamine borane": 86.97, "ammonia borane": 30.87,
          "dimethylamine borane": 58.92}
OXIDATION = [(r"haucl4|aucl3", 3), (r"agno3|agoac|ag2so4|agclo4", 1), (r"h2ptcl6|k2ptcl6|ptcl4", 4),
             (r"k2ptcl4|na2ptcl4|pt\(acac\)2|ptcl2", 2), (r"pdcl2|na2pdcl4|k2pdcl4|pd\(acac\)2|pd\(oac\)2|pd\(no3\)2", 2),
             (r"h2ircl6|ircl4", 4), (r"ircl3|ir\(acac\)3|rucl3|ru\(acac\)3|rhcl3|rh\(acac\)3", 3),
             (r"fecl3|fe\(acac\)3|fe\(no3\)3|co\(acac\)3", 3),
             (r"fecl2|feso4|cocl2|co\(acac\)2|co\(no3\)2|co\(oac\)2|nicl2|ni\(acac\)2|ni\(no3\)2|ni\(oac\)2|"
              r"cucl2|cu\(acac\)2|cu\(no3\)2|cu\(oac\)2|cuso4|zncl2|zn\(acac\)2|zn\(no3\)2|sncl2", 2)]
UNITS = {"mol": 1.0, "mmol": 1e-3, "umol": 1e-6, "µmol": 1e-6, "μmol": 1e-6, "g": None, "mg": None, "kg": None}
MASS = {"g": 1.0, "mg": 1e-3, "kg": 1e3, "ug": 1e-6, "µg": 1e-6}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").replace("·", "·")
    return re.sub(r"\s+", " ", s).strip().lower()


def formula_elements(f: str) -> dict | None:
    """Element counts of a simple formula (HAuCl4·3H2O, Cu(acac)2, K2PtCl4); None when it is not one."""
    if not f:
        return None
    body = re.split(r"[·•*.]\s*x?\d*H2O|·x?H2O", f.replace(" ", ""))[0]
    for lig, comp in LIGANDS.items():
        body = re.sub(rf"(?<![A-Za-z]){lig}(?![a-z])", "(" + "".join(f"{e}{n}" for e, n in comp.items()) + ")", body)
    while "(" in body:
        new = re.sub(r"\(([^()]*)\)(\d*\.?\d*)", lambda m: "".join(
            f"{e}{float(n or 1) * float(m.group(2) or 1):g}" for e, n in re.findall(r"([A-Z][a-z]?)(\d*\.?\d*)", m.group(1))),
            body)
        if new == body:
            return None
        body = new
    toks = re.findall(r"([A-Z][a-z]?)(\d*\.?\d*)", body)
    if not toks or "".join(e + n for e, n in toks) != body or any(e not in WEIGHT for e, _ in toks):
        return None
    out = {}
    for e, n in toks:
        out[e] = out.get(e, 0) + float(n or 1)
    return out


def metals_of(inp: dict) -> set:
    out = set()
    for s in (inp.get("formula"), inp.get("name")):
        c = formula_elements(s or "")
        if c:
            out |= {e for e in c if e in METALS}
        for tok in re.findall(r"[A-Z][A-Za-z0-9()·.\[\]]*", s or ""):
            syms = re.findall(r"[A-Z][a-z]?", tok)
            if syms and all(x in METALS or x in WEIGHT for x in syms):
                out |= {x for x in syms if x in METALS}
        out |= {ELEMENT_WORDS[w] for w in re.findall(r"[a-z]+", (s or "").lower()) if w in ELEMENT_WORDS}
    return out


def moles(inp: dict, mw: float | None = None) -> float | None:
    a = inp.get("amount") or {}
    v, u = a.get("value"), norm(a.get("unit") or "")
    if not isinstance(v, (int, float)):
        return None
    if u in UNITS and UNITS[u]:
        return v * UNITS[u]
    if u in MASS:
        if mw is None:
            c = formula_elements(inp.get("formula") or inp.get("name") or "")
            mw = sum(WEIGHT[e] * n for e, n in c.items()) if c else None
        return v * MASS[u] / mw if mw else None
    return None


def oxidation(inp: dict) -> int | None:
    s = norm((inp.get("formula") or "") + " " + (inp.get("name") or "")).replace(" ", "")
    for pat, n in OXIDATION:
        if re.search(pat, s):
            return n
    return None


def is_air(atm) -> bool:
    return bool(AIR.search(NOT_AIR.sub(" ", str(atm or ""))))


def check(proc: dict, metals: set, cls: str = "", support: str = "", text: str = "") -> list[dict]:
    inputs, steps = proc.get("inputs") or [], proc.get("steps") or []
    precs = [i for i in inputs if i.get("function") == "metal_precursor"]
    reds = [i for i in inputs if i.get("function") == "reducing_agent"]
    names = " ".join(norm((i.get("name") or "") + " " + (i.get("formula") or "")) for i in inputs)
    atms = " ".join(str(s.get("atmosphere") or "") for s in steps)
    acts = {s.get("action") for s in steps}
    hot = lambda s, t: isinstance(s.get("temperature_C"), (int, float)) and s["temperature_C"] > t  # noqa: E731
    out = []

    def add(level, rule, why, step=None):
        out.append({"level": level, "rule": rule, "why": why, **({"step": step} if step is not None else {})})

    if any(i.get("function") == "support" and CARBON.search(i.get("name") or "") for i in inputs):
        for k, s in enumerate(steps, 1):
            if hot(s, 350) and is_air(s.get("atmosphere")):
                add("error", "carbon_support_burns", "a carbon support is heated above 350 °C in air: it burns", k)
    if metals & BASE_METALS:
        for k, s in enumerate(steps, 1):
            if hot(s, 200) and is_air(s.get("atmosphere")) and not any(
                    H2.search(str(t.get("atmosphere") or "")) or t.get("action") == "reduce" for t in steps[k:]):
                add("error", "base_metal_oxidized", f"{', '.join(sorted(metals & BASE_METALS))} heated above 200 °C "
                    "in air and never reduced afterwards: an oxide, not the metal", k)
    have = set().union(*(metals_of(i) for i in precs)) if precs else set()
    for m in sorted(metals - have):
        add("warning", "precursor_per_metal", f"no metal precursor contains {m}")
    for i in precs:
        if not isinstance((i.get("amount") or {}).get("value"), (int, float)):
            add("warning", "precursor_amount", f"{i.get('name')} has no amount")
    red_solvent = bool(REDUCING_SOLVENT.search(names))
    has_h2 = bool(H2.search(atms) or H2.search(names))
    if metals and precs and not (reds or red_solvent or has_h2 or re.search(r"\(co\)|carbonyl", names)
                                 or acts & {"reduce", "replace", "deposit", "pyrolyze"}
                                 or re.search(r"electro|galvanic|plasma|laser|radiolysis|photo", text, re.I)):
        add("warning", "nothing_reduces", "no reducing agent, reducing solvent, H2 or reducing step")
    if precs and reds and not (has_h2 or red_solvent):
        need = give = 0.0
        countable = True
        for i in precs:
            n, ox = moles(i), oxidation(i)
            if n is None or ox is None:
                countable = False
                break
            need += n * ox
        for r in reds:
            k = norm(r.get("name") or "")
            n = moles(r, MW_RED.get(k))
            if k not in E_RED or n is None:
                countable = False
                break
            give += n * E_RED[k]
        if countable and need > 0 and give < need:
            add("warning", "too_few_electrons", f"the reducing agents give {give / need:.2f} of the electrons the "
                "metals need")
    sols = [SOLVENT_ALIAS.get(norm(i.get("name") or ""), norm(i.get("name") or "")) for i in inputs
            if i.get("function") == "solvent"]
    bps = [SOLVENT_BP[s] for s in sols if s in SOLVENT_BP]
    if sols and len(bps) == len(sols) and not SEALED.search(text + " " + json.dumps(steps)):
        for k, s in enumerate(steps, 1):
            if (hot(s, max(bps) + 15) and s.get("action") not in ("calcine", "anneal", "dry", "pyrolyze", "evaporate")
                    and not re.search(r"h2|hydrogen|vacuum", str(s.get("atmosphere") or ""), re.I)):
                add("warning", "above_boiling_point", f"{s['temperature_C']} °C is above the boiling point of every "
                    f"solvent ({max(bps)} °C at most) and no sealed vessel is named", k)
    if cls == "ordered_intermetallic" and not any(
            isinstance(s.get("temperature_C"), (int, float)) and s["temperature_C"] >= 250
            and not is_air(s.get("atmosphere")) for s in steps):
        add("warning", "no_ordering_step", "an ordered intermetallic is asked, and no step heats to 250 °C or more "
            "outside air")
    return out


def target_from_question(q: str) -> tuple[set, str, str]:
    spec = {}
    for line in q.splitlines():
        m = re.match(r"^- ([^:]+):\s*(.*)$", line.strip())
        if m:
            spec[m.group(1).strip()] = m.group(2).strip()
    comp = next((v for k, v in spec.items() if k.startswith("Composition")), "")
    metals = {e for e in re.findall(r"([A-Z][a-z]?)\s*=", comp) if e in METALS}
    return metals, spec.get("Material class", ""), spec.get("Support", "")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("draft"); ap.add_argument("--question"); ap.add_argument("--metals", default="")
    ap.add_argument("--class", dest="cls", default=""); ap.add_argument("--support", default="")
    ap.add_argument("--text", default="", help="the draft's text (markdown), for the sealed-vessel and electro words")
    a = ap.parse_args(argv)
    metals, cls, support = set(filter(None, a.metals.split(","))), a.cls, a.support
    if a.question:
        qm, qc, qs = target_from_question(open(a.question, encoding="utf-8").read())
        metals, cls, support = metals or qm, cls or qc, support or qs
    text = open(a.text, encoding="utf-8").read() if a.text else ""
    found = check(json.load(open(a.draft, encoding="utf-8")), metals, cls, support, text)
    print(json.dumps({"target": {"metals": sorted(metals), "class": cls, "support": support},
                      "errors": [f for f in found if f["level"] == "error"],
                      "warnings": [f for f in found if f["level"] == "warning"]}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
