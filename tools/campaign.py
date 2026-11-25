"""Writing an experiment-design agent's proposals and their results back into a scale-catal document, so that a
campaign stays auditable (docs/querying.md, "Writing proposals and results back").

The convention uses kernel records only:

* a **proposal** is an experiment (an Experiment or a label that is_a it, such as CatalyticTest) with
  ``experiment_status: proposed``, ``origin: hypothesized`` and ``record_version: 1``, PART_OF_DESIGN the campaign's
  Design with its ``iteration``, PROPOSED_BY the agent (a Software, Model or Person record) with ``proposed_at``, the
  rationale and the acquisition value, and AIMS_TO_TEST the Prediction it was chosen to test;
* the **prediction** is a Prediction (``origin: hypothesized``, ``proposed_at``, the variable, the expected value with
  ``origin: predicted``, the direction), PROPOSED_BY the agent too and, when a hypothesis is named, DERIVES from it;
* a **result** is a new record of the proposal's label, ``origin: recorded`` with ``recorded_at``, its own status
  (completed, failed, aborted, with the reason), ``record_version`` one more than the proposal's, that REVISES the
  proposal (the proposal is kept as it was: what was proposed stays readable after it was run), PART_OF_DESIGN the
  same iteration, and TESTS every Prediction the proposal AIMS_TO_TEST, with the outcome and the results it rests on.

A failed run is recorded like any other; a result recorded before its prediction was proposed is refused (it would be
post hoc). A design matrix (:mod:`designmatrix`) reads results only: proposals, superseded records and predictions
are never observations. A person's approval of a proposal is a separate decision (PROPOSED_BY.human_decision).
"""

from __future__ import annotations

import copy
from typing import Any

from .util import ScaleError

AGENT_LABELS = ("Software", "Model", "StructureModel", "Person", "Institution")
EXPERIMENT_LABELS = ("Experiment", "Characterization", "Computation", "Analysis", "CatalyticTest",
                     "ThermalContributionAnalysis")
DONE = ("completed", "failed", "aborted")


def _index(doc: dict) -> tuple[dict, set]:
    return {n["id"]: n for n in doc["nodes"]}, {e["id"] for e in doc["links"]}


def _need(nodes: dict, rid: str, labels: tuple, what: str) -> dict:
    n = nodes.get(rid)
    if n is None or n["label"] not in labels:
        raise ScaleError(f"{what} {rid!r} is not a {' or '.join(labels)} record of the document")
    return n


def _new_id(ids: set, rid: str) -> str:
    if rid in ids:
        raise ScaleError(f"the id {rid!r} is already used in the document")
    ids.add(rid)
    return rid


def propose(doc: dict, *, design: str, iteration: int, agent: str, proposed_at: str, proposals: list[dict]) -> dict:
    """A copy of ``doc`` with ``proposals`` added under the convention above. Each proposal is ``{id, label (default
    Experiment), name, props (its conditions), rationale, acquisition_value, human_decision, prediction: {id, name,
    statement, variable, expected (a value object), direction}, hypothesis (an id)}``. ``proposed_at`` is an ISO
    datetime with its offset."""
    out = copy.deepcopy(doc)
    nodes, link_ids = _index(out)
    ids = set(nodes) | link_ids
    _need(nodes, design, ("Design",), "design")
    _need(nodes, agent, AGENT_LABELS, "agent")
    if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 0:
        raise ScaleError("iteration is a whole number (the round of the campaign)")
    day = str(proposed_at)[:10]
    for p in proposals:
        label = p.get("label", "Experiment")
        if label not in EXPERIMENT_LABELS:
            raise ScaleError(f"a proposal is an experiment ({', '.join(EXPERIMENT_LABELS)}), not {label}")
        pred = p.get("prediction")
        if not isinstance(pred, dict) or not pred.get("id"):
            raise ScaleError(f"proposal {p.get('id')}: states the prediction it is chosen to test (prediction.id)")
        if pred.get("variable"):
            _need(nodes, pred["variable"], ("Variable",), "prediction variable")
        props = {k: v for k, v in (p.get("props") or {}).items()
                 if k not in ("origin", "experiment_status", "record_version", "recorded_at")}
        eid = _new_id(ids, p["id"])
        out["nodes"].append({"id": eid, "label": label, "props": {
            "name": p.get("name") or eid, **props, "origin": "hypothesized", "experiment_status": "proposed",
            "record_version": 1}})
        pid = _new_id(ids, pred["id"])
        expected = dict(pred["expected"], origin="predicted") if isinstance(pred.get("expected"), dict) else None
        out["nodes"].append({"id": pid, "label": "Prediction", "props": {
            "name": pred.get("name") or pid, "origin": "hypothesized", "proposed_at": day,
            **({"statement": pred["statement"]} if pred.get("statement") else {}),
            **({"variable": pred["variable"]} if pred.get("variable") else {}),
            **({"expected": expected} if expected else {}),
            **({"direction": pred["direction"]} if pred.get("direction") else {})}})
        why = {k: p[k] for k in ("rationale", "acquisition_value", "human_decision") if p.get(k) is not None}
        rec = {"origin": "recorded", "recorded_at": proposed_at}
        out["links"] += [
            {"id": _new_id(ids, f"{eid}/proposed-by"), "type": "PROPOSED_BY", "source": eid, "target": agent,
             "props": {**rec, "proposed_at": proposed_at, **why}},
            {"id": _new_id(ids, f"{pid}/proposed-by"), "type": "PROPOSED_BY", "source": pid, "target": agent,
             "props": {**rec, "proposed_at": proposed_at}},
            {"id": _new_id(ids, f"{eid}/part-of-design"), "type": "PART_OF_DESIGN", "source": eid, "target": design,
             "props": {**rec, "iteration": iteration}},
            {"id": _new_id(ids, f"{eid}/aims-to-test"), "type": "AIMS_TO_TEST", "source": eid, "target": pid,
             "props": dict(rec)}]
        if p.get("hypothesis"):
            _need(nodes, p["hypothesis"], ("Hypothesis",), "hypothesis")
            out["links"].append({"id": _new_id(ids, f"{p['hypothesis']}/derives/{pid}"), "type": "DERIVES",
                                 "source": p["hypothesis"], "target": pid, "props": dict(rec)})
    return out


