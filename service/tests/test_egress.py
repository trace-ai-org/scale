"""Egress in the SCALE service: a graph whose records are not public reaches no model (a mocked provider sink here)
unless the service's own policy approves the endpoint for their levels (docs/private-data.md)."""

import json
import sys
from types import SimpleNamespace

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "service"))                 # the scaleweb package, not installed with scale-catal
from scale.egress import EgressDenied, EgressGate  # noqa: E402
from scale.util import load_doc, write_canonical  # noqa: E402
from scaleweb import agent, auth, runner  # noqa: E402
from scaleweb.app import Service  # noqa: E402
from scaleweb.config import Config  # noqa: E402

PUBLIC = REPO / "examples" / "methanol-synthesis-public"
CANARY = "CANARY-5d0e2b-unpublished-catalyst"
PLAN = {"match": {"label": "Catalyst", "as": "c"}, "return": ["c.name"], "limit": 500}


class Sink:
    """A mocked provider: records every request it receives and answers with one query, then a text."""

    def __init__(self):
        self.seen = []
        self.messages = self

    def create(self, **kw):
        self.seen.append(json.dumps(kw, default=str))
        blocks = ([{"type": "tool_use", "id": "q1", "name": "query", "input": {"plan": PLAN}}] if len(self.seen) == 1
                  else [{"type": "text", "text": "answered"}])
        return SimpleNamespace(content=blocks, stop_reason="tool_use", usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    def received(self, text):
        return any(text in s for s in self.seen)


def private_graph():
    """The public example with its header made project-only and one catalyst named by a canary."""
    g = load_doc(PUBLIC)
    g["graph"]["stewardship"] = {"access": "project", "project": "lab:project/a"}
    cat = next(n for n in g["nodes"] if n["label"] == "Catalyst")
    cat["props"]["name"] = CANARY
    for n in g["nodes"] + g["links"]:                      # every record follows the header
        (n.get("props") or {}).pop("stewardship", None)
    return g


def cfg(tmp_path, **kw):
    return Config(secret="s" * 40, github_client_id="cid", allowlist=["ada"], site_origins=["https://x.example"],
                  public_url="https://scale.example", workdir=str(tmp_path / "jobs"), **kw)


def ask(svc, graph):
    h = {"authorization": f"Bearer {auth.session_for(svc.cfg, 'ada')}"}
    return svc.route("POST", "/ask", {}, h, json.dumps({"graph": graph, "question": "Which catalysts?"}).encode())


def test_the_service_refuses_to_send_a_private_graph_to_its_model(tmp_path):
    svc = Service(cfg(tmp_path), repo=REPO)
    sink = Sink()
    svc.client_factory = lambda: sink
    st, body, _ = ask(svc, private_graph())
    assert st == 403 and body["egress"] == "denied" and "project" in body["error"] and "'anthropic'" in body["error"]
    assert sink.seen == []                                                     # the model was never called
    st, body, _ = ask(svc, load_doc(PUBLIC))                                   # a public graph goes as before
    assert st == 200 and sink.seen and not sink.received(CANARY)


def test_with_claude_code_too_nothing_runs_for_a_private_graph(tmp_path, monkeypatch):
    svc = Service(cfg(tmp_path, ask="claude-code"), repo=REPO)
    monkeypatch.setattr(runner, "run_claude", lambda *a, **k: pytest.fail("Claude Code must not run"))
    st, body, _ = ask(svc, private_graph())
    assert st == 403 and "egress denied" in body["error"]


def test_an_approved_endpoint_receives_the_records(tmp_path):
    pol = tmp_path / "egress.yaml"
    pol.write_text("egress:\n  endpoints:\n  - id: anthropic\n    access: [project]\n")
    svc = Service(cfg(tmp_path, egress_policy=str(pol)), repo=REPO)
    sink = Sink()
    svc.client_factory = lambda: sink
    st, body, _ = ask(svc, private_graph())
    assert st == 200 and body["answer"] == "answered" and sink.received(CANARY)
    # approved for another endpoint name only: denied
    svc = Service(cfg(tmp_path, egress_policy=str(pol), egress_endpoint="other"), repo=REPO)
    svc.client_factory = lambda: pytest.fail("no model is called")
    assert ask(svc, private_graph())[0] == 403


def test_the_question_loop_denies_by_default_and_checks_each_batch_of_tool_results(tmp_path):
    p = tmp_path / "g.graph.json"
    write_canonical(p, private_graph())
    sink = Sink()
    with pytest.raises(EgressDenied):
        agent.answer(p, "Which catalysts?", sink, "m", 5, 10**6, repo=REPO)       # no gate given: deny by default
    assert sink.seen == []
    # the loop's own check: tool results that may not leave stop the loop before the next request
    sink = Sink()
    calls = []

    def gate(results):
        calls.append(results)
        raise EgressDenied("egress denied: test")
    loop = agent.Loop(sink, "m", "skill", [], {"query": lambda a: {"rows": [CANARY]}}, max_turns=5, max_tokens=10**6,
                      before_send=gate)
    with pytest.raises(EgressDenied):
        loop.run("q")
    assert len(sink.seen) == 1 and not sink.received(CANARY) and CANARY in json.dumps(calls)
    # a gate approving the endpoint lets the answer run
    sink = Sink()
    res = agent.answer(p, "Which catalysts?", sink, "m", 5, 10**6, repo=REPO,
                       egress=EgressGate("local", [{"id": "local", "url": None, "access": ["project"]}]))
    assert res["answer"] == "answered" and sink.received(CANARY)
