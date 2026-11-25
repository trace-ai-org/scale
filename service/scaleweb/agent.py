"""The two model loops: curate one source, and answer a question from a graph.

Both run the model (the Claude Messages API, through a client the caller passes) over tools that call the scale-catal
code itself: the curation loop reads the page texts, describes the schema, writes records into the working graph and
runs ``scale source check`` until it passes; the question loop runs ``describe``, ``stats``, ``search`` and ``query``
exactly as the CLI and the MCP server do. The skills (``skills/curate-corpus``, ``skills/query-graph``) are the system
prompts, so the procedure is the one an agent follows anywhere else. Each loop stops at its turn limit or token budget.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from scale import curate, pgraph
from scale.curation_tools import STOP, CurationTools, short_check, skill
from scale.api import ScaleToolkit
from scale.egress import EgressGate
from scale.util import find_repo_root, load_doc

RESULT_CHARS = 40_000          # the longest tool result handed back to the model


_skill = skill                 # a skill's procedure, without its front matter (runner.py too)


def _clip(obj) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= RESULT_CHARS else s[:RESULT_CHARS] + f"\n… ({len(s) - RESULT_CHARS} more characters cut)"


def _tool(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties or {}, "required": required or []}}


def _mark_cache(messages: list[dict]) -> None:
    """Cache breakpoints on the newest two user messages, so each turn reads the conversation so far from the cache
    and writes only what is new (with the system prompt's, three of the four breakpoints allowed)."""
    for m in messages:
        if isinstance(m["content"], list):
            for b in m["content"]:
                b.pop("cache_control", None)
    users = [m for m in messages if m["role"] == "user" and isinstance(m["content"], list) and m["content"]]
    for m in users[-2:]:
        m["content"][-1]["cache_control"] = {"type": "ephemeral"}


class Loop:
    """A tool-use loop over one conversation, with its token count and a log the page shows. The count is as billed,
    at the price of an input token: a token read from the cache counts a tenth, one written to it a quarter more."""

    def __init__(self, client, model: str, system: str, tools: list[dict], handlers: dict[str, Callable[[dict], object]],
                 max_turns: int, max_tokens: int, log: Callable[[str], None] = lambda s: None,
                 before_send: Callable[[list[dict]], None] | None = None):
        self.client, self.model, self.tools, self.handlers = client, model, tools, handlers
        self.system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self.max_turns, self.max_tokens, self.log = max_turns, max_tokens, log
        self.before_send = before_send          # raises (scale.egress.EgressDenied) before tool results may not leave
        self.tokens = 0

    def run(self, first: str, stop_tool: str | None = None) -> tuple[str, dict | None]:
        """Run to an answer (the model's last text) or to ``stop_tool``'s input; the reason when it ran out."""
        messages = [{"role": "user", "content": first}]
        for turn in range(self.max_turns):
            _mark_cache(messages)
            r = self.client.messages.create(model=self.model, max_tokens=16000, system=self.system,
                                            tools=self.tools, messages=messages)
            u = getattr(r, "usage", None)
            self.tokens += (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "output_tokens", 0) or 0) \
                + round(1.25 * (getattr(u, "cache_creation_input_tokens", 0) or 0)) \
                + round(0.1 * (getattr(u, "cache_read_input_tokens", 0) or 0))
            blocks = [b if isinstance(b, dict) else b.model_dump() for b in r.content]
            messages.append({"role": "assistant", "content": blocks})
            text = "\n".join(b["text"] for b in blocks if b["type"] == "text").strip()
            calls = [b for b in blocks if b["type"] == "tool_use"]
            if not calls:
                return text, None
            results = []
            for c in calls:
                if c["name"] == stop_tool:
                    return text, c["input"]
                self.log(f"{c['name']} {_clip(c['input'])[:160]}")
                try:
                    out = self.handlers[c["name"]](c["input"])
                    results.append({"type": "tool_result", "tool_use_id": c["id"], "content": _clip(out)})
                except Exception as e:                        # a tool's failure goes back to the model as data
                    results.append({"type": "tool_result", "tool_use_id": c["id"], "is_error": True,
                                    "content": f"{type(e).__name__}: {e}"})
            if self.before_send is not None:
                self.before_send(results)
            messages.append({"role": "user", "content": results})
            if self.tokens >= self.max_tokens:
                return text, {"stopped": f"token budget reached ({self.tokens} tokens)"}
        return "", {"stopped": f"turn limit reached ({self.max_turns} turns)"}