def record_result(doc: dict, *, proposal: str, result_id: str, recorded_at: str, status: str = "completed",
                  props: dict | None = None, outcome: str = "not_reported", result_refs: list[dict] | None = None,
                  status_reason: str | None = None, evidence: list[dict] | None = None) -> dict:
    """A copy of ``doc`` with the result of ``proposal`` appended under the convention above: a new record
    ``result_id`` (``origin: recorded``, ``recorded_at``, ``experiment_status`` ``status``) holding the proposal's
    conditions updated by ``props`` (its measured results, ``result_values`` with their ``evidence_ids``), with
    ``evidence`` (the items those ids name, citing the run's Dataset), REVISES the proposal, PART_OF_DESIGN its
    iteration, TESTS each Prediction the proposal AIMS_TO_TEST with ``outcome`` and ``result_refs``. A proposal that
    already has a result, a record that is not a proposal and a result recorded before its prediction was proposed are
    refused."""
    if status not in DONE:
        raise ScaleError(f"a result's status is one of {DONE}, not {status!r}")
    if status in ("failed", "aborted") and not status_reason:
        raise ScaleError("a failed or aborted run states why (status_reason): it is kept, never dropped")
    out = copy.deepcopy(doc)
    nodes, link_ids = _index(out)
    ids = set(nodes) | link_ids
    prop = _need(nodes, proposal, EXPERIMENT_LABELS, "proposal")
    pp = prop.get("props") or {}
    if pp.get("experiment_status") not in ("proposed", "planned"):
        raise ScaleError(f"{proposal} is not a proposal (experiment_status {pp.get('experiment_status')!r})")
    if any(e["type"] == "REVISES" and e["target"] == proposal for e in out["links"]):
        raise ScaleError(f"{proposal} already has a result (a record REVISES it); a repeat is a new proposal")
    preds = [e["target"] for e in out["links"] if e["type"] == "AIMS_TO_TEST" and e["source"] == proposal]
    for pid in preds:
        when = (nodes[pid].get("props") or {}).get("proposed_at")
        if when and str(recorded_at)[:10] < str(when)[:10]:
            raise ScaleError(f"the result is recorded on {recorded_at[:10]}, before the prediction {pid} was proposed "
                             f"({when}): a prediction proposed after its result is post hoc")
    design = [e for e in out["links"] if e["type"] == "PART_OF_DESIGN" and e["source"] == proposal]
    base = {k: v for k, v in pp.items() if k not in ("origin", "experiment_status", "record_version", "name")}
    rid = _new_id(ids, result_id)
    new_props: dict[str, Any] = {**base, **(props or {}), "name": (props or {}).get("name") or f"{pp.get('name', proposal)} (run)",
                                 "origin": "recorded", "recorded_at": recorded_at, "experiment_status": status,
                                 "record_version": int(pp.get("record_version") or 1) + 1}
    if status_reason:
        new_props["status_reason"] = status_reason
    if evidence:
        new_props["evidence"] = list(new_props.get("evidence") or []) + list(evidence)
    out["nodes"].append({"id": rid, "label": prop["label"], "props": new_props})
    rec = {"origin": "recorded", "recorded_at": recorded_at}
    out["links"].append({"id": _new_id(ids, f"{rid}/revises"), "type": "REVISES", "source": rid, "target": proposal,
                         "props": dict(rec)})
    cited: dict[str, list] = {}                      # the run log (a Dataset) or source each evidence item cites
    for ev in new_props.get("evidence") or []:
        if isinstance(ev, dict) and isinstance(ev.get("source"), str):
            cited.setdefault(ev["source"], []).append(ev.get("locator"))
    for src, locs in sorted(cited.items()):
        out["links"].append({"id": _new_id(ids, f"{rid}/reported-in/{src.rsplit('/', 1)[-1]}"), "type": "REPORTED_IN",
                             "source": rid, "target": src,
                             "props": {"origin": "derived", "locators": sorted({x for x in locs if isinstance(x, str)})}})
    for e in design:
        out["links"].append({"id": _new_id(ids, f"{rid}/part-of-design"), "type": "PART_OF_DESIGN", "source": rid,
                             "target": e["target"], "props": {**rec, **{k: v for k, v in (e.get("props") or {}).items()
                                                                        if k not in ("origin", "recorded_at")}}})
    for pid in preds:
        out["links"].append({"id": _new_id(ids, f"{rid}/tests/{pid.rsplit('/', 1)[-1]}"), "type": "TESTS", "source": rid,
                             "target": pid, "props": {**rec, **({"outcome": outcome} if status == "completed" else
                                                                {"outcome": "inconclusive", "error": "incomplete_data"}),
                                                      **({"result_refs": result_refs} if result_refs else {})}})
    return out
