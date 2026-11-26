"""Egress: which model endpoints may receive a payload drawn from a document, deny by default (docs/private-data.md).

A payload's levels are the access levels its document grants (:func:`scale.audience.payload_levels`). A public payload
may go to any endpoint. A payload with any other level goes only to an endpoint the trusted policy lists for every one
of its non-public levels; an endpoint the policy does not list, or no policy at all, receives public payloads only.

The policy is the ``egress`` section of a data repository's ``scale-data.yaml`` (:mod:`scale.audience`), or a file
named by ``$SCALE_EGRESS_POLICY`` holding the same section (``egress: {endpoints: [...]}``). The endpoint a process
sends to is named by whoever starts it (``$SCALE_EGRESS_ENDPOINT``, or the caller's argument): the gate trusts that
name, so it guards against accidents, not against a process that lies about where it sends.

Where it is enforced: the agent tools served to a model (``scale mcp``, ``scale serve``, ``scale call``; a
:class:`scale.api.ScaleToolkit` given a gate), the SCALE service's question loop (``service/scaleweb``) and, in the
browser, the graph explorer's own-key questions (``site/egress.js``).
"""

from __future__ import annotations

import os
from pathlib import Path

from .audience import LEVELS, POLICY_FILE, find_policy, parse_endpoints, parse_policy, payload_levels
from .util import ScaleError, load_yaml

POLICY_ENV = "SCALE_EGRESS_POLICY"
ENDPOINT_ENV = "SCALE_EGRESS_ENDPOINT"
UNKNOWN = "unknown"


class EgressDenied(ScaleError):
    """A payload that may not go to the endpoint asked for."""


def load_endpoints(path: str | Path) -> list[dict]:
    """The approved endpoints of a policy file: a data repository's ``scale-data.yaml``, or a file holding only
    ``egress: {endpoints: [...]}``."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ScaleError(f"{p}: no such egress policy file")
    data = load_yaml(p) or {}
    if isinstance(data, dict) and "scale_data_policy" in data:
        return parse_policy(data, str(p))["endpoints"]
    if not isinstance(data, dict) or set(data) - {"egress"} or not isinstance(data.get("egress") or {}, dict) \
            or set(data.get("egress") or {}) - {"endpoints"}:
        raise ScaleError(f"{p}: an egress policy holds egress: {{endpoints: [{{id, url, access}}]}}")
    return parse_endpoints((data.get("egress") or {}).get("endpoints"), f"{p}: egress.endpoints")


class EgressGate:
    """The check before a payload leaves for a model endpoint. ``endpoints`` are the approved endpoints (None: read
    them per document, from ``$SCALE_EGRESS_POLICY`` or the data repository holding the document; none found: none
    approved); ``endpoint`` names where the payloads go."""

    def __init__(self, endpoint: str | None = None, endpoints: list[dict] | None = None):
        self.endpoint = (endpoint or UNKNOWN).strip() or UNKNOWN
        self.endpoints = endpoints

    @classmethod
    def from_env(cls, endpoint: str | None = None) -> EgressGate:
        """A gate for a process serving tools to a model: the endpoint from the argument or ``$SCALE_EGRESS_ENDPOINT``,
        the policy from ``$SCALE_EGRESS_POLICY`` when it is set (else per document)."""
        env = os.environ.get(POLICY_ENV, "").strip()
        return cls(endpoint or os.environ.get(ENDPOINT_ENV), load_endpoints(env) if env else None)

    def approved(self, where: str | Path | None = None) -> set[str]:
        """The levels the policy approves for this gate's endpoint (public always)."""
        endpoints = self.endpoints
        if endpoints is None:
            pf = find_policy(where) if where is not None else None
            endpoints = parse_policy(load_yaml(pf), str(pf))["endpoints"] if pf else []
        return {"public"} | {lv for e in endpoints if e["id"] == self.endpoint for lv in e["access"]}

    def denied(self, levels: set[str], where: str | Path | None = None) -> list[str]:
        return sorted(set(levels) - self.approved(where), key=lambda lv: LEVELS.index(lv) if lv in LEVELS else 99)

    def check(self, doc: dict, what: str = "the document", where: str | Path | None = None) -> None:
        """Raise :class:`EgressDenied` when a payload drawn from ``doc`` may not go to this gate's endpoint; ``where``
        (the document's path) finds its data repository's policy when the gate has none of its own."""
        levels = payload_levels(doc)
        denied = self.denied(levels, where)
        if denied:
            raise EgressDenied(
                f"egress denied: {what} holds records whose access is {', '.join(denied)}, and the endpoint "
                f"{self.endpoint!r} is not approved for {'that level' if len(denied) == 1 else 'those levels'}. Nothing "
                f"was sent. A person who may decide approves it in the egress section of the data repository's "
                f"{POLICY_FILE} (or the file ${POLICY_ENV} names) and names the endpoint in ${ENDPOINT_ENV}; "
                "or use a public view (scale graph filter --public).")


__all__ = ["EgressDenied", "EgressGate", "load_endpoints", "POLICY_ENV", "ENDPOINT_ENV"]