# ------------------------------------------------------------------------------------------------- curation


def curate_folder(folder: Path, client, model: str, max_turns: int, max_tokens: int,
                  log: Callable[[str], None] = lambda s: None, repo: Path | None = None) -> dict:
    """Fill the graph of a ``scale source start`` folder by the curate-corpus skill until ``scale source check``
    passes (or the loop runs out); returns the check, the model's summary and the tokens spent. The tools are
    ``scale.curation_tools``, the ones the graph explorer gives a model with a reader's own key."""
    ct = CurationTools(folder, repo)
    handlers = {t["name"]: (lambda a, n=t["name"]: ct.call(n, a)) for t in ct.definitions() if t["name"] != STOP}
    loop = Loop(client, model, ct.system(), ct.definitions(), handlers, max_turns, max_tokens, log)
    text, stop = loop.run(ct.first(), stop_tool=STOP)
    final = curate.check(folder, ct.repo, derive_links=True)
    return {"check": short_check(final), "summary": (stop or {}).get("summary") or text,
            "stopped": (stop or {}).get("stopped"), "tokens": loop.tokens}


# ------------------------------------------------------------------------------------------------- questions


def answer(graph_path: Path, question: str, client, model: str, max_turns: int, max_tokens: int,
           log: Callable[[str], None] = lambda s: None, repo: Path | None = None, egress: EgressGate | None = None) -> dict:
    """Answer a question from one graph by the query-graph skill: plans run by ``scale query``, evidence by
    ``scale search``; returns the answer, the plans run with their results' kinds, and the tokens spent.

    ``egress`` (default: a gate for the endpoint ``anthropic`` with no endpoint approved) refuses, before the model is
    called at all, a graph whose records are not public unless its policy approves the endpoint for their levels; and
    it is asked again before each batch of tool results leaves (scale.egress.EgressDenied)."""
    gate = egress if egress is not None else EgressGate("anthropic", [])
    doc = load_doc(graph_path)
    gate.check(doc, "the graph asked about")
    repo = Path(repo) if repo else find_repo_root()
    kit = ScaleToolkit(workspace=graph_path.parent, repo=repo)
    plans: list[dict] = []

    def run_query(a: dict):
        res = kit.call("query", {"path": graph_path.name, "plan": a["plan"]})
        plans.append({"plan": a["plan"], "status": res.get("status") or res.get("error"),
                      "rows": res.get("row_count", 0)})
        return res

    tools = [
        _tool("describe", "The schema: labels and edge types with their properties (scale describe).",
              {"names": {"type": "array", "items": {"type": "string"}}}),
        _tool("stats", "The graph's units in use per property and its link patterns (scale graph stats)."),
        _tool("search", "Search the graph's evidence by words and meaning (scale graph search); hits lead to the "
              "records that cite them.", {"query": {"type": "string"}, "k": {"type": "integer"},
                                          "published_before": {"type": "string"}}, ["query"]),
        _tool("query", "Run a graph query plan (scale graph query): match, steps, where, return, evidence, order_by, "
              "limit, text, published_before. Returns rows, the kind of answer and the coverage.",
              {"plan": {"type": "object"}}, ["plan"]),
    ]
    handlers = {
        "describe": lambda a: kit.call("describe", {"names": a.get("names") or [], "domain": "catalysis"}),
        "stats": lambda a: pgraph.stats(load_doc(graph_path)),
        "search": lambda a: kit.call("search", {"path": graph_path.name, "query": a["query"], "k": int(a.get("k") or 10),
                                                **({"published_before": a["published_before"]}
                                                   if a.get("published_before") else {})}),
        "query": run_query,
    }
    system = (_skill(repo, "query-graph") + "\n\n# Working here\n\nYou answer in a web page, through the tools given "
              "(no shell). The graph is the one the user is viewing. Write the answer in Markdown: every factual "
              "sentence from returned rows, citing record ids and evidence locators in brackets, then a **Limits** "
              "line. Keep your own extrapolations in a separate, labelled paragraph.")
    loop = Loop(client, model, system, tools, handlers, max_turns, max_tokens, log,
                before_send=lambda results: gate.check(doc, "the graph asked about"))
    text, stop = loop.run(question)
    return {"answer": text, "plans": plans, "stopped": (stop or {}).get("stopped"), "tokens": loop.tokens}
